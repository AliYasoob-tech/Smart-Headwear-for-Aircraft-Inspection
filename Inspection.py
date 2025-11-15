import sys
import time
import logging
import textwrap
import threading
import cv2
import lgpio
import os 
import shutil
import signal 
from PIL import Image, ImageDraw, ImageFont, ImageOps
from flask import Flask, jsonify
from datetime import datetime

sys.path.append("..")
from lib import LCD_2inch4

# --- Configuration ---
FLASK_HOST = '0.0.0.0'
FLASK_PORT = 5000
INPUT_COOLDOWN_TIME = 1.0
USB_DRIVE_PATH = "/media/pi/HP_USB/Videos" 

BUTTON_PINS = {
    "NEXT": 5,
    "PREV": 6,
    "FAIL": 13,
    "PASS": 19,
}
GPIO_CHIP = 0

COLORS = {
    "BACKGROUND": "black",
    "TEXT": "white",
    "HEADER_TEXT": "#39FF14",
    "SUCCESS": "#4CAF50",
    "FAIL": "#F44336",
    "PENDING": "yellow"
}

STATE_START, STATE_PREREQUISITES, STATE_TOOLS, STATE_INSPECTION, STATE_SUMMARY = range(5)

INSPECTION_WORKFLOW = {
    "prerequisites": [
        {"desc": "Aircraft Intake Safety Plugs Removed", "status": "PENDING"},
        {"desc": "Landing Gear Pinned & Cockpit Secured", "status": "PENDING"},
        {"desc": "Master Power OFF & Ground Power Connected", "status": "PENDING"}
    ],
    "tools": ["Borescope Camera", "Digital Multimeter", "Mig-21 Maintenance Manual"],
    "panels": [
        {
            "name": "Engine Fan & Compressor",
            "tasks": [
                {"desc": "Turbine Blades: Inspect for FOD/cracks", "ref_val": "Ref: No nicks > 1mm. Smooth leading edges.", "status": "PENDING"},
                {"desc": "Compressor Vanes: Check for abrasion/chipping", "ref_val": "Ref: Vane surfaces clean, no severe pitting.", "status": "PENDING"}
            ]
        },
        {
            "name": "Fuel & Power System",
            "tasks": [
                {"desc": "Fuel Flow Meter: Verify Zero Reading (Engine OFF)", "ref_val": "Ref: Display shows 0.0 (+/- 0.1) kg/s.", "status": "PENDING"},
                {"desc": "Fuel Pipe Connections: Inspect for leaks/chaffing", "ref_val": "Ref: All joints dry. Lines secured with proper clamps.", "status": "PENDING"},
                {"desc": "Temperature Sensor (EGT): Check wiring harness security", "ref_val": "Ref: Connector fully seated and locked. No frayed wires.", "status": "PENDING"},
                {"desc": "External Control Box: Inspect for tamper or damage ('Do not open box')", "ref_val": "Ref: Sealed and intact. Security wire unbroken.", "status": "PENDING"}
            ]
        },
        {
            "name": "Afterburner Section",
            "tasks": [
                {"desc": "Afterburner Stabilizer Ring: Inspect for warping/deformation", "ref_val": "Ref: Even gap alignment. No visible heat stress.", "status": "PENDING"},
                {"desc": "Afterburner Blades (Nozzle): Check actuator linkage function", "ref_val": "Ref: Smooth, unrestricted movement. Full travel confirmed.", "status": "PENDING"},
                {"desc": "Thrust Sensor (Red Circle): Check for mounting security", "ref_val": "Ref: Sensor rigidly mounted. No excessive vibration play.", "status": "PENDING"}
            ]
        }
    ]
}


app_instance = None

class FlaskServer(threading.Thread):
    def __init__(self, app_ref):
        super().__init__()
        self.app = Flask(__name__)
        self.inspection_app = app_ref
        self._setup_routes()

    def _setup_routes(self):
        
        @self.app.route('/')
        def index():
            return f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <title>MiG-21 Inspection Remote</title>
                <style>
                    body {{ font-family: sans-serif; text-align: center; margin: 20px; background-color: #333; color: white; }}
                    .btn {{ 
                        display: block; width: 80%; padding: 30px 0; margin: 20px auto; 
                        font-size: 24px; font-weight: bold; border: none; border-radius: 10px; 
                        cursor: pointer; box-shadow: 0 4px #999;
                    }}
                    #btn_next {{ background-color: {COLORS['HEADER_TEXT']}; color: black; }}
                    #btn_prev {{ background-color: #007bff; color: white; }}
                    #btn_pass {{ background-color: {COLORS['SUCCESS']}; color: white; }}
                    #btn_fail {{ background-color: {COLORS['FAIL']}; color: white; }}
                </style>
                <script>
                    function sendCommand(cmd) {{
                        fetch('/api/' + cmd)
                        .then(response => response.json())
                        .then(data => console.log('Response:', data))
                        .catch(error => console.error('Error:', error));
                    }}
                </script>
            </head>
            <body>
                <h2>MiG-21 Inspection Control</h2>
                <button id="btn_next" class="btn" onclick="sendCommand('next')">NEXT (S1)</button>
                <button id="btn_prev" class="btn" onclick="sendCommand('prev')">PREV (S2)</button>
                <button id="btn_pass" class="btn" onclick="sendCommand('pass')">PASS (S4)</button>
                <button id="btn_fail" class="btn" onclick="sendCommand('fail')">FAIL (S3)</button>
                <p>Status: Check Pi Display</p>
            </body>
            </html>
            """
        
        @self.app.route('/api/<command>', methods=['GET'])
        def handle_command(command):
            if time.time() < self.inspection_app.last_input_time + INPUT_COOLDOWN_TIME:
                return jsonify({"status": "error", "message": "Input cooldown active"}), 429
                
            if command == 'next':
                self.inspection_app._advance_state(direction=1)
                message = "Advanced to next step."
            elif command == 'prev':
                self.inspection_app._advance_state(direction=-1)
                message = "Reverted to previous step."
            elif command == 'pass':
                self.inspection_app._mark_status("PASS")
                message = "Current step marked PASS."
            elif command == 'fail':
                self.inspection_app._mark_status("FAIL")
                message = "Current step marked FAIL."
            else:
                return jsonify({"status": "error", "message": "Invalid command"}), 400
            
            self.inspection_app.last_input_time = time.time()
            return jsonify({"status": "success", "command": command, "message": message})

    def run(self):
        logging.info(f"Starting Flask server on http://{FLASK_HOST}:{FLASK_PORT}/")
        self.app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False)


class InspectionDisplay:
    def __init__(self):
        logging.info("Initializing system...")

        # GPIO Initialization
        self.gpio_handle = None
        try:
            self.gpio_handle = lgpio.gpiochip_open(GPIO_CHIP)
            for pin in BUTTON_PINS.values():
                lgpio.gpio_claim_input(self.gpio_handle, pin, lgpio.SET_PULL_UP)
            logging.info("GPIO polling configured.")
        except lgpio.error as e:
            logging.error(f"Failed to open GPIO chip: {e}.")

        # Cooldown Timer
        self.last_input_time = time.time() - INPUT_COOLDOWN_TIME

        # Display Setup
        self.disp = LCD_2inch4.LCD_2inch4()
        self.disp.Init()
        self.disp.clear()
        self.width = 320
        self.height = 240
        self.needs_redraw = True

        # Webcam Setup 
        self.cap = cv2.VideoCapture(0)
        time.sleep(1.0)  

        if not self.cap.isOpened():
            logging.error("Could not open USB webcam.")
            sys.exit(1)

        self.frame_width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.frame_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.fps = self.cap.get(cv2.CAP_PROP_FPS)

        if self.frame_width == 0 or self.frame_height == 0:
            self.frame_width, self.frame_height = 640, 480
            logging.warning("Camera returned invalid size. Defaulting to 640x480.")
        if self.fps <= 0 or self.fps > 60:
            self.fps = 20.0
            logging.warning("Camera returned invalid FPS. Defaulting to 20.0.")

        logging.info(f"Camera opened successfully: {self.frame_width}x{self.frame_height} @ {self.fps:.1f}fps")

        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.video_filename = f"inspection_{timestamp}.mp4"

        self.out = cv2.VideoWriter(
            self.video_filename, fourcc, self.fps, (self.frame_width, self.frame_height)
        )

        if not self.out.isOpened():
            logging.error(f"VideoWriter failed to open! Size=({self.frame_width},{self.frame_height}), fps={self.fps}")
            self.is_recording_active = False
        else:
            self.is_recording_active = True
            logging.info(f"VideoWriter opened successfully: {self.video_filename}")

        try:
            font_path = "../Font/Font02.ttf"
            self.font_header = ImageFont.truetype(font_path, 28)
            self.font_body_large = ImageFont.truetype(font_path, 26)
            self.font_body = ImageFont.truetype(font_path, 22)
            self.font_label = ImageFont.truetype(font_path, 20)
        except IOError:
            logging.error("Font files not found in '../Font/'.")
            sys.exit(1)

        self.state = STATE_START
        self.prereq_idx, self.panel_idx, self.task_idx = 0, 0, 0

        self.state_draw_map = {
            STATE_START: self._draw_start_screen,
            STATE_PREREQUISITES: self._draw_prereq_screen,
            STATE_TOOLS: self._draw_tools_screen,
            STATE_INSPECTION: self._draw_inspection_screen,
            STATE_SUMMARY: self._draw_summary_screen,
        }

    def _get_current_step(self):
        if self.state == STATE_PREREQUISITES:
            return INSPECTION_WORKFLOW["prerequisites"][self.prereq_idx]
        elif self.state == STATE_INSPECTION:
            return INSPECTION_WORKFLOW["panels"][self.panel_idx]["tasks"][self.task_idx]
        return None

    def _mark_status(self, status):
        step = self._get_current_step()
        if step and step["status"] == "PENDING":
            step["status"] = status
            self.needs_redraw = True 
            logging.info(f"Step marked: {step['desc']} -> {status}")

    def _advance_state(self, direction=1):
        if self.state == STATE_START and direction == 1:
            self.state = STATE_PREREQUISITES
            self.needs_redraw = True
            return

        elif self.state == STATE_TOOLS:
            if direction == 1:
                self.state = STATE_INSPECTION
            elif direction == -1:
                self.state = STATE_PREREQUISITES
                self.prereq_idx = len(INSPECTION_WORKFLOW["prerequisites"]) - 1
            self.needs_redraw = True
            return

        # --- Handle PREREQUISITES flow ---
        elif self.state == STATE_PREREQUISITES:
            num_prereqs = len(INSPECTION_WORKFLOW["prerequisites"])
            new_idx = self.prereq_idx + direction
            
            if 0 <= new_idx < num_prereqs:
                self.prereq_idx = new_idx
                self.needs_redraw = True
            elif new_idx >= num_prereqs and direction == 1:
                self.state = STATE_TOOLS 
                self.needs_redraw = True
            elif new_idx < 0 and direction == -1:
                self.state = STATE_START 
                self.needs_redraw = True
            return

        # Handle INSPECTION flow 
        elif self.state == STATE_INSPECTION:
            num_panels = len(INSPECTION_WORKFLOW["panels"])
            current_panel = INSPECTION_WORKFLOW["panels"][self.panel_idx]
            num_tasks = len(current_panel["tasks"])
            
            new_task_idx = self.task_idx + direction
            
            if 0 <= new_task_idx < num_tasks:
                self.task_idx = new_task_idx
                self.needs_redraw = True
            
            elif new_task_idx >= num_tasks and direction == 1:
                if self.panel_idx < num_panels - 1:
                    self.panel_idx += 1
                    self.task_idx = 0
                    self.needs_redraw = True
                else:
                    # Inspection Complete -> SUMMARY
                    self.state = STATE_SUMMARY
                    self.needs_redraw = True
                    # Stop recording when inspection is complete
                    if self.is_recording_active:
                        self.out.release()
                        self.is_recording_active = False
                        logging.info("Recording complete. File saved.")

            elif new_task_idx < 0 and direction == -1:
                # Revert to previous panel's last task or TOOLS
                if self.panel_idx > 0:
                    self.panel_idx -= 1
                    # Set index to the last task of the newly selected previous panel
                    self.task_idx = len(INSPECTION_WORKFLOW["panels"][self.panel_idx]["tasks"]) - 1
                    self.needs_redraw = True
                else:
                    # Revert to previous major stage 
                    self.state = STATE_TOOLS 
                    self.needs_redraw = True
            return

        # Handle SUMMARY flow 
        elif self.state == STATE_SUMMARY and direction == -1:
            self.state = STATE_INSPECTION
            self.panel_idx = len(INSPECTION_WORKFLOW["panels"]) - 1
            self.task_idx = len(INSPECTION_WORKFLOW["panels"][self.panel_idx]["tasks"]) - 1
            self.needs_redraw = True
            return

    def _backup_to_usb(self):
        dest_dir = USB_DRIVE_PATH
        source_file = self.video_filename
        
        logging.info(f"Attempting USB backup to: {dest_dir}")

        try:
            os.makedirs(dest_dir, exist_ok=True)
            logging.info(f"Directory check/create successful: {dest_dir}")
        except Exception as e:
            logging.error(f"Failed to create directory {dest_dir}: {e}")
            return 

        test_file_path = os.path.join(dest_dir, "test_write.txt")
        try:
            with open(test_file_path, 'w') as f:
                f.write(f"Write test successful at {datetime.now()}\n")
            logging.info(f"Successfully wrote test file: {test_file_path}")
        except Exception as e:
            logging.error(f"FAILED to write test file: {e}")

        if not os.path.exists(source_file):
            logging.error(f"Local video file not found: {source_file}")
            return
            
        try:
            destination_file = os.path.join(dest_dir, source_file)
            shutil.copy2(source_file, destination_file) # copy2 preserves metadata
            logging.info(f"Successfully copied video to USB drive: {destination_file}")
        except Exception as e:
            logging.error(f"Failed to copy video to USB drive: {e}")

    def _wrap_and_draw_text(self, draw, text, position, font, fill, char_width=20, centered=False):
        lines = textwrap.wrap(text, width=char_width)
        x_start, y = position
        for line in lines:
            if centered:
                text_bbox = draw.textbbox((0,0), line, font=font)
                text_width = text_bbox[2] - text_bbox[0]
                x = (self.width - text_width) / 2
            else:
                x = x_start
                
            draw.text((x, y), line, font=font, fill=fill)
            bbox = draw.textbbox((0,0), line, font=font)
            y += (bbox[3] - bbox[1]) + 2 
        return y
    
    def _draw_header(self, draw, title):
        padding = self.width * 0.05
        draw.text((padding, self.height * 0.05), title, font=self.font_header, fill=COLORS["HEADER_TEXT"])
        draw.line([(0, self.height * 0.2), (self.width, self.height * 0.2)], fill="#333")

    def _draw_start_screen(self, draw):
        self._draw_header(draw, "MiG-21 Engine Check (POC)")

        draw.text((self.width / 2, self.height * 0.35), "INSPECTION SYSTEM READY", font=self.font_body_large, fill=COLORS["SUCCESS"], anchor="mm")
        
        self._wrap_and_draw_text(draw, "Use physical buttons or web interface for control.", (self.width / 2, self.height * 0.4), self.font_label, COLORS["TEXT"], char_width=30, centered=True)
        
        draw.text((self.width / 2, self.height * 0.7), "PRESS NEXT (B1) TO START", font=self.font_body_large, fill=COLORS["HEADER_TEXT"], anchor="mm")
        draw.text((self.width / 2, self.height * 0.9), f"Rec: {self.video_filename}", font=self.font_label, fill=COLORS["PENDING"], anchor="mm")
        
    def _draw_prereq_screen(self, draw):
        prereq = INSPECTION_WORKFLOW["prerequisites"][self.prereq_idx]
        progress = f"({self.prereq_idx + 1}/{len(INSPECTION_WORKFLOW['prerequisites'])})"
        self._draw_header(draw, f"Safety & Pre-Check {progress}")
        status_color = COLORS.get(prereq["status"], COLORS["PENDING"])
        draw.text((self.width * 0.95, self.height * 0.05), prereq["status"], font=self.font_header, fill=status_color, anchor="rt")
        
        self._wrap_and_draw_text(draw, prereq["desc"], (self.width / 2, self.height * 0.4), self.font_body, COLORS["TEXT"], char_width=28, centered=True)

        draw.text((self.width / 2, self.height * 0.9), "PASS (B4) / FAIL (B3) | B1/B2 to Navigate", font=self.font_label, fill=COLORS["HEADER_TEXT"], anchor="mm")
        
    def _draw_tools_screen(self, draw):
        self._draw_header(draw, "Required Equipment Checklist")
        draw.text((self.width / 2, self.height * 0.3), "Confirm ALL tools are ready.", font=self.font_label, fill=COLORS["PENDING"], anchor="mm")
        y = self.height * 0.40 
        for tool in INSPECTION_WORKFLOW["tools"]:
            draw.text((self.width / 2, y), f"{tool}", font=self.font_body_large, fill=COLORS["TEXT"], anchor="mm")
            y += self.height * 0.12 
        draw.text((self.width / 2, self.height * 0.9), "PRESS NEXT (B1) TO CONTINUE", font=self.font_label, fill=COLORS["HEADER_TEXT"], anchor="mm")
        
    def _draw_inspection_screen(self, draw):
        panel = INSPECTION_WORKFLOW["panels"][self.panel_idx]
        task = panel["tasks"][self.task_idx]
        task_progress = f"Task {self.task_idx + 1} of {len(panel['tasks'])}"
        self._draw_header(draw, f"{panel['name']}")
        status_color = COLORS.get(task["status"], COLORS["PENDING"])
        draw.text((self.width * 0.95, self.height * 0.05), task["status"], font=self.font_header, fill=status_color, anchor="rt")
        padding = self.width * 0.05
        draw.text((padding, self.height * 0.23), f"TASK ({task_progress}):", font=self.font_body, fill=COLORS["HEADER_TEXT"])
        
        y_after_task = self._wrap_and_draw_text(draw, task["desc"], (padding, self.height * 0.30), self.font_body, COLORS["TEXT"], char_width=28)

        _, top, _, bottom = draw.textbbox((0, 0), "A", font=self.font_body)
        label_line_height = bottom - top

        reference_y_start = max(y_after_task + 8, self.height * 0.5) 
        draw.text((padding, reference_y_start), "REFERENCE:", font=self.font_body, fill=COLORS["HEADER_TEXT"])
        
        self._wrap_and_draw_text(draw, task["ref_val"], (padding, reference_y_start + label_line_height + 2), self.font_label, COLORS["TEXT"], char_width=28)

        draw.text((self.width / 2, self.height * 0.95), "PASS (B4) / FAIL (B3) | NEXT (B1)", font=self.font_label, fill=COLORS["PENDING"], anchor="mm")
        
    def _draw_summary_screen(self, draw):
        self._draw_header(draw, "Engine Check Complete")
        total_tasks = sum(len(p["tasks"]) for p in INSPECTION_WORKFLOW["panels"]) + len(INSPECTION_WORKFLOW["prerequisites"])
        fail_count = 0
        for p in INSPECTION_WORKFLOW["prerequisites"]:
            if p["status"] == "FAIL": fail_count += 1
        for panel in INSPECTION_WORKFLOW["panels"]:
            for task in panel["tasks"]:
                if task["status"] == "FAIL": fail_count += 1
        pass_count = total_tasks - fail_count
        summary_color = COLORS["SUCCESS"] if fail_count == 0 else COLORS["FAIL"]
        draw.text((self.width / 2, self.height * 0.4), "RECORDING SAVED!", font=self.font_header, fill=summary_color, anchor="mm")
        draw.text((self.width / 2, self.height * 0.6), f"Pass/Fail: {pass_count}/{fail_count}", font=self.font_body_large, fill=COLORS["TEXT"], anchor="mm")
        draw.text((self.width / 2, self.height * 0.8), f"File: {self.video_filename}", font=self.font_label, fill=COLORS["HEADER_TEXT"], anchor="mm")

    def _check_buttons(self):
        if self.gpio_handle is None:
            return
        
        if time.time() < self.last_input_time + INPUT_COOLDOWN_TIME:
            return

        for action, pin in BUTTON_PINS.items():
            if lgpio.gpio_read(self.gpio_handle, pin) == 0:
                while lgpio.gpio_read(self.gpio_handle, pin) == 0:
                    time.sleep(0.01)

                if action == "NEXT":
                    self._advance_state(direction=1)
                elif action == "PREV":
                    self._advance_state(direction=-1)
                elif action == "FAIL":
                    self._mark_status("FAIL")
                elif action == "PASS":
                    self._mark_status("PASS")

                self.last_input_time = time.time()
                self.needs_redraw = True 
                return 

    def _process_and_record_frame(self, frame):
        if self.is_recording_active and self.out.isOpened():
            self.out.write(frame)

        if cv2.waitKey(1) & 0xFF == ord('q'):
            return False
        return True

    def run(self):
        
        while True:
            ret, frame = self.cap.read()
            if not ret:
                logging.error("Failed to read frame from webcam.")
                time.sleep(1)
                continue
            
            if not self._process_and_record_frame(frame):
                break
            
            self._check_buttons()

            if self.needs_redraw:
                self.needs_redraw = False 
                
                image = Image.new("RGB", (self.width, self.height), COLORS["BACKGROUND"])
                draw = ImageDraw.Draw(image)

                self.state_draw_map[self.state](draw) 

                final_image = image
                if self.disp.height > self.disp.width:
                    final_image = image.rotate(270, expand=True)

                mirrored_image = ImageOps.mirror(final_image)
                self.disp.ShowImage(mirrored_image)

        self.cleanup()

    def cleanup(self):
        logging.info("Cleaning up resources...")
        self.cap.release()

        if hasattr(self, "out") and self.out.isOpened():
            self.out.release()
            logging.info(f"Recording finalized: {self.video_filename}")
        else:
            logging.info("Video writer already closed (recording previously completed).")
        self._backup_to_usb()

        try:
            logging.info("Displaying shutdown message...")
            image = Image.new("RGB", (self.width, self.height), "black")
            draw = ImageDraw.Draw(image)
            draw.text(
                (self.width / 2, self.height / 2 - 10),
                "System Shutting Down...",
                font=self.font_body_large,
                fill=(255, 255, 255),
                anchor="mm"
            )
            draw.text(
                (self.width / 2, self.height / 2 + 25),
                "Please wait",
                font=self.font_label,
                fill=(180, 180, 180),
                anchor="mm"
            )
            self.disp.ShowImage(image)
            time.sleep(1.5) 
            blank = Image.new("RGB", (self.width, self.height), "black")

            if self.disp.height > self.disp.width:
                blank = blank.rotate(270, expand=True)
            blank = ImageOps.mirror(blank)

            self.disp.ShowImage(blank)
            time.sleep(0.3)
            self.disp.clear()
            logging.info("Display cleared completely (with orientation correction).")

        except Exception as e:
            logging.warning(f"Failed to clear display: {e}")

        cv2.destroyAllWindows()
        self.disp.module_exit()

        if self.gpio_handle is not None:
            lgpio.gpiochip_close(self.gpio_handle)
            logging.info("GPIO resources released.")
        logging.info("System cleanup complete. Safe to exit now.")


def signal_handler(sig, frame):
    if app_instance:
        logging.info(f"Signal {sig} received. Starting cleanup...")
        app_instance.cleanup()
    else:
        logging.warning("Signal received, but app_instance not ready.")
    sys.exit(0)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    
    # Initialize the main inspection application
    app_instance = InspectionDisplay()
    
    # Register signal handlers after app_instance is created
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Initialize and start the Flask server in a separate thread
    flask_thread = FlaskServer(app_instance)
    flask_thread.daemon = True 
    flask_thread.start()
    
    # Start the main inspection
    try:
        logging.info(f"System ready. Control via physical buttons or mobile at http://<Pi_IP_Address>:{FLASK_PORT}/")
        app_instance.run()
    except Exception as e:
        logging.error(f"Main loop crashed: {e}")
        app_instance.cleanup()
        sys.exit(1)