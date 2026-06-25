const int SENSOR_PIN = A0;
const int THRESHOLD_HIGH_RAW = 840;
const int THRESHOLD_LOW_RAW = 790;
const unsigned long SEND_INTERVAL_MS = 100;

unsigned int pulseCount = 0;
int lastState = HIGH;
unsigned long lastSendMs = 0;

void setup() {
  Serial.begin(115200);
  pinMode(SENSOR_PIN, INPUT);
}

void loop() {
  const unsigned long now = millis();
  const int raw = analogRead(SENSOR_PIN);

  int currentState = lastState;
  if (lastState == HIGH && raw < THRESHOLD_LOW_RAW) {
    currentState = LOW;
  } else if (lastState == LOW && raw > THRESHOLD_HIGH_RAW) {
    currentState = HIGH;
  }

  // Count one pulse on each low-going edge after hysteresis filtering.
  if (lastState == HIGH && currentState == LOW) {
    pulseCount++;
  }
  lastState = currentState;

  if (now - lastSendMs >= SEND_INTERVAL_MS) {
    Serial.print("{\"t\":");
    Serial.print(now);
    Serial.print(",\"pulse\":");
    Serial.print(pulseCount);
    Serial.println("}");

    pulseCount = 0;
    lastSendMs = now;
  }
}
