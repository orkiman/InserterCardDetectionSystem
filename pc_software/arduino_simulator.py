import flet as ft
import serial
import threading
import time
import random

class ArduinoSimulator:
    def __init__(self):
        # Configuration
        self.cfg_floor_value = 100
        self.cfg_card_threshold = 150
        self.cfg_reverse_sensor = False

        # State
        self.running = True
        self.machine_stop_active = False
        self.max_peak_in_window = 0
        self.state_idle = True

        # Manual controls
        self.manual_adc = 100
        self.envelope_present = False

        # Virtual serial port (using com0com or similar)
        self.port = None
        self.port_name = ""

    def send_telemetry(self):
        """Send data in Arduino format: D:ADC,envelope,stop"""
        if self.port and self.port.is_open:
            try:
                # Apply reversal if configured
                adc_value = self.manual_adc
                if self.cfg_reverse_sensor:
                    adc_value = 1023 - adc_value

                envelope_val = 1 if self.envelope_present else 0
                stop_val = 1 if self.machine_stop_active else 0
                msg = f"D:{adc_value},{envelope_val},{stop_val}\n"
                self.port.write(msg.encode())
            except Exception as e:
                print(f"Send error: {e}")

    def send_message(self, msg):
        """Send status/event messages"""
        if self.port and self.port.is_open:
            try:
                self.port.write(f"{msg}\n".encode())
                print(f"Sent: {msg}")  # Debug output
            except Exception as e:
                print(f"Send message error: {e}")

    def process_command(self, cmd):
        """Process commands from PC"""
        cmd = cmd.strip()

        if cmd == "PING":
            return

        if cmd == "RESUME":
            self.machine_stop_active = False
            self.state_idle = True
            self.max_peak_in_window = 0
            self.send_message("MSG:System Resumed")
            return

        if cmd.startswith("SET_THR:"):
            val = int(cmd.split(":")[1])
            if 0 < val <= 1023:
                self.cfg_card_threshold = val
                self.send_message(f"MSG:Card Threshold Set to {val}")

        if cmd.startswith("SET_FLOOR:"):
            val = int(cmd.split(":")[1])
            if 0 <= val <= 1023:
                self.cfg_floor_value = val
                self.send_message(f"MSG:Floor Value Set to {val}")

        if cmd.startswith("SET_REVERSE:"):
            val = int(cmd.split(":")[1])
            self.cfg_reverse_sensor = (val == 1)
            status = "Enabled" if self.cfg_reverse_sensor else "Disabled"
            self.send_message(f"MSG:Reverse Sensor {status}")

    def simulate_logic(self):
        """Simulate Arduino state machine logic"""
        # Check sensor range (50-1000 absolute range, only trigger once)
        if self.manual_adc < 50 or self.manual_adc > 1000:
            if not self.machine_stop_active:
                self.machine_stop_active = True
                self.send_message("LOG:ERR:SENSOR_OUT_OF_RANGE")
                self.send_message("ERR:SENSOR_OUT_OF_RANGE")
            return

        # State machine
        if self.state_idle and self.envelope_present:
            # Transition to measuring
            self.state_idle = False
            self.max_peak_in_window = 0

        if not self.state_idle and self.envelope_present:
            # Track peak
            if self.manual_adc > self.max_peak_in_window:
                self.max_peak_in_window = self.manual_adc

        if not self.state_idle and not self.envelope_present:
            # Envelope finished, validate
            if self.max_peak_in_window >= self.cfg_card_threshold:
                self.send_message("EVT:PASS")
            else:
                self.machine_stop_active = True
                self.send_message("LOG:ERR:EMPTY_ENVELOPE")
                self.send_message("ERR:EMPTY_ENVELOPE")

            self.state_idle = True
            self.max_peak_in_window = 0

    def serial_thread(self, status_callback):
        """Handle serial communication"""
        last_telemetry = 0
        last_logic = 0

        while self.running:
            current_time = time.time()

            # Read commands from serial
            if self.port and self.port.is_open:
                try:
                    if self.port.in_waiting:
                        line = self.port.readline().decode('utf-8', errors='ignore')
                        self.process_command(line)
                except Exception as e:
                    print(f"Read error: {e}")
                    status_callback("Disconnected")
                    if self.port:
                        self.port.close()
                    self.port = None

            # Send telemetry every 100ms (10Hz like Arduino)
            if current_time - last_telemetry >= 0.1:
                self.send_telemetry()
                last_telemetry = current_time

            # Run logic every 50ms
            if current_time - last_logic >= 0.05:
                self.simulate_logic()
                last_logic = current_time

            time.sleep(0.01)

    def connect(self, port_name, status_callback):
        """Connect to virtual serial port"""
        try:
            if self.port and self.port.is_open:
                self.port.close()

            self.port = serial.Serial(port_name, 115200, timeout=0.1)
            self.port_name = port_name

            # Flush buffers and discard any pending data
            self.port.reset_input_buffer()
            self.port.reset_output_buffer()

            # Read and discard any stale data
            discard_until = time.time() + 0.2
            while time.time() < discard_until:
                if self.port.in_waiting:
                    self.port.read(self.port.in_waiting)
                time.sleep(0.01)

            status_callback(f"Connected to {port_name}")
            self.send_message("MSG:System Booted")

            # Send initial configuration acknowledgment
            time.sleep(0.1)
            self.send_message(f"MSG:Floor Value Set to {self.cfg_floor_value}")
            self.send_message(f"MSG:Card Threshold Set to {self.cfg_card_threshold}")

            return True
        except Exception as e:
            status_callback(f"Error: {str(e)}")
            return False

    def disconnect(self):
        """Disconnect serial port"""
        self.running = False
        if self.port and self.port.is_open:
            self.port.close()


def main(page: ft.Page):
    page.title = "Arduino Card Detector Simulator"
    page.theme_mode = ft.ThemeMode.DARK
    page.window_width = 500
    page.window_height = 700

    sim = ArduinoSimulator()

    # UI Elements
    status_text = ft.Text("Not Connected", size=16, color=ft.Colors.RED)

    # Port selection
    port_input = ft.TextField(
        label="Virtual COM Port",
        value="COM3",
        width=150,
        hint_text="e.g., COM3"
    )

    def update_status(msg):
        status_text.value = msg
        if "Connected" in msg:
            status_text.color = ft.Colors.GREEN
        else:
            status_text.color = ft.Colors.RED
        status_text.update()

    def connect_clicked(e):
        if btn_connect.content.value == "Connect":
            if sim.connect(port_input.value, update_status):
                btn_connect.content.value = "Disconnect"
                btn_connect.bgcolor = ft.Colors.RED
                # Start serial thread
                threading.Thread(target=sim.serial_thread, args=(update_status,), daemon=True).start()
        else:
            sim.disconnect()
            btn_connect.content.value = "Connect"
            btn_connect.bgcolor = ft.Colors.GREEN
            update_status("Disconnected")
        btn_connect.update()

    btn_connect = ft.Button(
        content=ft.Text("Connect"),
        on_click=connect_clicked,
        bgcolor=ft.Colors.GREEN
    )

    # ADC Value slider
    lbl_adc_value = ft.Text("ADC Value: 100", size=20, weight=ft.FontWeight.BOLD)

    def adc_changed(e):
        sim.manual_adc = int(slider_adc.value)
        lbl_adc_value.value = f"ADC Value: {sim.manual_adc}"
        lbl_adc_value.update()

    slider_adc = ft.Slider(
        min=0,
        max=1023,
        divisions=1023,
        value=100,
        label="{value}",
        on_change=adc_changed
    )

    # Quick set buttons
    def set_floor(e):
        slider_adc.value = sim.cfg_floor_value
        adc_changed(e)
        slider_adc.update()

    def set_with_card(e):
        slider_adc.value = sim.cfg_card_threshold + 50
        adc_changed(e)
        slider_adc.update()

    def set_empty(e):
        slider_adc.value = sim.cfg_card_threshold - 20
        adc_changed(e)
        slider_adc.update()

    # Envelope checkbox
    def envelope_changed(e):
        sim.envelope_present = chk_envelope.value

    chk_envelope = ft.Checkbox(
        label="Envelope Present (Active)",
        value=False,
        on_change=envelope_changed
    )

    # Output indicators
    ind_stop = ft.Container(
        content=ft.Text("STOP OUTPUT", color=ft.Colors.WHITE, size=12),
        bgcolor=ft.Colors.GREEN,
        padding=10,
        border_radius=5,
        width=150,
        alignment=ft.Alignment(0, 0)
    )

    # Update indicators thread
    def update_indicators():
        while sim.running:
            if sim.machine_stop_active:
                ind_stop.bgcolor = ft.Colors.RED
            else:
                ind_stop.bgcolor = ft.Colors.GREEN

            try:
                ind_stop.update()
            except:
                pass

            time.sleep(0.1)

    threading.Thread(target=update_indicators, daemon=True).start()

    # Configuration display
    lbl_config = ft.Text(
        f"Floor: {sim.cfg_floor_value} | Threshold: {sim.cfg_card_threshold}",
        size=12,
        color=ft.Colors.GREY_500
    )

    def update_config():
        while sim.running:
            reverse_str = "ON" if sim.cfg_reverse_sensor else "OFF"
            lbl_config.value = f"Floor: {sim.cfg_floor_value} | Threshold: {sim.cfg_card_threshold} | Reverse: {reverse_str}"
            try:
                lbl_config.update()
            except:
                pass
            time.sleep(0.5)

    threading.Thread(target=update_config, daemon=True).start()

    # State display
    lbl_state = ft.Text("State: IDLE", size=14, color=ft.Colors.CYAN)
    lbl_peak = ft.Text("Peak in Window: 0", size=12, color=ft.Colors.GREY_400)

    def update_state_display():
        while sim.running:
            state_str = "IDLE" if sim.state_idle else "MEASURING"
            if sim.machine_stop_active:
                state_str = "FAULT"
            lbl_state.value = f"State: {state_str}"
            lbl_peak.value = f"Peak in Window: {sim.max_peak_in_window}"

            try:
                lbl_state.update()
                lbl_peak.update()
            except:
                pass

            time.sleep(0.1)

    threading.Thread(target=update_state_display, daemon=True).start()

    # Layout
    page.add(
        ft.Container(
            content=ft.Column([
                ft.Text("Arduino Simulator", size=24, weight=ft.FontWeight.BOLD),
                ft.Divider(),

                # Connection
                ft.Row([port_input, btn_connect]),
                status_text,
                ft.Divider(),

                # Manual Controls
                ft.Text("Manual Controls", size=18, weight=ft.FontWeight.BOLD),
                lbl_adc_value,
                slider_adc,
                ft.Row([
                    ft.Button("Set to Floor", on_click=set_floor, bgcolor=ft.Colors.BLUE_700),
                    ft.Button("With Card", on_click=set_with_card, bgcolor=ft.Colors.GREEN_700),
                    ft.Button("Empty", on_click=set_empty, bgcolor=ft.Colors.ORANGE_700),
                ]),
                ft.Container(height=10),
                chk_envelope,

                ft.Divider(),

                # State Display
                ft.Text("System State", size=18, weight=ft.FontWeight.BOLD),
                lbl_state,
                lbl_peak,
                lbl_config,

                ft.Divider(),

                # Outputs
                ft.Text("Outputs", size=18, weight=ft.FontWeight.BOLD),
                ind_stop,

            ], spacing=10),
            padding=20
        )
    )

ft.run(main)
