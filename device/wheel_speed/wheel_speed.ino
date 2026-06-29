// Arduino Uno -> STM32 USART2 (115200)
// Wiring: Uno D1(TX) -> STM32 PD6(USART2_RX), GND common
// A0 = 오른쪽 휠스피드, A1 = 왼쪽 휠스피드
// 20 Hz마다 50 ms 동안 센 펄스 수 전송: WPS,R,<count>,L,<count>

const int PIN_RIGHT = A0;
const int PIN_LEFT = A1;
const float ADC_VREF = 5.0f;

const float V_HIGH = 4.3f;
const float V_LOW = 3.6f;

const int RAW_HIGH = (int)(V_HIGH * 1023.0f / ADC_VREF + 0.5f);  // 4.3V 이상 → HIGH
const int RAW_LOW = (int)(V_LOW * 1023.0f / ADC_VREF + 0.5f);    // 3.6V 이하 → LOW
// 3.6~4.3V 사이(데드밴드)는 이전 상태 유지 → 채터링 방지

const unsigned long SEND_INTERVAL_MS = 50;  // 20 Hz

struct WheelChannel {
  int pin;
  unsigned int pulseCount;
  int lastState;
};

WheelChannel wheelRight = {PIN_RIGHT, 0, HIGH};
WheelChannel wheelLeft = {PIN_LEFT, 0, HIGH};

unsigned long lastSendMs = 0;

void sendPulseLine(unsigned int rightCount, unsigned int leftCount) {
  Serial.print(F("WPS,R,"));
  Serial.print(rightCount);
  Serial.print(F(",L,"));
  Serial.println(leftCount);
}

void processWheel(WheelChannel &wheel) {
  const int raw = analogRead(wheel.pin);

  int currentState = wheel.lastState;
  if (raw >= RAW_HIGH) {
    currentState = HIGH;
  } else if (raw <= RAW_LOW) {
    currentState = LOW;
  }

  if (wheel.lastState == HIGH && currentState == LOW) {
    wheel.pulseCount++;
  }
  wheel.lastState = currentState;
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_RIGHT, INPUT);
  pinMode(PIN_LEFT, INPUT);
  sendPulseLine(0, 0);
}

void loop() {
  const unsigned long now = millis();

  processWheel(wheelRight);
  processWheel(wheelLeft);

  if (now - lastSendMs >= SEND_INTERVAL_MS) {
    sendPulseLine(wheelRight.pulseCount, wheelLeft.pulseCount);
    wheelRight.pulseCount = 0;
    wheelLeft.pulseCount = 0;
    lastSendMs = now;
  }
}
