import base64
import json
import math
import multiprocessing
import os
import threading
import time
from collections import deque
from io import BytesIO
import requests
from PIL import Image, ImageDraw, ImageFont
from flask import Response
import octoprint.plugin
from octoprint.events import Events
import onnxruntime
import telebot
import re

from .inference import image_inference

class PinozcamPlugin(octoprint.plugin.StartupPlugin,
                     octoprint.plugin.TemplatePlugin,
                     octoprint.plugin.SettingsPlugin,
                     octoprint.plugin.AssetPlugin,
                     octoprint.plugin.BlueprintPlugin,
                     octoprint.plugin.EventHandlerPlugin):
    """
    An OctoPrint plugin that enhances 3D printing with AI-based monitoring for potential print failures.
    
    Attributes:
        lock (threading.Lock): A lock to ensure thread-safe operations.
        stop_event (threading.Event): An event to signal stopping of threads.
        count (int): A counter to track detected failures.
        action (int): Determines the action to take upon detection (0: notify, 1: pause, 2: stop).
        ai_input_image (PIL.Image.Image): The current image being analyzed by AI.
        ai_results (collections.deque): Stores recent AI analysis results.
        telegram_bot_token (str): Token for Telegram bot integration.
        telegram_chat_id (str): Chat ID for Telegram notifications.
        ai_running (bool): Indicates if AI processing is active.
        num_threads (int): Num of threads to use for AI inference.
        snapshot (str): URL for the camera snapshot.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

        #external setup Parameters:
        self.enable_AI=True
        self.action = 0
        self.ai_start_delay = 0
        self.print_layout_threshold=0.5
        self.img_sensitivity=0.04
        self.scores_threshold=0.75
        self.max_count=2
        self.enable_max_failure_count_notification = True
        self.count_time = 300
        self.max_notification=0
        self.telegram_bot_token = ""
        self.telegram_chat_id = ""
        self.custom_snapshot_url = ""
        self.discord_webhook_url= ""

        #internal parameters
        self.mask_image_data = '0' * 4096 # 64*64 mask matrix
        self.count = 0
        self.welcome_text = "Welcome to PiNozCam!"
        self.no_camera_text = "No Camera"
        self.proc_img_width=640
        self.proc_img_height=384
        self.font = None
        self.ai_running = False
        self.num_threads = 1
        self.ai_input_image = None
        self.ai_results = deque(maxlen=100)
        self.notification_reach_to_max=False
        self.setting_change_while_printing=False
        self.current_telegram_message_set = set()
        self.current_telegram_message_mute = False
        self.telegram_pending_action = None
        self.current_telegram_message_paused = False
        self.telegram_server_running = False

        #files:
        self.font_path = os.path.join(os.path.dirname(__file__), 'static', 'Arial.ttf')
        self.no_camera_path = os.path.join(os.path.dirname(__file__), 'static', 'no_camera.jpg')
        self.bin_file_path = os.path.join(os.path.dirname(__file__),'static', 'nozcam.bin')
        
        #camera
        self.cameras = []
        self.snap_new_method = False

    def initialize_cameras(self):
        self._logger.info("Initialize the camera")
        if hasattr(octoprint.plugin.types, "WebcamProviderPlugin"):
            self.cameras = self._plugin_manager.get_implementations(octoprint.plugin.types.WebcamProviderPlugin)
            self.snap_new_method = True
        else:
            self.cameras = []
            self.snap_new_method = False

    def initialize_font(self, font_size=28):
        """
        Initializes the font for the plugin and caches it for later use.

        Parameters:
        - font_size: The size of the font.
        """
        #check file exist
        if not os.path.exists(self.font_path):
            self._logger.error(f"Font file does not exist: {self.font_path}")
            self.font_path = None 

        try:
            self.font = ImageFont.truetype(self.font_path, font_size)
            self._logger.info("Customized font loaded successfully.")
        except IOError as e:
            self.font = ImageFont.load_default()
            self._logger.info(f"Failed to load custom font, using default font. Error: {e}")

    def _encode_no_camera_image(self):
        """
        Encodes a 'no camera' placeholder image to a base64 string.
        
        Returns:
            A base64 encoded string of the 'no camera' image.
        """
        no_camera_path = os.path.join(os.path.dirname(__file__), 'static', 'no_camera.jpg')
        try:
            with open(no_camera_path, "rb") as image_file:
                return f"data:image/jpeg;base64,{base64.b64encode(image_file.read()).decode('utf-8')}"
        except FileNotFoundError:
            self._logger.error(f"No camera image not found at {no_camera_path}")
            return self.create_image_with_text(self.no_camera_text)
    
    def cpu_is_raspberry_pi(self):
        """
        Checks if the script is running on a Raspberry Pi.

        Returns:
            bool: True if the CPU is a Raspberry Pi, False otherwise.
        """
        try:
            with open("/proc/cpuinfo", "r") as f:
                return "Raspberry Pi" in f.read()
        except IOError:
            return False

    def get_cpu_temperature(self):
        if self.cpu_is_raspberry_pi():
            try:
                with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                    temp_str = f.read()
                    temp_c = int(temp_str) / 1000.0
                    return temp_c
            except FileNotFoundError:
                return 0
        else:
            return 0
    
    def get_settings_defaults(self):
        return dict(
            maskImageData='0' * 4096, # 64*64 mask matrix
            enableAI=True,
            action=0,
            aiStartDelay=0,
            printLayoutThreshold=0.5,
            imgSensitivity=0.04,
            scoresThreshold=0.75,
            maxCount=2,
            enableMaxFailureCountNotification=True,
            countTime=300,
            cpuSpeedControl=0.5,
            customSnapshotURL="",
            maxNotification=0,
            telegramBotToken="",
            telegramChatID="",
            discordWebhookURL="",
        )
    
    def apply_mask_to_image(self, input_image):
        """
        Applies a black mask to the input image based on the stored mask_image_data.

        Args:
            input_image (PIL.Image): The input image to apply the mask to.

        Returns:
            PIL.Image: The input image with the black mask applied.
        """
        # Decompress the mask_image_data into a 2D boolean array
        mask_matrix = [[char == '1' for char in self.mask_image_data[i:i+64]] for i in range(0, len(self.mask_image_data), 64)]

        # Create a drawing context for the input image
        draw = ImageDraw.Draw(input_image)
        
        # Draw black rectangles on the input image based on the mask_matrix
        block_width = math.ceil(input_image.size[0] / 64)
        block_height = math.ceil(input_image.size[1] / 64)
        for i in range(64):
            for j in range(64):
                if mask_matrix[i][j]:
                    x, y = j * block_width, i * block_height
                    draw.rectangle((x, y, x + block_width, y + block_height), fill=(0, 0, 0))

        return input_image

    def perform_action(self):
        """
        Executes an action based on the current setting of 'self.action'.
        - If action is 1, it pauses the print.
        - If action is 2, it stops the print.
        - Otherwise, it logs that there is no interference with the printing process.
        """
        if self.action == 1:
            self._logger.info("Pausing print...")
            self._printer.pause_print()
            self._logger.info("Print paused.")
        elif self.action == 2:
            self._logger.info("Stopping print...")
            self._printer.cancel_print()
            self._logger.info("Print stopped.")
        else:
            self._logger.info("No interference with the printing process.")
    
    def on_after_startup(self):
        """
        Initializes plugin settings after startup by loading values from the configuration.
        It also logs the initialized settings for verification.
        """
        self.mask_image_data = self._settings.get(["maskImageData"])
        self.enable_AI = self._settings.get_int(["enableAI"])
        self.action = self._settings.get_int(["action"])
        self.ai_start_delay = self._settings.get_int(["aiStartDelay"])
        self.print_layout_threshold = self._settings.get_float(["printLayoutThreshold"])
        self.img_sensitivity = self._settings.get_float(["imgSensitivity"])
        self.scores_threshold = self._settings.get_float(["scoresThreshold"])
        self.max_count = self._settings.get_int(["maxCount"])
        self.enable_max_failure_count_notification = self._settings.get_boolean(["enableMaxFailureCountNotification"])
        self.count_time = self._settings.get_int(["countTime"])
        self.cpu_speed_control = self._settings.get_float(["cpuSpeedControl"])
        self.custom_snapshot_url = self._settings.get(["customSnapshotURL"])
        self.max_notification = self._settings.get(["maxNotification"])
        self.telegram_bot_token = self._settings.get(["telegramBotToken"])
        self.telegram_chat_id = self._settings.get(["telegramChatID"])
        self.discord_webhook_url = self._settings.get(["discordWebhookURL"])

        if "/?action=stream" in self.custom_snapshot_url:
            self.custom_snapshot_url = self.custom_snapshot_url.replace("/?action=stream", "/?action=snapshot")
            self._logger.info(f"self.custom_snapshot_url has been changed to {self.custom_snapshot_url}")

        # Calculate the number of threads to use for AI inference       
        self._thread_calculation()
        #

        self.initialize_cameras()

        self.initialize_font()
        
        if not os.path.exists(self.bin_file_path):
            self._logger.error(f"No bin file does not exist: {self.bin_file_path}")
            self.bin_file_path = None 
        
        if not os.path.exists(self.no_camera_path):
            self._logger.error(f"No camera image file does not exist: {self.no_camera_path}")
            self.no_camera_path = None 
        
        self.setup_telegram_bot()
        

    def start_telegram_bot(self):
        @self.telegram_bot.message_handler(commands=['hi'])
        def send_welcome(message):
            # Use the get_snapshot method to get the processed image
            input_unmasked_image = self.get_snapshot()
            if input_unmasked_image:
                title, state, progress, nozzle_temp, bed_temp, file_metadata = self.get_printer_status()
                status_message = f"Printer: {title}\nStatus: {state}\nProgress: {progress}\nNozzle Temp: {nozzle_temp}°C\nBed Temp: {bed_temp}°C"
                if file_metadata:
                    status_message += f"\nFile: {file_metadata.get('name', 'Unknown')}"
                self.telegram_send_with_reply(image=input_unmasked_image, caption=status_message, reply_buttons=4, disable_notification=True)
            else:
                self.telegram_send_with_reply(caption="No camera connected.", reply_buttons=0, disable_notification=True)
        
        @self.telegram_bot.message_handler(func=lambda message: True)
        def echo_all(message):
            response_text = "I am PiNozCam. Send /hi or click Check button from previous messages to see the current camera view."
            self.telegram_bot.reply_to(message, response_text)
        

        #try to start the telegram_bot polling for 3 times
        max_retries = 3
        retry_count = 0
    
        while retry_count < max_retries:
            try:
                self.telegram_bot.infinity_polling(long_polling_timeout=60)
                self._logger.info("Telegram bot polling stopped gracefully")
                break
            except Exception as e:
                retry_count += 1
                self._logger.error(f"Telegram bot polling error (attempt {retry_count}/{max_retries}): {str(e)}")
                if retry_count < max_retries:
                    self._logger.info("Retrying Telegram bot polling in 5 seconds...")
                    time.sleep(5)
        
        if retry_count == max_retries:
            self._logger.error("Telegram bot polling failed after 3 attempts. Stopping.")

    def telegram_check_setting(self):
        """
        Tests if the Telegram chat_id and bot token are correct by attempting to retrieve chat information.
        If the chat information is retrieved, it indicates that the chat_id and token are valid.
        
        This function makes a GET request to the Telegram API's getChat endpoint using the stored
        chat_id and bot token. A successful retrieval of chat information means that the provided
        credentials are correct.

        Returns:
        bool: True if the chat information is successfully retrieved, otherwise False.

        Exception Handling:
        - This function catches exceptions related to network issues and logs an error if the API call fails.
        """
        telegram_api_url = f"https://api.telegram.org/bot{self.telegram_bot_token}/getChat"
        data = {'chat_id': self.telegram_chat_id}

        try:
            # Attempt to retrieve chat information
            response = requests.get(telegram_api_url, params=data)
            if response.status_code == 200:
                self._logger.info("Successfully retrieved chat information. Telegram settings are correct.")
                return True
            else:
                self._logger.error(f"Failed to retrieve chat information. Status Code: {response.status_code}, Response: {response.text}")
                return False
        except requests.exceptions.RequestException as e:
            # Handle exceptions related to the request, such as connectivity issues, timeouts, etc.
            self._logger.error(f"An error occurred while attempting to verify Telegram settings: {str(e)}")
            return False
    
    def telegram_send_without_reply(self, image=None, caption=" "):
        """
        Sends an alert message with an image to a Telegram chat.
        The message includes details such as printer name, severity, failure area, failure count, and max failure count.

        Parameters:
        - image: The PIL Image object to send.
        - caption: The caption of the message.

        Returns:
        bool: True if the message was sent successfully, False otherwise.

        Exception Handling:
        - Handles exceptions related to network issues or other unforeseen errors that could occur during the request.
        """
        telegram_api_url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        data = {'chat_id': self.telegram_chat_id}
        files = None

        try:
            if image:
                image_stream = BytesIO()
                image.save(image_stream, format='JPEG')
                image_stream.seek(0)
                files = {'photo': ('image.jpeg', image_stream, 'image/jpeg')}
                data['caption'] = caption
                telegram_api_url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendPhoto"
            else:
                data['text'] = caption

            response = requests.post(telegram_api_url, files=files, data=data)
            if response.status_code == 200:
                self._logger.info("Telegram message sent successfully.")
                return True
            else:
                self._logger.error(f"Failed to send message to Telegram. Status Code: {response.status_code}, Response: {response.text}")
                return False
        except requests.exceptions.RequestException as e:
            self._logger.error(f"Error occurred while sending message to Telegram: {str(e)}")
            return False


    def discord_send(self, image=None, caption=""):
        """
        Sends an alert message with an image to a Discord channel via webhook.
        
        Parameters:
        - image: The PIL Image object to send.
        - caption: The caption of the message, including details such as printer name, severity, failure area, failure count, and max failure count.
        
        The function attempts to send an image and a text message to the configured Discord webhook URL.
        It logs a success message if the HTTP request is successful, or an error if it fails.
        
        Exception Handling:
        - Handles exceptions related to network issues or other unforeseen errors that could occur during the request.
        - Logs detailed error information to assist with troubleshooting.

        Returns:
        bool: True if the message was sent successfully, False otherwise.
        """

        try:
            image_stream = BytesIO()
            image.save(image_stream, format='JPEG')
            image_stream.seek(0)
            files = {'file': ('image.jpeg', image_stream, 'image/jpeg')}
            data = {"content": caption}
            response = requests.post(self.discord_webhook_url, files=files, data=data)
            
            if response.status_code in [200, 204]:
                self._logger.info("Message sent to Discord successfully.")
                return True
            else:
                self._logger.error(f"Failed to send message to Discord. Status Code: {response.status_code}, Response: {response.json()}")
                return False
        except requests.exceptions.RequestException as e:
            self._logger.error(f"Error occurred while sending message to Discord: {str(e)}")
            return False
        except Exception as e:
            self._logger.error(f"An unexpected error occurred while sending message to Discord: {str(e)}")
            return False


    def on_event(self, event, payload):
        """
        Handles OctoPrint events to start or stop AI image processing based on the printer's status.
        - Initiates AI processing when a print job starts or resumes.
        - Stops AI processing when a print job is done, failed, cancelled, or paused.
        """
        if event in [Events.PRINT_STARTED, Events.PRINT_RESUMED]:
            self._logger.info(f"{event}: {payload}")
            self._logger.info("Print started, beginning AI image processing.")
            if event == Events.PRINT_STARTED:
                self._logger.info("Count and results are cleared.")
                #initial the parameters
                self.count = 0
                self.ai_results.clear()
                self.notification_reach_to_max=False
                self.current_telegram_message_paused = False
                self.current_telegram_message_set.clear()
            if not hasattr(self, 'ai_thread') or not self.ai_thread.is_alive():
                self.ai_running = True
                self.ai_thread = threading.Thread(target=self.process_ai_image)
                self.ai_thread.daemon = True
                self.ai_thread.start()
        elif event in [Events.PRINT_DONE, Events.PRINT_FAILED, Events.PRINT_CANCELLED, Events.PRINT_PAUSED]:
            self._logger.info(f"{event}: {payload}")
            self._logger.info("Print ended, stopping AI image processing.")
            self.ai_running = False
            self.current_telegram_message_set.clear()

    def encode_image_to_base64(self, image):
        """
        Encodes a PIL Image object to a base64 string for easy embedding or storage.
        """
        buffered = BytesIO()
        image.save(buffered, format="JPEG")
        return "data:image/jpeg;base64," + base64.b64encode(buffered.getvalue()).decode('utf-8')

    def process_ai_image(self):
        """
        Continuously processes images from a camera to detect failures using AI inference.
        It fetches the latest image, performs inference, and takes action based on the results.
        This function runs in a dedicated thread to not block the main plugin operations.
        """
        while self.ai_running:
            while not self.enable_AI and self.ai_running:
                time.sleep(1)  # if enable_AI == False, pause 1 second and check

            if not self.ai_running:
                break
            
            # Load model_data into memory
            self._logger.info("begin loading AI Model into memory.")
            try:
                with open(self.bin_file_path, 'rb') as model_file:
                    model_data = model_file.read()
                self._logger.info("Successfully loaded AI Model into memory.")

                # Initialize SessionOptions and InferenceSession here
                sess_opt = onnxruntime.SessionOptions()
                sess_opt.intra_op_num_threads = self.num_threads
                ort_session = onnxruntime.InferenceSession(model_data, sess_opt, providers=['CPUExecutionProvider'])
                self._logger.info("InferenceSession initialized.")
            except Exception as e:
                self._logger.error(f"Failed to load model from {self.bin_file_path}. Error: {e}")
                self.ai_running = False
            
            self._logger.info(f"Waiting for {self.ai_start_delay}s before starting AI processing")
            time.sleep(self.ai_start_delay)

            while self.enable_AI and self.ai_running:
                
                #get rid of results longer than count_time
                with self.lock:
                    while self.ai_results and time.time() - self.ai_results[0]['time'] > self.count_time:
                        result = self.ai_results.popleft()
                        if result['severity'] > 0.66:
                            self.count -= 1
                            #self._logger.info(f"Reduced failure count: {self.count}")
                
                #self._logger.info("Begin to process one image.")
                
                ai_unmasked_input_image = self.get_snapshot()
                if ai_unmasked_input_image is None:
                    self._logger.error(f"Failed to fetch image for AI processing: {e}")
                    continue

                #apply mask to the ai_input_image
                ai_input_image = self.apply_mask_to_image(ai_unmasked_input_image)

                try:
                    scores, boxes, labels, severity, percentage_area, elapsed_time = image_inference(
                        input_image=ai_input_image, 
                        scores_threshold=self.scores_threshold, 
                        img_sensitivity=self.img_sensitivity, 
                        ort_session=ort_session, 
                        _proc_img_width=self.proc_img_width, 
                        _proc_img_height=self.proc_img_height
                    )
                except Exception as e:
                    self._logger.error(f"AI inference error: {e}")
                    continue
                self._logger.info(f"scores={scores} boxes={boxes} labels={labels} severity={severity} percentage_area={percentage_area} elapsed_time={elapsed_time}")
                #draw the result image
                ai_result_image = self.draw_response_data(scores, boxes, labels, severity, ai_input_image)

                if self.setting_change_while_printing:
                    self.setting_change_while_printing=False
                    continue
                
                # Store the result
                if severity > 0.33:
                    result = {
                        'time': time.time(),
                        'scores': scores,
                        'boxes': boxes,
                        'labels': labels,
                        'severity': severity,
                        'percentage_area': percentage_area,
                        'elapsed_time': elapsed_time,
                        'ai_input_image': self.encode_image_to_base64(ai_input_image),
                        'ai_result_image': self.encode_image_to_base64(ai_result_image)
                    }
                    with self.lock:
                        self.ai_results.append(result)
                    #self._logger.info("Stored new AI inference result.")
                    if severity > 0.66:
                        with self.lock:
                            self.count += 1
                        #self._logger.info(f"self.count increased by 1 self.count={self.count}")
                        
                        #       
                        if not self.notification_reach_to_max and self.max_notification != 0 and self.count > self.max_notification:
                            self.notification_reach_to_max = True 

                        with self.lock:
                            failure_count = self.count

                        title, state, progress, nozzle_temp, bed_temp, file_metadata = self.get_printer_status()
                        status_message = f"Printer: {title}\nStatus: {state}\nProgress: {progress}\nNozzle Temp: {nozzle_temp}°C\nBed Temp: {bed_temp}°C"
                        if file_metadata:
                            status_message += f"\nFile: {file_metadata.get('name', 'Unknown')}"

                        severity_percentage = severity * 100
                        caption = (
                                f"{status_message}\n"
                                f"Severity: {severity_percentage:.2f}%\n"
                                f"Failure Area: {percentage_area:.2f}\n"
                                f"Failure Count: {failure_count}\n"
                                f"Max Failure Count: {self.max_count}\n"
                                )
                        
                        if self.telegram_pending_action:
                            action_time, _ = self.telegram_pending_action
                            if time.time() - action_time > 20:
                                self.telegram_pending_action = None

                        # If count exceeds  within the last count_time minutes, perform action
                        if not self.notification_reach_to_max and self.telegram_bot_token and self.telegram_chat_id:
                            if not self.enable_max_failure_count_notification or (failure_count >= self.max_count):
                                if not self.telegram_pending_action and not self.current_telegram_message_mute:
                                    #self.telegram_send(ai_result_image,severity,percentage_area)
                                    self.telegram_send_with_reply(image=ai_result_image, caption=caption, reply_buttons=4, disable_notification=False)
                        
                        if not self.notification_reach_to_max and self.discord_webhook_url.startswith("http"):
                            if not self.enable_max_failure_count_notification or (failure_count >= self.max_count):
                                self.discord_send(image=ai_result_image, caption=caption)
                        
                        if failure_count >= self.max_count:
                            self.perform_action()
                            
        sess_opt = None
    
    @staticmethod
    def _largest_power_of_two(n):
        exponent = math.floor(math.log2(n))
        return 2 ** exponent
    
    def _thread_calculation(self):
        total_cpu_cores = multiprocessing.cpu_count()
        num_threads_candidate = max(1, math.ceil(total_cpu_cores * self.cpu_speed_control))
        self.num_threads = self._largest_power_of_two(num_threads_candidate)
        self._logger.info(f"num_threads:{self.num_threads}")
    
    def draw_response_data(self, scores, boxes, labels, severity, image):
        """
        Draws bounding boxes and labels on the image based on inference results.

        Parameters:
        - scores: Confidence scores of the detected objects.
        - boxes: Coordinates of the bounding boxes for detected objects.
        - labels: Class labels for the detected objects.
        - severity: The severity level of the detection.
        - image: The original image on which detections are to be drawn.

        Returns:
        - The image with bounding boxes and labels drawn on it.
        """
        draw = ImageDraw.Draw(image)
        color = "green"  # Default color for bounding boxes

        # Change the color based on the severity of the detection
        if severity > 0.66:
            color = "red"
        elif severity > 0.33:
            color = "yellow"

        # Assuming you've already created an ImageDraw.Draw object named 'draw'
        for box, score in zip(boxes, scores):
            if score < self.scores_threshold:
                break
            x1, y1, x2, y2 = box
            draw.rectangle([(x1, y1), (x2, y2)], outline=color, width=2)
            
            # Text to be drawn
            spaghetti_text = ""
            score_text = f"{score:.2f}"  # Format the score to two decimal places

            # Adjust text position so it does not overlap with the bounding box
            spaghetti_text_position = (x1, y1 - 32)
            score_text_position = (x2 - 56, y1 - 26)
            
            # Ensure the text stays within the image boundaries
            if spaghetti_text_position[1] < 0:
                spaghetti_text_position = (x1, y2 + 5)
            if score_text_position[1] < 0:
                score_text_position = (x2 - 5, y2 + 5)

            # Draw text
            draw.text(spaghetti_text_position, spaghetti_text, fill=color, font=self.font)
            draw.text(score_text_position, score_text, fill=color, font=self.font)

        return image
    
    def create_image_with_text(self, text, image_size=None, text_color="black"):
        # Determine the image size
        if image_size is None:
            image_size = (self.proc_img_width, self.proc_img_height)
        
        # Create a blank image
        image = Image.new('RGB', image_size, (255, 255, 255))
        draw = ImageDraw.Draw(image)

        # Calculate text position for center alignment
        self._logger.info(f"self.font.getmask(text).getbbox()={self.font.getmask(text).getbbox()}")
        text_bbox=self.font.getmask(text).getbbox()
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]
        x = (image.width - text_width) / 2
        y = (image.height - text_height) / 2

        # Draw the text
        draw.text((x, y), text, fill=text_color, font=self.font)
        
        return image

    def setup_telegram_bot(self):
        """
        Set up and start the Telegram bot in a separate thread. This method initializes the Telegram bot
        using the stored bot token and chat ID, and then starts the bot in a daemon thread to handle messages
        asynchronously. If the bot is already running, it first stops the current bot before restarting it.
        
        This method should be called whenever the Telegram configuration might have changed, such as after
        updating settings or during plugin startup. This ensures that the bot is always running with the
        latest configuration.

        Exception Handling:
        - If initializing the Telegram bot or starting the thread fails, an error will be logged,
        and the method will attempt to safely shut down any partially started services.
        - It checks if the current configuration is valid using `telegram_check_setting` method before
        proceeding to start the bot. If settings are invalid, it logs an error and exits without starting the bot.

        Usage:
        - Call this method in `on_after_startup` to ensure the bot starts running when the plugin is loaded.
        - Call this method in `on_settings_save` to restart the bot with new settings after changes are made.

        Raises:
        - Exception: Captures any exceptions related to starting the bot thread or stopping a currently
        running bot, logs the error, and ensures no stray threads or services remain active.
        """
        if self.telegram_bot_token and self.telegram_chat_id and self.telegram_check_setting():
            try:
                if not hasattr(self, 'telegram_bot_thread') or not self.telegram_bot_thread.is_alive():
                    self.telegram_bot = telebot.TeleBot(self.telegram_bot_token)
                    self.telegram_bot_thread = threading.Thread(target=self.start_telegram_bot)
                    self.telegram_bot_thread.daemon = True
                    self.telegram_bot_thread.start()
                self.telegram_server_running=True
            except Exception as e:
                self._logger.error(f"An error occurred while setting up the Telegram bot: {str(e)}")
                # Attempt to stop the bot if it's running
                if hasattr(self, 'telegram_bot_thread') and self.telegram_bot_thread.is_alive():
                    try:
                        if hasattr(self, 'telegram_bot'):
                            self.telegram_bot.stop_polling()
                    except Exception as ex:
                        self._logger.error(f"Error occurred while stopping Telegram bot polling: {str(ex)}")
                self.telegram_server_running = False
                self._logger.info("Telegram bot has been stopped due to error.")   
        else:
            self._logger.error("Failed to send test message. Telegram bot will not be started.")
            if hasattr(self, 'telegram_bot_thread') and self.telegram_bot_thread.is_alive():
                if hasattr(self, 'telegram_bot'):
                    try:
                        self.telegram_bot.stop_polling()
                    except Exception as e:
                        self._logger.error(f"Error occurred while stopping Telegram bot polling: {str(e)}")

                self._logger.info(f"telegram_bot has been stopped.")
            self.telegram_server_running=False

    def on_settings_save(self, data):
        """
        Handles saving of plugin settings. Updates the plugin's configuration
        according to the data provided by the user in the UI.

        Parameters:
        - data: A dictionary containing the settings data to be saved.
        """
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

        # Update the plugin settings based on the data provided
        self.mask_image_data = data.get("maskImageData", self.mask_image_data)
        self.enable_AI = bool(data.get("enableAI", self.enable_AI))
        self.action = int(data.get("action", self.action))
        self.ai_start_delay = int(data.get("aiStartDelay", self.ai_start_delay))
        self.print_layout_threshold = float(data.get("printLayoutThreshold", self.print_layout_threshold))
        self.img_sensitivity = float(data.get("imgSensitivity", self.img_sensitivity))
        self.scores_threshold = float(data.get("scoresThreshold", self.scores_threshold))
        self.max_count = int(data.get("maxCount", self.max_count))
        self.enable_max_failure_count_notification = self._settings.get_boolean(["enableMaxFailureCountNotification"])
        self.count_time = int(data.get("countTime", self.count_time))
        self.cpu_speed_control = float(data.get("cpuSpeedControl", self.cpu_speed_control))
        self.custom_snapshot_url = data.get("customSnapshotURL", self.custom_snapshot_url)
        self.max_notification = int(data.get("maxNotification", self.max_notification))
        self.telegram_bot_token = data.get("telegramBotToken", self.telegram_bot_token)
        self.telegram_chat_id = data.get("telegramChatID", self.telegram_chat_id)
        self.discord_webhook_url = data.get("discordWebhookURL", self.discord_webhook_url)

        if "/?action=stream" in self.custom_snapshot_url:
            self.custom_snapshot_url = self.custom_snapshot_url.replace("/?action=stream", "/?action=snapshot")
            self._logger.info(f"self.custom_snapshot_url has been changed to {self.custom_snapshot_url}")
        self._logger.info("Plugin settings saved.")

        #re-initialize the parameters
        self._thread_calculation()
        self.initialize_cameras()
        self.initialize_font()
        self.notification_reach_to_max=False
        self.setting_change_while_printing=True

        #send a welcome test message to the telegram chat
        welcome_image = self.create_image_with_text(self.welcome_text)
        welcome_image = self.apply_mask_to_image(welcome_image)
        
        #telegram

        self.setup_telegram_bot()
            
        if self.telegram_server_running:
            self.telegram_send_with_reply(welcome_image, "Welcome to PiNozCam!", reply_buttons=0, disable_notification=True)
        
        #discord
        if self.discord_webhook_url.startswith("http"):

            self.discord_send(welcome_image, 0, 0, "Welcome to PiNozCam!")

    def get_printer_status(self):
        """
        Retrieves the current status of the printer including job progress, temperatures, and file metadata.
        """
        
        title = self._settings.global_get(["appearance", "name"])

        # Get the current printer data
        printer_data = self._printer.get_current_data()

        # Get the printer state
        state_id = self._printer.get_state_id()

        if state_id == "PRINTING":
            state = "🖨️ Printing"
            progress = printer_data['progress']['completion']  # Get print progress (percentage)
        elif state_id == "PAUSED":
            state = "⏸️ Paused"
            progress = printer_data['progress']['completion']
        elif state_id == "OPERATIONAL":
            state = "⏹️ Idle"
            progress = 0
        else:
            state = "❓ Unknown"
            progress = 0

        # Initialize temperature variables
        nozzle_temp = 0
        bed_temp = 0

        # Set default values if any value is None
        state = state or "Unknown"
        progress = f"{progress:.1f}%" if progress is not None else "0.0%"
        
        # Get temperature information
        temperatures = {}
        for k, v in self._printer.get_current_temperatures().items():
            if re.search(r'^(tool\d+|bed|chamber)$', k):
                temperatures[k] = v
        nozzle_temp = temperatures.get('tool0', {}).get('actual', nozzle_temp)
        bed_temp = temperatures.get('bed', {}).get('actual', bed_temp)

        # Get file metadata
        file_metadata = printer_data.get('job', {}).get('file', {})

        return title, state, progress, nozzle_temp, bed_temp, file_metadata


    def telegram_send_with_reply(self, image=None, caption='', reply_buttons=0, disable_notification=False):
        keyboard = None
        if reply_buttons == 2:
            keyboard = telebot.types.InlineKeyboardMarkup()
            keyboard.row(
                telebot.types.InlineKeyboardButton('Yes', callback_data='yes'),
                telebot.types.InlineKeyboardButton('No', callback_data='no')
            )
        elif reply_buttons == 4:
            keyboard = telebot.types.InlineKeyboardMarkup()
            keyboard.row(
                telebot.types.InlineKeyboardButton('Check', callback_data='check'),
                telebot.types.InlineKeyboardButton('Unmute' if self.current_telegram_message_mute else 'Mute', callback_data='mute')
            )
            keyboard.row(
                 telebot.types.InlineKeyboardButton('Resume' if self.current_telegram_message_paused else 'Pause', callback_data='pause'),
                telebot.types.InlineKeyboardButton('Stop', callback_data='stop')
            )

        try:
            if image:
                message = self.telegram_bot.send_photo(self.telegram_chat_id, image, caption=caption, reply_markup=keyboard, disable_notification=disable_notification)
            else:
                message = self.telegram_bot.send_message(self.telegram_chat_id, text=caption, reply_markup=keyboard, disable_notification=disable_notification)
            
            self._logger.info(f"Message sent to Telegram successfully. Message ID: {message.message_id}")
            if self.ai_running:
                self.current_telegram_message_set.add(message.message_id)
        except Exception as e:
            self._logger.error(f"Failed to send message to Telegram: {str(e)}")

        @self.telegram_bot.callback_query_handler(func=lambda call: True)
        def callback_query(call):
            message_id = call.message.message_id
            if call.data == "yes":
                if self.telegram_pending_action:
                    action_time, action_type = self.telegram_pending_action
                    self._logger.info(f"action_type={action_type}")
                    if time.time() - action_time <= 20:
                        if action_type == "pause" and (message_id in self.current_telegram_message_set):
                            if not self.current_telegram_message_paused:
                                self._logger.info(f"User confirmed to pause the print for message ID: {message_id}")
                                self._logger.info("Pausing print...")
                                self._printer.pause_print()
                                self._logger.info("Print paused.")
                                self.telegram_send_with_reply(caption="The print job has been paused.", reply_buttons=0, disable_notification=True)
                                self.current_telegram_message_paused = True
                        elif action_type == "resume":
                            self._logger.info(f"current_telegram_message_paused={self.current_telegram_message_paused}")
                            if self.current_telegram_message_paused:
                                self._logger.info(f"User confirmed to pause the print for message ID: {message_id}")
                                self._logger.info("Resuming print...")
                                self._printer.resume_print()
                                self._logger.info("Print resumed.")
                                self.telegram_send_with_reply(caption="The print job has been resumed.", reply_buttons=0, disable_notification=True)
                                self.current_telegram_message_paused = False
                        elif action_type == "stop" and (message_id in self.current_telegram_message_set):
                            self._logger.info(f"User confirmed to stop the print for message ID: {message_id}")
                            self._logger.info("Stopping print...")
                            self._printer.cancel_print()
                            self._logger.info("Print stopped.")
                            self.telegram_send_with_reply(caption="The print job has been stopped.", reply_buttons=0, disable_notification=True)
                        self.telegram_pending_action = None
                    else:
                        self.telegram_send_with_reply(caption="You have to response within 60 seconds.", reply_buttons=0, disable_notification=True)
                        self.telegram_pending_action = None
                else:
                    self._logger.info(f"User clicked 'Yes' button for message ID: {message_id}, telegram_pending_action={self.telegram_pending_action}")
            elif call.data == "no":
                if self.telegram_pending_action:
                    self._logger.info(f"User canceled the pending action for message ID: {message_id}")
                    self.telegram_send_with_reply(caption="Never Mind.", reply_buttons=0, disable_notification=True)
                    self.telegram_pending_action = None
                else:
                    self._logger.info(f"User clicked 'No' button for message ID: {message_id}, telegram_pending_action={self.telegram_pending_action}")
            elif call.data == "check":
                self._logger.info(f"User clicked 'Check' button for message ID: {message_id}")
                # Use the get_snapshot method to get the processed image
                input_unmasked_image = self.get_snapshot()
                title, state, progress, nozzle_temp, bed_temp, file_metadata = self.get_printer_status()
                status_message = f"Printer: {title}\nStatus: {state}\nProgress: {progress}\nNozzle Temp: {nozzle_temp}°C\nBed Temp: {bed_temp}°C"
                if file_metadata:
                    status_message += f"\nFile: {file_metadata.get('name', 'Unknown')}"
                if input_unmasked_image:
                        self.telegram_send_with_reply(image=input_unmasked_image, caption=status_message, reply_buttons=4, disable_notification=True)
                else:
                    status_message += "\nNo camera connected."
                    self.telegram_send_with_reply(caption=status_message, reply_buttons=0, disable_notification=True)
            elif call.data == "mute":
                if self.current_telegram_message_mute:
                    self.current_telegram_message_mute = False
                    self._logger.info(f"User clicked 'Unmute' button for message ID: {message_id}")
                    self.telegram_send_with_reply(caption="Telegram Notification is unmuted.", reply_buttons=0, disable_notification=True)
                else:
                    self.current_telegram_message_mute = True
                    self._logger.info(f"User clicked 'Mute' button for message ID: {message_id}")
                    self.telegram_send_with_reply(caption="Telegram Notification is muted. Send /hi or click Check button from previous messages to manully see the current camera view", reply_buttons=0, disable_notification=True)
            elif call.data == "pause":
                if not self.current_telegram_message_paused:
                    if message_id in self.current_telegram_message_set:
                        self._logger.info(f"User clicked 'Pause' button for message ID: {message_id}")
                        self.telegram_pending_action = (time.time(), "pause")
                        self.telegram_send_with_reply(caption="Are you sure you want to pause the print job?", reply_buttons=2, disable_notification=True)
                    else:
                        self.telegram_send_with_reply(caption="There is no active print job.", reply_buttons=0, disable_notification=True)
                else:
                    self.telegram_pending_action = (time.time(), "resume")
                    self.telegram_send_with_reply(caption="Are you sure you want to resume the print job?", reply_buttons=2, disable_notification=True)

            elif call.data == "stop":
                if message_id in self.current_telegram_message_set:
                    self._logger.info(f"User clicked 'Stop' button for message ID: {message_id}")
                    self.telegram_pending_action = (time.time(), "stop")
                    self.telegram_send_with_reply(caption="Are you sure you want to stop the print job?", reply_buttons=2, disable_notification=True)
                else:
                    self.telegram_send_with_reply(caption="There is no active print job.", reply_buttons=0, disable_notification=True)
            self.telegram_bot.answer_callback_query(call.id)

    def transform_image(self, img, must_flip_h, must_flip_v, must_rotate):
        # Only call Pillow if we need to transpose anything
        if must_flip_h or must_flip_v or must_rotate:
            self._logger.info(
                "Transformations : FlipH={}, FlipV={} Rotate={}".format(must_flip_h, must_flip_v, must_rotate))

            if must_flip_h:
                img = img.transpose(Image.FLIP_LEFT_RIGHT)
            if must_flip_v:
                img = img.transpose(Image.FLIP_TOP_BOTTOM)
            if must_rotate:
                img = img.rotate(90, expand=True)
        return img

    def get_snapshot(self):
        if self.custom_snapshot_url:
            self._logger.info("Using custom URL")
            try:
                if self.custom_snapshot_url.startswith("file://"):
                    # Handle local file paths
                    file_path = self.custom_snapshot_url.partition('file://')[2]
                    with open(file_path, "rb") as file:
                        img = Image.open(file)
                        return img.copy()
                    
                else:
                    # Handle HTTP URLs
                    if "/?action=stream" in self.custom_snapshot_url:
                        self.custom_snapshot_url = self.custom_snapshot_url.replace("/?action=stream", "/?action=snapshot")
                        self._logger.info(f"self.custom_snapshot_url has been changed to {self.custom_snapshot_url}")
                    response = requests.get(self.custom_snapshot_url)
                    response.raise_for_status()  # This will throw an error for bad responses
                    img = Image.open(BytesIO(response.content))
                    return img
            except requests.RequestException as e:
                self._logger.error(f"Failed to fetch custom snapshot URL: {e}")
                return None
            except IOError as e:
                self._logger.error(f"Failed to open local file from custom snapshot URL: {e}")
                return None

        if self.snap_new_method:
            for camera in self.cameras:
                configs = camera.get_webcam_configurations()
                for config in configs:
                    try:
                        snapshot_iter = camera.take_webcam_snapshot(config)
                        snapshot = b''
                        for b in snapshot_iter:
                            snapshot += b

                        must_flip_h = config.flipH
                        must_flip_v = config.flipV
                        must_rotate = config.rotate90

                        # Create an Image object from the snapshot bytes
                        img = Image.open(BytesIO(snapshot))
                        img = self.transform_image(img, must_flip_h, must_flip_v, must_rotate)
                        #self._logger.info(config)
                        return img
                    except Exception as e:
                        self._logger.error(f"Error processing camera snapshot: {e}")
                        pass
        
        self._logger.info("Falling back to default snapshot method")
        snapshot = None
        snapshot_url = self._settings.global_get(["webcam", "snapshot"])
        if not snapshot_url:
            self._logger.error("No snapshot URL configured")
            return None

        try:
            if snapshot_url.startswith("file://"):
                # Handling local file paths
                file_path = snapshot_url.partition('file://')[2]
                with open(file_path, "rb") as file:
                    img = Image.open(file)
            else:
                # Handling URLs
                response = requests.get(snapshot_url)
                response.raise_for_status()
                img = Image.open(BytesIO(response.content))
            
            # Apply transformations if needed
            must_flip_h = self._settings.global_get_boolean(["webcam", "flipH"])
            must_flip_v = self._settings.global_get_boolean(["webcam", "flipV"])
            must_rotate = self._settings.global_get_boolean(["webcam", "rotate90"])
            img = self.transform_image(img, must_flip_h, must_flip_v, must_rotate)  # Note: This line might need adjustment
            return img
        except requests.RequestException as e:
            self._logger.error(f"Failed to fetch default snapshot: {e}")
            return None
        except IOError as e:
            self._logger.error(f"Failed to open local snapshot file: {e}")
            return None
    
    def check_response(self, base64EncodedImage):
        """
        Helper method to construct a JSON response for checking the AI processing status.

        Parameters:
        - base64EncodedImage: The base64 encoded image to be included in the response.

        Returns:
        - Flask.Response: JSON response containing the image and additional status information.
        """
        failure_count = 0
        with self.lock:
            failure_count = self.count
        response_data  = {
                "image": base64EncodedImage,  
                "failureCount": failure_count,  
                "aiStatus": "ON" if self.enable_AI and self.ai_running else "OFF",
                "telegramStatus": "ON" if self.telegram_server_running else "OFF",
                "cpuTemperature": int(self.get_cpu_temperature())
            }
        return Response(json.dumps(response_data), mimetype="application/json")
    
    @octoprint.plugin.BlueprintPlugin.route("/check", methods=["GET"])
    def check(self):
        """
        Endpoint to check the current status of the AI processing and
        return the latest processed image or camera snapshot.

        Returns:
        - Flask.Response: JSON response containing the image data and additional information.
        """
        
        with self.lock:
            #within 5 seconds, show the failure image.
            if self.ai_results and (time.time() - self.ai_results[-1]['time']) <= 5:
                ai_result_image = self.ai_results[-1]['ai_result_image']  
            else:
                ai_result_image = None 

        if ai_result_image:
            return self.check_response(ai_result_image)

        # Use the get_snapshot method to get the processed image
        input_unmasked_image = self.get_snapshot()
        if input_unmasked_image is None:
            return self.check_response(self._encode_no_camera_image())

        input_image = self.apply_mask_to_image(input_unmasked_image)
        # Since get_snapshot returns the image data as bytes, we can directly use it
        encoded_image = self.encode_image_to_base64(input_image)
        return self.check_response(encoded_image)


    def get_template_configs(self):
        return [
            dict(type="tab", custom_bindings=False)
        ]

    def get_assets(self):
        return dict(js=["js/pinozcam.js"],
                    css=["css/pinozcam.css"],
                )
        
    def get_update_information(self, *args, **kwargs):
        return dict(
            pinozcam=dict(
                displayName="PiNozCam",
                displayVersion=self._plugin_version,
                type="github_release",
                current=self._plugin_version,
                user="DrAlexLiu",
                repo="OctoPrint-PiNozCam",
                # update method: pip
                pip="https://github.com/DrAlexLiu/OctoPrint-PiNozCam/archive/{target}.zip"
            )
        )
    
    def is_blueprint_csrf_protected(self):
        return True

__plugin_name__ = "PiNozCam"
__plugin_pythoncompat__ = ">=3.7,<4"
#__plugin_implementation__ = PinozcamPlugin()

def __plugin_load__():
	global __plugin_implementation__
	plugin = PinozcamPlugin()
	__plugin_implementation__ = plugin

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.plugin.softwareupdate.check_config": plugin.get_update_information,
	}
