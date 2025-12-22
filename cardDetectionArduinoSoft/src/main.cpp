#include <Arduino.h>

// --- FORWARD DECLARATIONS ---
void validateResult();
void triggerStop(String reason);
void resetSystem();
void processCommand(String cmd);

// --- HARDWARE PIN DEFINITIONS ---
const int PIN_SENSOR      = A0;  // Analog Height Sensor
const int PIN_ENVELOPE    = 2;   // Digital Input: Envelope Present Signal
const int PIN_STOP_OUT    = 8;   // Digital Output: Machine Stop Trigger
const int PIN_READY_OUT   = 9;   // Digital Output: System Heartbeat/Ready LED

// --- CONFIGURATION CONSTANTS (DEFAULTS) ---
// These can be updated via Serial commands
int CFG_CARD_THRESHOLD    = 50;  // Delta needed to confirm card (e.g., +50 ADC steps)
int CFG_BASE_FLOOR        = 30;  // Minimum valid sensor value (detects broken wire)
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
  
  pinMode(PIN_STOP_OUT, OUTPUT);
  pinMode(PIN_READY_OUT, OUTPUT);
  
  // Initial Output States
  digitalWrite(PIN_STOP_OUT, LOW);  // Low = Run, High = Stop (Assumed logic)
  digitalWrite(PIN_READY_OUT, HIGH); // HIGH = System Ready (Fully ON)

  // 3. Init State
  lastPingReceived = millis();
  filteredValue = analogRead(PIN_SENSOR); // Seed filter
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
  // EMA Filter: New = (Alpha * Raw) + ((1-Alpha) * Old)
  filteredValue = (FILTER_ALPHA * rawValue) + ((1.0 - FILTER_ALPHA) * filteredValue);
  int sensorValue = (int)filteredValue;

  // ============================================================
  // 2. WATCHDOG & SAFETY CHECK
  // ============================================================
  // A. PC Connection Watchdog
  if (currentMillis - lastPingReceived > WATCHDOG_TIMEOUT) {
    if (!machineStopActive) {
      triggerStop("ERR:WATCHDOG_TIMEOUT");
    }
  }

  // B. Sensor Health (Broken Wire Check)
  if (sensorValue < CFG_BASE_FLOOR) {
    if (!machineStopActive) {
      triggerStop("ERR:SENSOR_FAULT_LOW");
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
  // Logic: Did we see a peak high enough to be a card?
  // Note: We are comparing absolute ADC value against a configured Threshold.
  // Ideally, Threshold should be (BaseFloor + ThicknessDelta).
  
  if (maxPeakInWindow >= CFG_CARD_THRESHOLD) {
    // PASS: Card detected
    Serial.println("EVT:PASS"); 
  } else {
    // FAIL: Peak was too low (Empty Envelope)
    triggerStop("ERR:EMPTY_ENVELOPE");
  }
}

void triggerStop(String reason) {
  machineStopActive = true;
  currentState = STATE_FAULT;
  digitalWrite(PIN_STOP_OUT, HIGH); // Activate Stop Relay
  digitalWrite(PIN_READY_OUT, LOW); // Turn OFF Ready LED
  Serial.print("LOG:"); Serial.println(reason);
}

void resetSystem() {
  machineStopActive = false;
  currentState = STATE_IDLE;
  digitalWrite(PIN_STOP_OUT, LOW); // Release Stop Relay
  digitalWrite(PIN_READY_OUT, HIGH); // Turn ON Ready LED
  // Reset filter to avoid instant re-trigger
  filteredValue = analogRead(PIN_SENSOR); 
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

  // Configuration: Set Threshold (e.g., "SET_THR:600")
  if (cmd.startsWith("SET_THR:")) {
    int val = cmd.substring(8).toInt();
    if (val > 0 && val < 1023) {
      CFG_CARD_THRESHOLD = val;
      Serial.print("MSG:Threshold Set to ");
      Serial.println(CFG_CARD_THRESHOLD);
    }
  }

  // Configuration: Set Floor (e.g., "SET_MIN:25")
  if (cmd.startsWith("SET_MIN:")) {
    int val = cmd.substring(8).toInt();
    if (val > 0 && val < 1023) {
      CFG_BASE_FLOOR = val;
      Serial.print("MSG:Floor Set to ");
      Serial.println(CFG_BASE_FLOOR);
    }
  }
}