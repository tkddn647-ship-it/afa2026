// SEN-HZ43WB G3/4" Hall Effect Water Flow Sensor
// Datasheet: F = 8.1 × Q(L/min) - 5  (±10%)
//            1 L = 477 pulse (±10%)
// Wiring: Red=VCC(5V), Black=GND, Yellow=Pulse → D2

const byte sensorPin = 2;

// Instantaneous flow: Q = (F + 5) / 8.1   (F = pulses/sec)
const float FLOW_K = 8.1;
const float FLOW_OFFSET = 5.0;

volatile unsigned int pulseCount = 0;
float flowRate_L_min = 0.0;
unsigned long oldTime = 0;

void setup() {
  Serial.begin(9600);

  pinMode(sensorPin, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(sensorPin), pulseCounter, FALLING);

  oldTime = millis();
}

void loop() {
  unsigned long currentTime = millis();

  // 1000 ms = 1초마다 유량 계산
  if ((currentTime - oldTime) >= 1000) {
    detachInterrupt(digitalPinToInterrupt(sensorPin));

    // 1초 동안의 펄스 수 ≈ F(Hz)
    // Q(L/min) = (F + 5) / 8.1
    // 펄스가 없으면 유량 0 (공식 offset 때문에 (0+5)/8.1 이 나오면 안 됨)
    if (pulseCount == 0) {
      flowRate_L_min = 0.0;
    } else {
      flowRate_L_min = ((float)pulseCount + FLOW_OFFSET) / FLOW_K;
    }

    pulseCount = 0;
    oldTime = currentTime;

    attachInterrupt(digitalPinToInterrupt(sensorPin), pulseCounter, FALLING);

    Serial.print(flowRate_L_min, 2);
    Serial.println(" L/min");
  }
}

void pulseCounter() {
  pulseCount++;
}
