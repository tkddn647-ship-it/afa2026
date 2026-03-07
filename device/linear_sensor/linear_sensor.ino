/*
 * ESP32 리니어 센서 -> STM32 UART 전송 코드
 * * [연결]
 * 센서 VCC -> ESP32 3V3
 * 센서 GND -> ESP32 GND
 * 센서 SIG -> ESP32 GPIO 34
 * * [통신 연결]
 * ESP32 TX2 (GPIO 17) -> STM32 UART4 RX
 * ESP32 RX2 (GPIO 16) -> STM32 UART4 TX
 * ESP32 GND           -> STM32 GND
 */

#include <HardwareSerial.h>

// 1. 설정 변수
const int SENSOR_PIN = 34;      
const float MAX_STROKE = 100.0; // 센서 최대 길이 (mm)
const int SAMPLE_COUNT = 20;    

// STM32와 통신할 시리얼 포트 설정 (Serial2 사용)
// ESP32 기본 핀: RX=16, TX=17
HardwareSerial MySerial(2); 

void setup() {
  // PC 디버깅용 시리얼
  Serial.begin(115200);       
  
  // STM32 통신용 시리얼 (속도 115200, 8N1, RX핀, TX핀)
  MySerial.begin(115200, SERIAL_8N1, 16, 17);

  analogReadResolution(12);   
  analogSetAttenuation(ADC_11db); 

  Serial.println("Sensor Ready! Sending to STM32...");
}

void loop() {
  // 1. 센서 값 읽기 (평균 필터)
  long sum = 0;
  for(int i = 0; i < SAMPLE_COUNT; i++) {
    sum += analogRead(SENSOR_PIN);
    delay(1); 
  }
  int adcValue = sum / SAMPLE_COUNT; 

  // 2. 거리(mm)로 변환
  // 공식: (현재값 / 최대값) * 센서길이
  float distanceFloat = (adcValue / 4095.0) * MAX_STROKE;
  
  // [수정된 부분] 정수 변환(int casting) 삭제함
  
  // ---------------------------------------------------------
  // STM32로 데이터 전송 (소수점 포함)
  // 형식: "45.12\n" (소수점 2자리까지)
  // ---------------------------------------------------------
  MySerial.print(distanceFloat, 2); // 뒤에 숫자 2가 소수점 자릿수임
  MySerial.write('\n'); // 종료 문자 전송

  // ---------------------------------------------------------
  // PC 시리얼 모니터 확인용
  // ---------------------------------------------------------
  Serial.print("Sending to STM32 -> Val: ");
  Serial.println(distanceFloat, 2); // 여기도 소수점 2자리 출력

  delay(50); 
}