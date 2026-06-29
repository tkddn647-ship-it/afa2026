/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    sensor_uart.h
  * @brief   200 Hz 센서 버퍼 + UART4(Raspberry Pi) 20 Hz 전송
  ******************************************************************************
  */
/* USER CODE END Header */

#ifndef __SENSOR_UART_H__
#define __SENSOR_UART_H__

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

#define SENSOR_SAMPLE_RATE_HZ   200U
#define SENSOR_FLUSH_RATE_HZ    20U
#define SENSOR_BATCH_SIZE       (SENSOR_SAMPLE_RATE_HZ / SENSOR_FLUSH_RATE_HZ)

typedef struct
{
  uint32_t timestamp_ms;
  float FR;
  float FL;
  float RR;
  float RL;
  float x_g;
  float y_g;
  float z_g;
  float ecu_temp_c; /* MCU 칩 내부(다이) 온도 °C — ECU/보드 공기 온도 아님 */
  float steering_angle_deg; /* Bosch LWS CAN2 조향각 ° */
  float steering_speed_dps;   /* Bosch LWS CAN2 조향 속도 °/s */
  float wheel_rpm_right;      /* 오른쪽 바퀴 RPM (STM32 계산) */
  float wheel_rpm_left;       /* 왼쪽 바퀴 RPM (STM32 계산) */
} SensorSample_t;

extern SensorSample_t data[SENSOR_BATCH_SIZE];
extern volatile uint8_t data_count;
extern volatile uint32_t sensor_uart_tx_lines;
extern volatile uint32_t sensor_uart_overflow;

void SensorUart_Init(void);
void SensorUart_Process(void);

#ifdef __cplusplus
}
#endif

#endif /* __SENSOR_UART_H__ */
