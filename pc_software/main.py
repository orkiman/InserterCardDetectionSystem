import flet as ft
import flet_charts as fc
import serial
import serial.tools.list_ports
import threading
import time
import json
import os
from datetime import datetime

# --- CONFIGURATION & STATE ---
CONFIG_FILE = "config.json"
ERROR_LOG_FILE = "error_log.txt"
DEFAULT_CONFIG = {
    "serial_port": "",
    "baud_rate": 115200,
    "floor_value": 100,
    "factor": 0.01,
    "envelope_card_threshold": 150,
    "reverse_sensor": False,
    "system_override": False,
    "log_level": "warn",  # "info" = log all, "warn" = log errors only
    "total_count": 0  # Persistent total envelope count
}

# PubSub Topics
TOPIC_STATUS = "status"
TOPIC_DATA = "data"
TOPIC_EVENT = "event"
TOPIC_ERROR_HISTORY = "error_history"
TOPIC_COUNTERS = "counters"


class AppState:
    def __init__(self):
        self.config = self.load_config()
        self.connected = False
        self.raw_val = 0
        self.mm_val = 0.0
        self.envelope_active = False
        self.stop_active = False
        self.last_event = "System Ready"
        self.last_error = ""
        self.error_history = []
        self.max_error_history = 10
        self.graph_points = []
        self.max_graph_points = 50
        self.floor_error = False
        self.graph_min = 0
        self.graph_max = 1023
        # Counters
        self.session_count = 0
        self.last_max_value = 0

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    loaded_config = json.load(f)
                    if "cal_factor" in loaded_config or "cal_offset" in loaded_config:
                        new_config = DEFAULT_CONFIG.copy()
                        new_config["serial_port"] = loaded_config.get("serial_port", "")
                        new_config["baud_rate"] = loaded_config.get("baud_rate", 115200)
                        if "cal_factor" in loaded_config:
                            new_config["factor"] = loaded_config["cal_factor"]
                        return new_config
                    config = DEFAULT_CONFIG.copy()
                    config.update(loaded_config)
                    return config
            except:
                pass
        return DEFAULT_CONFIG.copy()

    def save_config(self):
        with open(CONFIG_FILE, 'w') as f:
            json.dump(self.config, f, indent=4)

    def log_error(self, error_msg, max_val=0):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total = self.config.get("total_count", 0)
        log_msg = f"#{total} {error_msg} (max={max_val})" if max_val else f"#{total} {error_msg}"
        self.error_history.insert(0, (timestamp, log_msg, "error"))
        if len(self.error_history) > self.max_error_history:
            self.error_history.pop()
        try:
            with open(ERROR_LOG_FILE, 'a') as f:
                f.write(f"[{timestamp}] #{total} ERROR: {error_msg} (max={max_val})\n")
        except:
            pass

    def log_pass(self, max_val, override=False):
        # Only log if log_level is "info"
        if self.config.get("log_level", "warn") != "info":
            return
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total = self.config.get("total_count", 0)
        status = "PASS_OVERRIDE" if override else "PASS"
        log_msg = f"#{total} {status} (max={max_val})"
        self.error_history.insert(0, (timestamp, log_msg, "info"))
        if len(self.error_history) > self.max_error_history:
            self.error_history.pop()
        try:
            with open(ERROR_LOG_FILE, 'a') as f:
                f.write(f"[{timestamp}] #{total} INFO: {status} (max={max_val})\n")
        except:
            pass

    def increment_counters(self):
        self.session_count += 1
        self.config["total_count"] = self.config.get("total_count", 0) + 1
        self.save_config()

    def get_mm(self, raw_adc):
        if raw_adc < 50 or raw_adc > 1000:
            self.floor_error = True
            return 0.0
        else:
            self.floor_error = False
        return (raw_adc - self.config["floor_value"]) * self.config["factor"]


state = AppState()
serial_lock = threading.Lock()
ser = None


def serial_handler(page: ft.Page):
    global ser
    last_ping = 0
    while True:
        with serial_lock:
            if state.config["serial_port"] and not state.connected:
                try:
                    ser = serial.Serial(state.config["serial_port"], state.config["baud_rate"], timeout=1)
                    ser.dtr = False
                    time.sleep(0.1)
                    ser.dtr = True
                    time.sleep(2.0)
                    # Aggressive buffer clearing - read and discard all pending data
                    ser.reset_input_buffer()
                    ser.reset_output_buffer()
                    time.sleep(0.1)
                    while ser.in_waiting:
                        ser.read(ser.in_waiting)
                        time.sleep(0.05)
                    ser.reset_input_buffer()
                    state.graph_points.clear()
                    state.graph_min = 0
                    state.graph_max = 1023
                    state.connected = True
                    page.pubsub.send_all_on_topic(TOPIC_STATUS, None)
                    ser.write(f"SET_FLOOR:{state.config['floor_value']}\n".encode())
                    time.sleep(0.1)
                    ser.write(f"SET_THR:{state.config['envelope_card_threshold']}\n".encode())
                    time.sleep(0.1)
                    reverse_val = 1 if state.config.get('reverse_sensor', False) else 0
                    ser.write(f"SET_REVERSE:{reverse_val}\n".encode())
                    time.sleep(0.1)
                    override_val = 1 if state.config.get('system_override', False) else 0
                    ser.write(f"SET_OVERRIDE:{override_val}\n".encode())
                except Exception:
                    time.sleep(2)

        if state.connected and ser and ser.is_open:
            try:
                if ser.in_waiting:
                    line = ser.readline().decode('utf-8', errors='ignore').strip()
                    if line.startswith("D:"):
                        parts = line.split(":")[1].split(",")
                        if len(parts) >= 3:
                            state.raw_val = int(parts[0])
                            state.mm_val = state.get_mm(state.raw_val)
                            state.envelope_active = (parts[1] == "1")
                            state.stop_active = (parts[2] == "1")
                            if len(state.graph_points) > 0:
                                raw_values = [p.y for p in state.graph_points]
                                state.graph_min = min(raw_values + [state.raw_val])
                                state.graph_max = max(raw_values + [state.raw_val])
                            state.graph_points.append(fc.LineChartDataPoint(len(state.graph_points), state.raw_val))
                            if len(state.graph_points) > state.max_graph_points:
                                state.graph_points.pop(0)
                                for i, p in enumerate(state.graph_points):
                                    p.x = i
                            page.pubsub.send_all_on_topic(TOPIC_DATA, None)
                    elif line.startswith("EVT:PASS:"):
                        # Format: EVT:PASS:maxValue
                        parts = line.split(":")
                        max_val = int(parts[2]) if len(parts) > 2 else 0
                        state.last_max_value = max_val
                        state.last_event = f"PASS OK (max={max_val})"
                        state.increment_counters()
                        state.log_pass(max_val, override=False)
                        page.pubsub.send_all_on_topic(TOPIC_EVENT, None)
                        page.pubsub.send_all_on_topic(TOPIC_COUNTERS, None)
                        page.pubsub.send_all_on_topic(TOPIC_ERROR_HISTORY, None)
                    elif line.startswith("EVT:PASS_OVERRIDE:"):
                        # Format: EVT:PASS_OVERRIDE:maxValue
                        parts = line.split(":")
                        max_val = int(parts[2]) if len(parts) > 2 else 0
                        state.last_max_value = max_val
                        state.last_event = f"PASS OVERRIDE (max={max_val})"
                        state.increment_counters()
                        state.log_pass(max_val, override=True)
                        page.pubsub.send_all_on_topic(TOPIC_EVENT, None)
                        page.pubsub.send_all_on_topic(TOPIC_COUNTERS, None)
                        page.pubsub.send_all_on_topic(TOPIC_ERROR_HISTORY, None)
                    elif line.startswith("ERR:"):
                        # Format: ERR:ERROR_TYPE:maxValue or ERR:ERROR_TYPE
                        parts = line.split(":")
                        error_type = parts[1] if len(parts) > 1 else "UNKNOWN"
                        max_val = int(parts[2]) if len(parts) > 2 else 0
                        state.last_max_value = max_val
                        state.last_error = f"STOP: {error_type} (max={max_val})"
                        state.last_event = state.last_error
                        state.stop_active = True
                        state.increment_counters()  # Count every envelope (including errors)
                        state.log_error(error_type, max_val)
                        page.pubsub.send_all_on_topic(TOPIC_EVENT, None)
                        page.pubsub.send_all_on_topic(TOPIC_COUNTERS, None)
                        page.pubsub.send_all_on_topic(TOPIC_ERROR_HISTORY, None)

                if time.time() - last_ping > 1.0:
                    ser.write(b"PING\n")
                    last_ping = time.time()

            except Exception:
                state.connected = False
                if ser:
                    ser.close()
                page.pubsub.send_all_on_topic(TOPIC_STATUS, None)

        time.sleep(0.05)


def send_command(cmd):
    with serial_lock:
        if state.connected and ser:
            try:
                ser.write(f"{cmd}\n".encode())
            except Exception:
                pass


def main(page: ft.Page):
    page.title = "Card Detector HMI"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 20
    page.window.maximized = True

    # SnackBar for notifications
    snack_bar = ft.SnackBar(content=ft.Text(""))
    page.overlay.append(snack_bar)

    # Status indicators
    status_icon = ft.Icon(ft.Icons.CIRCLE, color=ft.Colors.RED, size=20)
    status_text = ft.Text("Disconnected", color=ft.Colors.RED)

    def update_port(port_name):
        state.config["serial_port"] = port_name
        state.save_config()
        global ser
        if ser:
            ser.close()
        state.connected = False
        page.update()

    def on_port_select(e):
        update_port(e.control.value)

    # Port selection
    port_dropdown = ft.Dropdown(
        width=200,
        hint_text="Select Port",
        options=[],
        on_select=on_port_select
    )

    def refresh_ports(e=None):
        ports = serial.tools.list_ports.comports()
        port_dropdown.options = [ft.DropdownOption(p.device) for p in ports]
        port_dropdown.value = state.config["serial_port"]
        page.update()

    # Main display labels
    lbl_mm = ft.Text("0.00 mm", size=60, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_200)
    lbl_raw = ft.Text("ADC: 0", size=20, color=ft.Colors.GREY_500)
    lbl_error = ft.Text("", size=16, color=ft.Colors.RED, visible=False)
    lbl_event = ft.Text("System Ready", size=25, weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN)

    # Chart
    chart_data = [fc.LineChartData(
        points=state.graph_points,
        stroke_width=3,
        color=ft.Colors.CYAN,
        curved=True,
        rounded_stroke_cap=True,
    )]

    chart = fc.LineChart(
        data_series=chart_data,
        border=ft.Border.all(1, ft.Colors.WHITE10),
        left_axis=fc.ChartAxis(
            labels=[
                fc.ChartAxisLabel(value=0, label=ft.Text("Min")),
                fc.ChartAxisLabel(value=1023, label=ft.Text("Max"))
            ],
            label_size=40,
        ),
        bottom_axis=fc.ChartAxis(label_size=0),
        min_y=0,
        max_y=1023,
        min_x=0,
        max_x=state.max_graph_points,
        expand=True,
    )

    # Resume button
    btn_resume_text = ft.Text("RESUME MACHINE", color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD)

    def on_resume_clicked(_):
        send_command("RESUME")
        state.last_error = ""
        state.stop_active = False
        state.last_event = "System Ready"
        page.pubsub.send_all_on_topic(TOPIC_EVENT, None)

    btn_resume = ft.Container(
        content=btn_resume_text,
        bgcolor=ft.Colors.GREEN,
        padding=ft.Padding.symmetric(horizontal=20, vertical=10),
        border_radius=5,
        on_click=on_resume_clicked,
        ink=True,
    )

    # Override warning label (shown on main screen when override active)
    lbl_override_warning = ft.Container(
        content=ft.Row([
            ft.Icon(ft.Icons.WARNING, color=ft.Colors.WHITE, size=20),
            ft.Text("SYSTEM OVERRIDE ACTIVE", color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD, size=14),
        ], alignment=ft.MainAxisAlignment.CENTER),
        bgcolor=ft.Colors.ORANGE,
        padding=10,
        border_radius=5,
        visible=state.config.get("system_override", False),
    )

    # Counter labels
    lbl_session_count = ft.Text(f"Session: {state.session_count}", size=14, weight=ft.FontWeight.BOLD)
    lbl_total_count = ft.Text(f"Total: {state.config.get('total_count', 0)}", size=14, weight=ft.FontWeight.BOLD)

    def reset_session_count(_):
        state.session_count = 0
        lbl_session_count.value = f"Session: {state.session_count}"
        lbl_session_count.update()

    # Configuration fields
    txt_threshold = ft.TextField(
        label="Envelope + Card Threshold (ADC)",
        value=str(state.config["envelope_card_threshold"]),
        width=250,
        helper="Below this = empty envelope (error)"
    )

    def on_reverse_change(e):
        state.config["reverse_sensor"] = e.control.value
        state.save_config()
        send_command(f"SET_REVERSE:{1 if e.control.value else 0}")

    chk_reverse = ft.Checkbox(
        label="Reverse Sensor Signal (1023 - ADC)",
        value=state.config.get("reverse_sensor", False),
        on_change=on_reverse_change
    )

    def on_override_change(e):
        state.config["system_override"] = e.control.value
        state.save_config()
        send_command(f"SET_OVERRIDE:{1 if e.control.value else 0}")
        # Update override warning visibility
        lbl_override_warning.visible = e.control.value
        lbl_override_warning.update()
        # If enabling override, clear error and resume
        if e.control.value:
            on_resume_clicked(None)

    chk_system_override = ft.Checkbox(
        label="System Override (bypass error detection)",
        value=state.config.get("system_override", False),
        on_change=on_override_change
    )

    # Log level dropdown
    def on_log_level_change(e):
        state.config["log_level"] = e.control.value
        state.save_config()

    drp_log_level = ft.Dropdown(
        label="Log Level",
        width=200,
        value=state.config.get("log_level", "warn"),
        options=[
            ft.DropdownOption(key="warn", text="Errors Only"),
            ft.DropdownOption(key="info", text="All Events"),
        ],
        on_select=on_log_level_change
    )

    txt_floor = ft.TextField(
        label="Floor Value (ADC)",
        value=str(state.config["floor_value"]),
        width=250,
        helper="ADC value when nothing is on sensor"
    )

    txt_factor = ft.TextField(
        label="Conversion Factor",
        value=str(state.config["factor"]),
        width=250,
        helper="Multiply by (ADC - Floor) to get mm"
    )

    lbl_config_status = ft.Text("", color=ft.Colors.GREEN)

    def save_settings(e):
        try:
            floor_val = int(txt_floor.value)
            factor_val = float(txt_factor.value)
            threshold_val = int(txt_threshold.value)

            state.config["floor_value"] = floor_val
            state.config["factor"] = factor_val
            state.config["envelope_card_threshold"] = threshold_val
            state.config["reverse_sensor"] = chk_reverse.value
            state.config["system_override"] = chk_system_override.value
            state.save_config()

            send_command(f"SET_FLOOR:{floor_val}")
            send_command(f"SET_THR:{threshold_val}")
            send_command(f"SET_REVERSE:{1 if chk_reverse.value else 0}")
            send_command(f"SET_OVERRIDE:{1 if chk_system_override.value else 0}")

            lbl_config_status.value = "Settings Saved & Uploaded"
            lbl_config_status.color = ft.Colors.GREEN

            snack_bar.content = ft.Text("Settings Saved & Uploaded")
            snack_bar.open = True
            page.update()
        except ValueError:
            lbl_config_status.value = "ERROR: Invalid input values"
            lbl_config_status.color = ft.Colors.RED
            page.update()

    # Error history display
    error_list = ft.Column([], spacing=5, scroll=ft.ScrollMode.AUTO, height=150)

    def update_error_list():
        error_list.controls.clear()
        if not state.error_history:
            error_list.controls.append(
                ft.Text("No events logged", size=12, color=ft.Colors.GREY_600, italic=True)
            )
        else:
            for entry in state.error_history:
                # Handle both old format (timestamp, msg) and new format (timestamp, msg, type)
                if len(entry) == 3:
                    timestamp, log_msg, log_type = entry
                else:
                    timestamp, log_msg = entry
                    log_type = "error"

                is_error = log_type == "error"
                color = ft.Colors.RED_300 if is_error else ft.Colors.GREEN_300
                bg_color = ft.Colors.RED if is_error else ft.Colors.GREEN

                error_list.controls.append(
                    ft.Container(
                        content=ft.Row([
                            ft.Text(timestamp, size=10, color=ft.Colors.GREY_500, width=140),
                            ft.Text(log_msg, size=11, color=color),
                        ]),
                        padding=5,
                        bgcolor=ft.Colors.with_opacity(0.05, bg_color),
                        border_radius=5,
                    )
                )
        try:
            error_list.update()
        except:
            pass

    update_error_list()

    # Dashboard tab
    tab_dashboard = ft.Container(
        content=ft.Column([
            lbl_override_warning,
            ft.Row([
                ft.Container(
                    content=ft.Column([
                        ft.Text("CURRENT HEIGHT", size=12, color=ft.Colors.GREY_400),
                        lbl_mm,
                        lbl_raw,
                        lbl_error
                    ]),
                    expand=True,
                ),
                ft.Container(
                    content=ft.Column([
                        ft.Text("STATUS", size=12, color=ft.Colors.GREY_400),
                        lbl_event,
                        btn_resume
                    ], horizontal_alignment=ft.CrossAxisAlignment.END),
                )
            ]),
            ft.Container(height=10),
            # Counters row
            ft.Container(
                content=ft.Row([
                    ft.Container(
                        content=ft.Row([
                            ft.Icon(ft.Icons.NUMBERS, size=16, color=ft.Colors.BLUE_300),
                            lbl_session_count,
                            ft.IconButton(ft.Icons.REFRESH, icon_size=14, on_click=reset_session_count, tooltip="Reset session"),
                        ]),
                        bgcolor=ft.Colors.with_opacity(0.1, ft.Colors.BLUE),
                        padding=ft.padding.symmetric(horizontal=10, vertical=5),
                        border_radius=5,
                    ),
                    ft.Container(
                        content=ft.Row([
                            ft.Icon(ft.Icons.INVENTORY, size=16, color=ft.Colors.PURPLE_300),
                            lbl_total_count,
                        ]),
                        bgcolor=ft.Colors.with_opacity(0.1, ft.Colors.PURPLE),
                        padding=ft.padding.symmetric(horizontal=10, vertical=5),
                        border_radius=5,
                    ),
                ], spacing=20),
            ),
            ft.Container(height=10),
            ft.Text("LIVE SENSOR DATA", size=12, weight=ft.FontWeight.BOLD),
            ft.Container(
                content=chart,
                height=250,
                bgcolor=ft.Colors.with_opacity(0.05, ft.Colors.WHITE),
                border_radius=10,
                padding=10
            ),
            ft.Container(height=10),
            ft.Text("EVENT LOG (Last 10)", size=12, weight=ft.FontWeight.BOLD),
            ft.Container(
                content=error_list,
                bgcolor=ft.Colors.with_opacity(0.02, ft.Colors.WHITE),
                border_radius=10,
                padding=10
            )
        ])
    )

    # Settings tab
    tab_settings = ft.Column([
        ft.Text("Connection", size=20, weight=ft.FontWeight.BOLD),
        ft.Row([port_dropdown, ft.IconButton(ft.Icons.REFRESH, on_click=refresh_ports)]),
        ft.Divider(),

        ft.Text("Validation Logic", size=20, weight=ft.FontWeight.BOLD),
        ft.Container(height=10),
        txt_threshold,
        ft.Text("Maximum ADC value for empty envelope detection (triggers error if below)",
                size=12, color=ft.Colors.GREY_500),
        ft.Container(height=15),
        chk_reverse,
        ft.Text("Enable if sensor is installed upside-down (inverts ADC reading)",
                size=12, color=ft.Colors.GREY_500),

        ft.Container(height=20),
        ft.Divider(),

        ft.Text("System Override", size=20, weight=ft.FontWeight.BOLD),
        ft.Container(height=10),
        chk_system_override,
        ft.Text("WARNING: Bypasses all error detection - use with caution!",
                size=12, color=ft.Colors.ORANGE_300),

        ft.Container(height=20),
        ft.Divider(),

        ft.Text("Logging", size=20, weight=ft.FontWeight.BOLD),
        ft.Container(height=10),
        drp_log_level,
        ft.Text("'Errors Only' logs only failures, 'All Events' also logs successful passes",
                size=12, color=ft.Colors.GREY_500),

        ft.Container(height=20),
        ft.Divider(),

        ft.Text("Display Parameters", size=20, weight=ft.FontWeight.BOLD),
        ft.Text("(For visualization only - do not affect validation)",
                size=11, color=ft.Colors.GREY_600, italic=True),
        ft.Container(height=5),
        ft.Container(
            content=ft.Column([
                txt_floor,
                ft.Text("Set the ADC value when sensor reads the base/floor (nothing on it)",
                        size=12, color=ft.Colors.GREY_500),
                ft.Container(height=15),
                txt_factor,
                ft.Text("Manual conversion factor to convert ADC units to mm",
                        size=12, color=ft.Colors.GREY_500),
            ]),
            padding=15,
            border=ft.Border.all(2, ft.Colors.BLUE_GREY_700),
            border_radius=10,
            bgcolor=ft.Colors.with_opacity(0.03, ft.Colors.BLUE_GREY)
        ),

        ft.Container(height=20),
        ft.Button(
            content=ft.Text("Save & Apply Configuration"),
            on_click=save_settings,
            bgcolor=ft.Colors.BLUE
        ),
        lbl_config_status,
    ], scroll=ft.ScrollMode.AUTO)

    # Tabs with TabBar and TabBarView
    tabs = ft.Tabs(
        selected_index=0,
        length=2,
        expand=True,
        content=ft.Column(
            expand=True,
            controls=[
                ft.TabBar(
                    tabs=[
                        ft.Tab(label="Monitor", icon=ft.Icons.DASHBOARD),
                        ft.Tab(label="Configuration", icon=ft.Icons.SETTINGS),
                    ]
                ),
                ft.TabBarView(
                    expand=True,
                    controls=[tab_dashboard, tab_settings],
                ),
            ],
        ),
    )

    # Keyboard shortcut: Space bar to resume
    def on_keyboard(e: ft.KeyboardEvent):
        if e.key == " ":
            on_resume_clicked(None)

    page.on_keyboard_event = on_keyboard

    page.add(
        ft.Row([status_icon, status_text], alignment=ft.MainAxisAlignment.END),
        tabs,
    )

    # PubSub callbacks with topic subscriptions
    def on_status_update(topic, message):
        status_icon.color = ft.Colors.GREEN if state.connected else ft.Colors.RED
        status_text.value = "Connected" if state.connected else "Disconnected"
        status_text.color = ft.Colors.GREEN if state.connected else ft.Colors.RED
        status_icon.update()
        status_text.update()

    def on_data_update(topic, message):
        if state.floor_error:
            lbl_mm.value = "0.00 mm"
            lbl_error.value = "ERROR: Sensor out of range - please adjust"
            lbl_error.visible = True
        else:
            lbl_mm.value = f"{state.mm_val:.2f} mm"
            lbl_error.visible = False

        lbl_raw.value = f"ADC: {state.raw_val}"
        lbl_mm.update()
        lbl_raw.update()
        lbl_error.update()

        if len(state.graph_points) > 0:
            chart.min_y = max(0, state.graph_min - 10)
            chart.max_y = min(1023, state.graph_max + 10)
            chart.left_axis.labels = [
                fc.ChartAxisLabel(value=chart.min_y, label=ft.Text(f"{int(chart.min_y)}")),
                fc.ChartAxisLabel(value=chart.max_y, label=ft.Text(f"{int(chart.max_y)}"))
            ]
        chart.update()

    def on_event_update(topic, message):
        if state.last_error:
            lbl_event.value = state.last_error
            lbl_event.color = ft.Colors.RED
            btn_resume.bgcolor = ft.Colors.RED
        else:
            lbl_event.value = state.last_event
            lbl_event.color = ft.Colors.GREEN
            btn_resume.bgcolor = ft.Colors.GREEN
        page.update()

    def on_error_history_update(topic, message):
        update_error_list()

    def on_counters_update(topic, message):
        lbl_session_count.value = f"Session: {state.session_count}"
        lbl_total_count.value = f"Total: {state.config.get('total_count', 0)}"
        lbl_session_count.update()
        lbl_total_count.update()

    # Subscribe to specific topics
    page.pubsub.subscribe_topic(TOPIC_STATUS, on_status_update)
    page.pubsub.subscribe_topic(TOPIC_DATA, on_data_update)
    page.pubsub.subscribe_topic(TOPIC_EVENT, on_event_update)
    page.pubsub.subscribe_topic(TOPIC_ERROR_HISTORY, on_error_history_update)
    page.pubsub.subscribe_topic(TOPIC_COUNTERS, on_counters_update)

    refresh_ports()
    threading.Thread(target=serial_handler, args=(page,), daemon=True).start()


ft.run(main)
