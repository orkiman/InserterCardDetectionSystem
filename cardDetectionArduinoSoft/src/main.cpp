#include <Arduino.h>

// --- FORWARD DECLARATIONS ---
void validateResult();
void triggerStop(String reason);
void resetSystem();
void processCommand(String cmd);

// --- HARDWARE PIN DEFINITIONS ---
const int PIN_SENSOR      = A0;  // Analog Height Sensor
const int PIN_ENVELOPE    = 2;   // Digital Input: Envelope Present Signal
const int PIN_ENABLE_OUT  = 8;   // Digital Output: Machine Enable Signal

// --- CONFIGURATION CONSTANTS (DEFAULTS) ---
// These can be updated via Serial commands
int CFG_FLOOR_VALUE       = 100; // Floor ADC value (50-500 valid range)
int CFG_CARD_THRESHOLD    = 150; // Below this = empty envelope (error)
int CFG_CARD_UPPER_THRESHOLD = 800; // Above this = double card (error)
bool CFG_REVERSE_SENSOR   = false; // Reverse sensor signal (1023 - ADC)
bool CFG_SYSTEM_OVERRIDE  = false; // Bypass all error detection
const long WATCHDOG_TIMEOUT = 2000; // Time in ms before stopping if no PC Ping

// --- SIGNAL FILTERING ---
// Exponential Moving Average factor (0.0 - 1.0). 
// Lower = smoother but slower. Higher = responsive but noisier.
const float FILTER_ALPHA  = 0.2; 
float filteredValue       = 0.0; 

// --- DEBOUNCE VARIABLES ---
const unsigned long DEBOUNCE_DELAY = 10; // ms to wait for stable signal
int envelopeState = HIGH;                // Current stable state (default HIGH/pullup)
int lastFlickerableState = HIGH;         // Previous raw reading
unsigned long lastDebounceTime = 0;

// --- STATE MACHINE ---
enum SystemState {
  STATE_IDLE,
  STATE_MEASURING,
  STATE_FAULT
};
SystemState currentState = STATE_IDLE;

// --- VARIABLES ---
unsigned long lastTelemetryTime = 0;
unsigned long lastPingReceived  = 0;
int maxPeakInWindow             = 0;
bool machineStopActive          = false;

// Telemetry Rate
const int TELEMETRY_INTERVAL    = 100; // Send data every 100ms (10Hz)

void setup() {
  // 1. Initialize Serial
  Serial.begin(115200);
  Serial.setTimeout(10); // Short timeout for non-blocking feel

  // 2. Configure Pins
  pinMode(PIN_ENVELOPE, INPUT_PULLUP); // Assume Active LOW (Ground = Envelope Present)

  pinMode(PIN_ENABLE_OUT, OUTPUT);

  // Initial Output States
  digitalWrite(PIN_ENABLE_OUT, HIGH);  // High = Enabled, Low = Disabled

  // 3. Init State
  lastPingReceived = millis();
  filteredValue = analogRead(PIN_SENSOR); // Seed filter
  if (CFG_REVERSE_SENSOR) {
    filteredValue = 1023 - filteredValue;
  }
  envelopeState = digitalRead(PIN_ENVELOPE); // Seed debounce
  lastFlickerableState = envelopeState;

  Serial.println("MSG:System Booted");
}

void loop() {
  unsigned long currentMillis = millis();

  // ============================================================
  // 1. READ & FILTER SENSOR
  // ============================================================
  int rawValue = analogRead(PIN_SENSOR);
  // Apply reversal if configured (for upside-down sensor installation)
  if (CFG_REVERSE_SENSOR) {
    rawValue = 1023 - rawValue;
  }
  // EMA Filter: New = (Alpha * Raw) + ((1-Alpha) * Old)
  filteredValue = (FILTER_ALPHA * rawValue) + ((1.0 - FILTER_ALPHA) * filteredValue);
  int sensorValue = (int)filteredValue;

  // ============================================================
  // 2. WATCHDOG & SAFETY CHECK (skipped if system override enabled)
  // ============================================================
  if (!CFG_SYSTEM_OVERRIDE) {
    // A. PC Connection Watchdog
    if (currentMillis - lastPingReceived > WATCHDOG_TIMEOUT) {
      if (!machineStopActive) {
        triggerStop("ERR:WATCHDOG_TIMEOUT");
      }
    }

    // B. Sensor Range Check (50-1000 absolute valid range)
    if (sensorValue < 50 || sensorValue > 1000) {
      if (!machineStopActive) {
        triggerStop("ERR:SENSOR_OUT_OF_RANGE");
      }
    }
  }

  // ============================================================
  // 3. LOGIC STATE MACHINE (Envelope Window)
  // ============================================================
  
  // --- DEBOUNCE INPUT ---
  int reading = digitalRead(PIN_ENVELOPE);
  
  // If the switch changed, due to noise or pressing:
  if (reading != lastFlickerableState) {
    lastDebounceTime = currentMillis; // reset the debouncing timer
    lastFlickerableState = reading;
  }

  if ((currentMillis - lastDebounceTime) > DEBOUNCE_DELAY) {
    // Whatever the reading is at, it's been there for longer than the debounce
    // delay, so take it as the actual current state:

    if (reading != envelopeState) {
      envelopeState = reading;
    }
  }
  
  bool isEnvelopePresent = (envelopeState == LOW); // Assuming Active LOW

  switch (currentState) {
    case STATE_IDLE:
      if (isEnvelopePresent) {
        // TRANSITION: IDLE -> MEASURING
        currentState = STATE_MEASURING;
        maxPeakInWindow = 0; // Reset peak for new envelope
      }
      break;

    case STATE_MEASURING:
      // Track the highest value seen while envelope is passing
      if (sensorValue > maxPeakInWindow) {
        maxPeakInWindow = sensorValue;
      }

      if (!isEnvelopePresent) {
        // TRANSITION: MEASURING -> IDLE (Envelope finished passing)
        validateResult(); 
        currentState = STATE_IDLE;
      }
      break;

    case STATE_FAULT:
      // Wait for manual reset command from PC
      break;
  }

  // ============================================================
  // 4. SERIAL COMMUNICATION (RX)
  // ============================================================
  if (Serial.available() > 0) {
    String command = Serial.readStringUntil('\n');
    command.trim();
    processCommand(command);
  }

  // ============================================================
  // 5. TELEMETRY (TX)
  // ============================================================
  if (currentMillis - lastTelemetryTime >= TELEMETRY_INTERVAL) {
    lastTelemetryTime = currentMillis;
    // Format: D:SensorVal,EnvelopeState,StopState
    // We send sensorValue (filtered) to give PC a smooth graph
    Serial.print("D:");
    Serial.print(sensorValue);
    Serial.print(",");
    Serial.print(isEnvelopePresent ? 1 : 0);
    Serial.print(",");
    Serial.println(machineStopActive ? 1 : 0);
  }
}

// ------------------------------------------------------------
// LOGIC FUNCTIONS
// ------------------------------------------------------------

void validateResult() {
  // Logic: Check if peak is within valid range
  // Below lower threshold = empty envelope (no card)
  // Above upper threshold = double card

  if (maxPeakInWindow >= CFG_CARD_THRESHOLD && maxPeakInWindow <= CFG_CARD_UPPER_THRESHOLD) {
    // PASS: Card detected within valid range
    Serial.print("EVT:PASS:");
    Serial.println(maxPeakInWindow);
  } else if (maxPeakInWindow < CFG_CARD_THRESHOLD) {
    // FAIL: Peak was below threshold (Empty Envelope)
    if (CFG_SYSTEM_OVERRIDE) {
      Serial.print("EVT:PASS_OVERRIDE:");
      Serial.println(maxPeakInWindow);
    } else {
      Serial.print("ERR:EMPTY_ENVELOPE:");
      Serial.println(maxPeakInWindow);
      machineStopActive = true;
      currentState = STATE_FAULT;
      digitalWrite(PIN_ENABLE_OUT, LOW);
    }
  } else {
    // FAIL: Peak was above upper threshold (Double Card)
    if (CFG_SYSTEM_OVERRIDE) {
      Serial.print("EVT:PASS_OVERRIDE:");
      Serial.println(maxPeakInWindow);
    } else {
      Serial.print("ERR:DOUBLE_CARD:");
      Serial.println(maxPeakInWindow);
      machineStopActive = true;
      currentState = STATE_FAULT;
      digitalWrite(PIN_ENABLE_OUT, LOW);
    }
  }
}

void triggerStop(String reason) {
  machineStopActive = true;
  currentState = STATE_FAULT;
  digitalWrite(PIN_ENABLE_OUT, LOW); // Disable machine
  Serial.println(reason);  // Send the error (already has ERR: prefix)
}

void resetSystem() {
  machineStopActive = false;
  currentState = STATE_IDLE;
  digitalWrite(PIN_ENABLE_OUT, HIGH); // Enable machine
  // Reset filter to avoid instant re-trigger
  filteredValue = analogRead(PIN_SENSOR);
  if (CFG_REVERSE_SENSOR) {
    filteredValue = 1023 - filteredValue;
  }
  Serial.println("MSG:System Resumed");
}

// ------------------------------------------------------------
// SERIAL COMMAND PARSER
// ------------------------------------------------------------
void processCommand(String cmd) {
  // Heartbeat
  if (cmd == "PING") {
    lastPingReceived = millis();
    // Ready LED stays solid ON; no toggling
    return;
  }

  // Resume after fault
  if (cmd == "RESUME") {
    resetSystem();
    return;
  }

  // Configuration: Set Lower Threshold (e.g., "SET_THR:150")
  if (cmd.startsWith("SET_THR:")) {
    int val = cmd.substring(8).toInt();
    if (val > 0 && val <= 1023) {
      CFG_CARD_THRESHOLD = val;
      Serial.print("MSG:Card Threshold Set to ");
      Serial.println(CFG_CARD_THRESHOLD);
    }
  }

  // Configuration: Set Upper Threshold (e.g., "SET_THR_UPPER:800")
  if (cmd.startsWith("SET_THR_UPPER:")) {
    int val = cmd.substring(14).toInt();
    if (val > 0 && val <= 1023) {
      CFG_CARD_UPPER_THRESHOLD = val;
      Serial.print("MSG:Card Upper Threshold Set to ");
      Serial.println(CFG_CARD_UPPER_THRESHOLD);
    }
  }

  // Configuration: Set Floor Value (e.g., "SET_FLOOR:100")
  if (cmd.startsWith("SET_FLOOR:")) {
    int val = cmd.substring(10).toInt();
    if (val >= 0 && val <= 1023) {
      CFG_FLOOR_VALUE = val;
      Serial.print("MSG:Floor Value Set to ");
      Serial.println(CFG_FLOOR_VALUE);
    }
  }

  // Configuration: Set Reverse Sensor (e.g., "SET_REVERSE:1")
  if (cmd.startsWith("SET_REVERSE:")) {
    int val = cmd.substring(12).toInt();
    CFG_REVERSE_SENSOR = (val == 1);
    Serial.print("MSG:Reverse Sensor ");
    Serial.println(CFG_REVERSE_SENSOR ? "Enabled" : "Disabled");
  }

  // Configuration: Set System Override (e.g., "SET_OVERRIDE:1")
  if (cmd.startsWith("SET_OVERRIDE:")) {
    int val = cmd.substring(13).toInt();
    CFG_SYSTEM_OVERRIDE = (val == 1);
    Serial.print("MSG:System Override ");
    Serial.println(CFG_SYSTEM_OVERRIDE ? "ENABLED - Safety bypassed!" : "Disabled");
  }
}