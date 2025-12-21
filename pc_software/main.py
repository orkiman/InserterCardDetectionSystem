import flet as ft
import serial
import serial.tools.list_ports
import threading
import time
import json
import os

# --- CONFIGURATION & STATE ---
CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "serial_port": "",
    "baud_rate": 115200,
    "cal_factor": 0.05,  # Default: roughly 1 ADC step = 0.05mm
    "cal_offset": 43.0,  # Default: 0 ADC = 43mm (Base)
    "threshold_card": 50,
    "threshold_floor": 30
}

class AppState:
    def __init__(self):
        self.config = self.load_config()
        self.connected = False
        self.raw_val = 0
        self.mm_val = 0.0
        self.envelope_active = False
        self.stop_active = False
        self.last_event = "System Ready"
        self.graph_points = []
        self.max_graph_points = 50

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    return json.load(f)
            except:
                pass
        return DEFAULT_CONFIG.copy()

    def save_config(self):
        with open(CONFIG_FILE, 'w') as f:
            json.dump(self.config, f, indent=4)

    def get_mm(self, raw_adc):
        # Linear Conversion: y = mx + c
        return (raw_adc * self.config["cal_factor"]) + self.config["cal_offset"]

state = AppState()
serial_lock = threading.Lock()
ser = None

# --- SERIAL THREAD ---
def serial_handler(page: ft.Page):
    global ser
    while True:
        with serial_lock:
            if state.config["serial_port"] and not state.connected:
                try:
                    ser = serial.Serial(state.config["serial_port"], state.config["baud_rate"], timeout=1)
                    state.connected = True
                    page.pubsub.send_all("update_status")
                    print(f"Connected to {state.config['serial_port']}")
                except Exception as e:
                    print(f"Connection Error: {e}")
                    time.sleep(2) # Wait before retry

        if state.connected and ser and ser.is_open:
            try:
                # 1. Read Line
                if ser.in_waiting:
                    line = ser.readline().decode('utf-8', errors='ignore').strip()
                    if line.startswith("D:"):
                        # Format: D:512,1,0 (ADC, Env, Stop)
                        parts = line.split(":")[1].split(",")
                        if len(parts) >= 3:
                            state.raw_val = int(parts[0])
                            state.mm_val = state.get_mm(state.raw_val)
                            state.envelope_active = (parts[1] == "1")
                            state.stop_active = (parts[2] == "1")
                            
                            # Update Graph Data
                            state.graph_points.append(ft.LineChartDataPoint(len(state.graph_points), state.raw_val))
                            if len(state.graph_points) > state.max_graph_points:
                                state.graph_points.pop(0)
                                # Re-index x axis to keep graph scrolling smoothly
                                for i, p in enumerate(state.graph_points):
                                    p.x = i
                            
                            page.pubsub.send_all("new_data")

                    elif line.startswith("EVT:PASS"):
                        state.last_event = "PASS OK"
                        page.pubsub.send_all("update_event")
                    elif line.startswith("ERR:"):
                        state.last_event = f"STOP: {line.split(':')[1]}"
                        page.pubsub.send_all("update_event")

                # 2. Heartbeat (Every ~1s)
                # Ideally handled by a separate timer, but simple loop logic here for now
                if int(time.time() * 10) % 10 == 0: # Roughly every second
                     ser.write(b"PING\n")

            except Exception as e:
                print(f"Serial Error: {e}")
                state.connected = False
                if ser: ser.close()
                page.pubsub.send_all("update_status")

        time.sleep(0.05) # 20Hz loop

def send_command(cmd):
    with serial_lock:
        if state.connected and ser:
            try:
                ser.write(f"{cmd}\n".encode())
                print(f"Sent: {cmd}")
            except:
                pass

# --- GUI MAIN ---
def main(page: ft.Page):
    page.title = "Card Detector HMI"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 20
    
    # --- UI ELEMENTS ---
    
    # 1. Status Bar
    status_icon = ft.Icon(ft.Icons.CIRCLE, color=ft.Colors.RED, size=20)
    status_text = ft.Text("Disconnected", color=ft.Colors.RED)
    port_dropdown = ft.Dropdown(
        width=200, 
        hint_text="Select Port",
        options=[],
        on_change=lambda e: update_port(e.control.value)
    )

    def refresh_ports(e=None):
        ports = serial.tools.list_ports.comports()
        port_dropdown.options = [ft.dropdown.Option(p.device) for p in ports]
        port_dropdown.value = state.config["serial_port"]
        page.update()

    def update_port(port_name):
        state.config["serial_port"] = port_name
        state.save_config()
        # Trigger reconnect logic in thread
        global ser
        if ser: ser.close()
        state.connected = False
        page.update()

    # 2. Dashboard Elements
    lbl_mm = ft.Text("0.00 mm", size=60, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_200)
    lbl_raw = ft.Text("ADC: 0", size=20, color=ft.Colors.GREY_500)
    lbl_event = ft.Text("System Ready", size=25, weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN)
    
    chart_data = [ft.LineChartData(
        data_points=state.graph_points,
        stroke_width=3,
        color=ft.Colors.CYAN,
        curved=True,
        stroke_cap_round=True,
    )]
    
    chart = ft.LineChart(
        data_series=chart_data,
        border=ft.border.all(1, ft.Colors.WHITE10),
        left_axis=ft.ChartAxis(
            labels=[ft.ChartAxisLabel(value=0, label=ft.Text("0")), ft.ChartAxisLabel(value=1023, label=ft.Text("1023"))],
            labels_size=40,
        ),
        bottom_axis=ft.ChartAxis(labels_size=0), # Hide X labels
        tooltip_bgcolor=ft.Colors.with_opacity(0.8, ft.Colors.GREY_900),
        min_y=0,
        max_y=1023,
        min_x=0,
        max_x=state.max_graph_points,
        expand=True,
    )

    btn_resume = ft.ElevatedButton("RESUME MACHINE", bgcolor=ft.Colors.RED, color=ft.Colors.WHITE, on_click=lambda _: send_command("RESUME"))

    # 3. Settings / Calibration Elements
    
    # Sliders
    slider_threshold = ft.Slider(min=0, max=500, divisions=500, label="Threshold: {value}", value=state.config["threshold_card"])
    slider_floor = ft.Slider(min=0, max=200, divisions=200, label="Floor: {value}", value=state.config["threshold_floor"])
    
    def save_settings(e):
        state.config["threshold_card"] = int(slider_threshold.value)
        state.config["threshold_floor"] = int(slider_floor.value)
        state.save_config()
        send_command(f"SET_THR:{state.config['threshold_card']}")
        send_command(f"SET_MIN:{state.config['threshold_floor']}")
        page.snack_bar = ft.SnackBar(ft.Text("Settings Saved & Uploaded"))
        page.snack_bar.open = True
        page.update()

    # Calibration Inputs
    txt_factor = ft.TextField(label="Factor (m)", value=str(state.config["cal_factor"]), width=100)
    txt_offset = ft.TextField(label="Offset (c)", value=str(state.config["cal_offset"]), width=100)
    
    # Wizard State
    cal_floor_val = 0
    cal_std_val = 0
    txt_std_thickness = ft.TextField(label="Piece Thickness (mm)", value="5.0", width=150)
    lbl_cal_step = ft.Text("Step 1: Clear Sensor area to measure floor.", color=ft.Colors.YELLOW)

    def run_cal_step1(e):
        # Measure Floor
        if not (30 < state.raw_val < 100):
            lbl_cal_step.value = f"Error: Sensor Value {state.raw_val} out of valid floor range (30-100)!"
            lbl_cal_step.color = ft.Colors.RED
            lbl_cal_step.update()
            return
        
        nonlocal cal_floor_val
        cal_floor_val = state.raw_val
        lbl_cal_step.value = f"Floor Recorded: {cal_floor_val}. Step 2: Place Standard Piece."
        lbl_cal_step.color = ft.Colors.CYAN
        lbl_cal_step.update()
        btn_cal_step2.disabled = False
        btn_cal_step2.update()

    def run_cal_step2(e):
        # Measure Standard
        nonlocal cal_std_val
        cal_std_val = state.raw_val
        
        # Calculate
        try:
            thickness = float(txt_std_thickness.value)
            delta_adc = cal_std_val - cal_floor_val
            
            if delta_adc <= 5: # Too small difference
                 lbl_cal_step.value = "Error: Delta too small. Is piece inserted?"
                 lbl_cal_step.color = ft.Colors.RED
                 lbl_cal_step.update()
                 return

            new_factor = thickness / delta_adc
            # Offset logic: Since y = mx + c. At floor (y=53mm approx? Or relative?)
            # Actually, usually user wants 0mm at floor or specific distance.
            # Let's assume user wants to measure Thickness. So Floor = 0mm thickness?
            # Or Height? Let's assume Height. Floor = 53mm (bottom of range).
            # Let's assume Floor is the MAX distance (53mm) or MIN?
            # Usually Analog: Closer = Higher Voltage. 
            # Let's stick to the prompt: Range 43-53mm.
            # Let's assume Floor (Empty) = 53mm (Furthest). 
            # Insert piece (5mm) -> Height = 48mm.
            
            # Simple approach requested: "Floor value" + "Standard Piece".
            # Let's assume Floor is Reference 0 (or known Envelop Base).
            # Let's set Offset so that Floor Reading = 0.0mm (Thickness Mode)
            # OR Floor Reading = 53.0mm (Absolute Mode).
            # Let's use Absolute Mode as per spec. 
            
            # If Floor = 53mm (Far). Piece (5mm) means Sensor sees 48mm.
            # Voltage/ADC goes UP as object gets CLOSER usually (Sharp sensors).
            # Let's assume: Higher ADC = Closer object = Smaller mm distance?
            # OR Higher ADC = Higher Thickness?
            # Let's trust the "Factor" approach.
            
            # Auto Calc:
            # We want Floor to equal roughly 53mm (or whatever user defines base as).
            # Let's just calculate Factor based on Thickness Delta.
            # Factor = Thickness_mm / (ADC_Piece - ADC_Floor)
            
            state.config["cal_factor"] = round(new_factor, 5)
            # Recalculate offset so that Floor ADC = 0mm (Relative Thickness) or Fixed?
            # Let's set it so current Floor ADC = 0.00mm (Relative to floor)
            state.config["cal_offset"] = -(cal_floor_val * new_factor)
            
            txt_factor.value = str(state.config["cal_factor"])
            txt_offset.value = str(state.config["cal_offset"])
            state.save_config()
            
            lbl_cal_step.value = f"Success! Factor: {new_factor:.4f}. Saved."
            lbl_cal_step.color = ft.Colors.GREEN
            lbl_cal_step.update()
            
        except ValueError:
             lbl_cal_step.value = "Error: Invalid Thickness value."
             lbl_cal_step.update()

    btn_cal_step1 = ft.ElevatedButton("1. Measure Floor", on_click=run_cal_step1)
    btn_cal_step2 = ft.ElevatedButton("2. Measure Piece", on_click=run_cal_step2, disabled=True)

    def save_manual_cal(e):
        try:
            state.config["cal_factor"] = float(txt_factor.value)
            state.config["cal_offset"] = float(txt_offset.value)
            state.save_config()
            page.snack_bar = ft.SnackBar(ft.Text("Calibration Saved"))
            page.snack_bar.open = True
            page.update()
        except:
             pass

    # --- LAYOUT CONSTRUCTION ---
    
    # TAB 1: DASHBOARD
    tab_dashboard = ft.Container(
        content=ft.Column([
            ft.Row([
                ft.Container(
                    content=ft.Column([
                        ft.Text("CURRENT HEIGHT", size=12, color=ft.Colors.GREY_400),
                        lbl_mm,
                        lbl_raw
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
            ft.Container(height=20),
            ft.Text("LIVE SENSOR DATA", size=12, weight=ft.FontWeight.BOLD),
            ft.Container(
                content=chart,
                height=300,
                bgcolor=ft.Colors.with_opacity(0.05, ft.Colors.WHITE),
                border_radius=10,
                padding=10
            )
        ])
    )

    # TAB 2: SETTINGS & CALIBRATION
    tab_settings = ft.Column([
        ft.Text("Connection", size=20, weight=ft.FontWeight.BOLD),
        ft.Row([port_dropdown, ft.IconButton(ft.Icons.REFRESH, on_click=refresh_ports)]),
        
        ft.Divider(),
        
        ft.Text("Safety Thresholds", size=20, weight=ft.FontWeight.BOLD),
        slider_threshold,
        slider_floor,
        ft.ElevatedButton("Apply & Upload Thresholds", on_click=save_settings),
        
        ft.Divider(),
        
        ft.Text("Calibration (ADC to mm)", size=20, weight=ft.FontWeight.BOLD),
        ft.Text("Manual Edit:", color=ft.Colors.GREY),
        ft.Row([txt_factor, txt_offset, ft.ElevatedButton("Save Manual", on_click=save_manual_cal)]),
        
        ft.Container(height=10),
        ft.Text("Calibration Wizard:", color=ft.Colors.GREY),
        ft.Container(
            content=ft.Column([
                lbl_cal_step,
                ft.Row([btn_cal_step1, txt_std_thickness, btn_cal_step2])
            ]),
            bgcolor=ft.Colors.with_opacity(0.1, ft.Colors.BLUE),
            padding=15,
            border_radius=10
        )
    ], scroll=ft.ScrollMode.AUTO)

    # Main Tabs
    t = ft.Tabs(
        selected_index=0,
        animation_duration=300,
        tabs=[
            ft.Tab(text="Monitor", icon=ft.Icons.DASHBOARD, content=tab_dashboard),
            ft.Tab(text="Configuration", icon=ft.Icons.SETTINGS, content=tab_settings),
        ],
        expand=True,
    )

    page.add(
        ft.Row([status_icon, status_text], alignment=ft.MainAxisAlignment.END),
        t
    )
    
    # --- EVENT LISTENERS ---
    def on_status_update(topic, msg):
        status_icon.color = ft.Colors.GREEN if state.connected else ft.Colors.RED
        status_text.value = "Connected" if state.connected else "Disconnected"
        status_text.color = ft.Colors.GREEN if state.connected else ft.Colors.RED
        status_icon.update()
        status_text.update()
        
    def on_data_update(topic, msg):
        lbl_mm.value = f"{state.mm_val:.2f} mm"
        lbl_raw.value = f"ADC: {state.raw_val}"
        lbl_mm.update()
        lbl_raw.update()
        # Chart update is expensive, maybe throttle? Flet handles it okay usually.
        chart.update()
        
    def on_event_update(topic, msg):
        lbl_event.value = state.last_event
        lbl_event.color = ft.Colors.RED if "STOP" in state.last_event else ft.Colors.GREEN
        lbl_event.update()

    page.pubsub.subscribe("update_status", on_status_update)
    page.pubsub.subscribe("new_data", on_data_update)
    page.pubsub.subscribe("update_event", on_event_update)

    # Init
    refresh_ports()
    # Start Serial Thread
    threading.Thread(target=serial_handler, args=(page,), daemon=True).start()

ft.app(target=main)