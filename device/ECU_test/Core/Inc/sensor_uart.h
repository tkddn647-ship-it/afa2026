/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    sensor_uart.h
  * @brief   100 Hz 센서 버퍼 + UART4(Raspberry Pi) 20 Hz DMA 전송
  ******************************************************************************
  */
/* USER CODE END Header */

#ifndef __SENSOR_UART_H__
#define __SENSOR_UART_H__

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

#define SENSOR_SAMPLE_RATE_HZ   100U
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

  /* CAN1 vehicle bus (Cascadia + Orion BMS) — 마지막 수신값 홀드 */
  float inv_temp_igt_c;
  float inv_temp_gatedriver_c;
  float inv_temp_coolant_c;
  float inv_temp_hotspot_c;
  float inv_temp_motor_c;
  float inv_motor_speed_rpm;
  float inv_motor_angle_deg;
  float inv_dc_current_a;
  float inv_a_current_a;
  float inv_b_current_a;
  float inv_c_current_a;
  float inv_voltage_v;
  float inv_voltage_output_v;
  uint8_t motor_precharge;
  uint8_t motor_main_contactor;
  uint8_t motor_inverter_mode; /* 0=Torque Mode, 1=Speed Mode */
  float inv_shudder_torque_nm;
  float inv_id_feedback_a;
  float inv_iq_feedback_a;
  float inv_torque_commanded_nm;
  float inv_torque_feedback_nm;
  float inv_id_command_a;
  float inv_iq_command_a;
  float bms_charge_pct;
  float bms_capacity_ah;
  float bms_voltage_v;
  float bms_current_a;
  float bms_ccl_a;
  float bms_dcl_a;
  float bms_temp_max_c;
  uint8_t bms_temp_max_id;
  float bms_temp_internal_c;
  uint8_t can1_link; /* 1=CAN1(인버터/BMS) 최근 수신, 0=끊김 */
  uint8_t can2_link; /* 1=CAN2(LWS 조향) 최근 수신, 0=끊김 */
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
