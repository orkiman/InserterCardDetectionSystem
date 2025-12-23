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

// --- CONFIGURATION CONSTANTS (DEFAULTS) ---
// These can be updated via Serial commands
int CFG_FLOOR_VALUE       = 100; // Floor ADC value (50-500 valid range)
int CFG_CARD_THRESHOLD    = 150; // Below this = empty envelope (error)
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

  // Initial Output States
  digitalWrite(PIN_STOP_OUT, LOW);  // Low = Run, High = Stop (Assumed logic)

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

  // B. Sensor Range Check (50-1000 absolute valid range)
  if (sensorValue < 50 || sensorValue > 1000) {
    if (!machineStopActive) {
      triggerStop("ERR:SENSOR_OUT_OF_RANGE");
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
  // Logic: Check if peak is above threshold
  // If below threshold = empty envelope (no card)

  if (maxPeakInWindow >= CFG_CARD_THRESHOLD) {
    // PASS: Card detected
    Serial.println("EVT:PASS");
  } else {
    // FAIL: Peak was below threshold (Empty Envelope)
    triggerStop("ERR:EMPTY_ENVELOPE");
  }
}

void triggerStop(String reason) {
  machineStopActive = true;
  currentState = STATE_FAULT;
  digitalWrite(PIN_STOP_OUT, HIGH); // Activate Stop Relay
  Serial.println(reason);  // Send the error (already has ERR: prefix)
}

void resetSystem() {
  machineStopActive = false;
  currentState = STATE_IDLE;
  digitalWrite(PIN_STOP_OUT, LOW); // Release Stop Relay
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

  // Configuration: Set Threshold (e.g., "SET_THR:150")
  if (cmd.startsWith("SET_THR:")) {
    int val = cmd.substring(8).toInt();
    if (val > 0 && val <= 1023) {
      CFG_CARD_THRESHOLD = val;
      Serial.print("MSG:Card Threshold Set to ");
      Serial.println(CFG_CARD_THRESHOLD);
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
}