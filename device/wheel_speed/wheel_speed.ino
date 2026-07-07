// Arduino Nano 휠스피드 — 현재 A0(오른쪽)만 사용
// VOLTAGE_MONITOR=1 이면 A0 전압만 출력
// VOLTAGE_MONITOR=0 이면 STM32로 WPS,R,<count>,L,0 전송

#define VOLTAGE_MONITOR  1

const int PIN_SENSOR = A0;

const float MEASURED_VCC = 4.5f;

const float RATIO_HIGH = 4.3f / 5.0f;
const float RATIO_LOW = 3.6f / 5.0f;
const int RAW_HIGH = (int)(1023.0f * RATIO_HIGH + 0.5f);
const int RAW_LOW = (int)(1023.0f * RATIO_LOW + 0.5f);

#if !VOLTAGE_MONITOR
const unsigned long SEND_INTERVAL_MS = 50;

unsigned int pulseCount = 0;
int lastState = HIGH;
unsigned long lastSendMs = 0;

void sendPulseLine(unsigned int rightCount) {
  Serial.print(F("WPS,R,"));
  Serial.print(rightCount);
  Serial.println(F(",L,0"));
}

void processSensor() {
  const int raw = analogRead(PIN_SENSOR);

  int currentState = lastState;
  if (raw >= RAW_HIGH) {
    currentState = HIGH;
  } else if (raw <= RAW_LOW) {
    currentState = LOW;
  }

  if (lastState == HIGH && currentState == LOW) {
    pulseCount++;
  }
  lastState = currentState;
}
#endif

#if VOLTAGE_MONITOR
const unsigned long PRINT_INTERVAL_MS = 100;
unsigned long lastPrintMs = 0;

float rawToVolts(int raw) {
  return raw * MEASURED_VCC / 1023.0f;
}

const char *stateLabel(int raw) {
  if (raw >= RAW_HIGH) {
    return "HIGH";
  }
  if (raw <= RAW_LOW) {
    return "LOW";
  }
  return "MID";
}
#endif

void setup() {
  Serial.begin(115200);
  pinMode(PIN_SENSOR, INPUT);

#if VOLTAGE_MONITOR
  Serial.print(F("VOLTAGE_MONITOR A0 only  VCC="));
  Serial.print(MEASURED_VCC, 2);
  Serial.print(F("V  HIGH>="));
  Serial.print(RAW_HIGH);
  Serial.print(F("  LOW<="));
  Serial.println(RAW_LOW);
#else
  sendPulseLine(0);
#endif
}

void loop() {
#if VOLTAGE_MONITOR
  const unsigned long now = millis();
  if (now - lastPrintMs >= PRINT_INTERVAL_MS) {
    const int raw = analogRead(PIN_SENSOR);
    Serial.print(F("A0,raw="));
    Serial.print(raw);
    Serial.print(F(",V="));
    Serial.print(rawToVolts(raw), 2);
    Serial.print(F(",st="));
    Serial.println(stateLabel(raw));
    lastPrintMs = now;
  }
#else
  const unsigned long now = millis();

  processSensor();

  if (now - lastSendMs >= SEND_INTERVAL_MS) {
    sendPulseLine(pulseCount);
    pulseCount = 0;
    lastSendMs = now;
  }
#endif
}
