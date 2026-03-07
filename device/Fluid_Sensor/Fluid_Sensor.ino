const byte sensorPin = 2; 

const float mL_per_pulse = 2.5; 

volatile unsigned int pulseCount = 0; // 펄스 개수를 저장할 변수
float flowRate_L_min = 0.0;           // 분당 유량 (L/min)
unsigned long oldTime = 0;            // 시간 계산을 위한 변수

void setup() {
  Serial.begin(9600);
  
  pinMode(sensorPin, INPUT_PULLUP); 
  attachInterrupt(digitalPinToInterrupt(sensorPin), pulseCounter, FALLING);
  
  oldTime = millis();
}

void loop() {
  unsigned long currentTime = millis();
 
  if ((currentTime - oldTime) >= 1000) { 

    detachInterrupt(digitalPinToInterrupt(sensorPin));
 
    float volume_mL_in_1sec = pulseCount * mL_per_pulse;
    flowRate_L_min = (volume_mL_in_1sec * 60.0) / 1000.0;

    pulseCount = 0;
    oldTime = currentTime;

    attachInterrupt(digitalPinToInterrupt(sensorPin), pulseCounter, FALLING);

    Serial.print(flowRate_L_min, 2); // 소수점 2자리까지 출력 원할 시 숫자 바꿔서 진행
    Serial.println(" L/min");
  }
}

void pulseCounter() {
  pulseCount++;
}