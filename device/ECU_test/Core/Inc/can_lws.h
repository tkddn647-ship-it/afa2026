/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    can_lws.h
  * @brief   Bosch LWS 조향각 센서 — CAN2 (500 kbaud, ID 0x2B0)
  ******************************************************************************
  */
/* USER CODE END Header */

#ifndef __CAN_LWS_H__
#define __CAN_LWS_H__

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>
#include "stm32f4xx_hal.h"

#define CAN_LWS_STD_ID     0x2B0U
#define CAN_LWS_CONFIG_ID  0x7C0U
#define CAN_LWS_CCW_ZERO   0x03U
#define CAN_LWS_CCW_RESET  0x05U

typedef struct
{
  float angle_deg;
  float speed_dps;
  uint8_t ok;
  uint8_t cal;
  uint8_t trim;
  uint8_t angle_valid;
  uint8_t speed_valid;
  uint32_t last_rx_ms;
  uint32_t rx_count;
} CanLwsState_t;

extern volatile CanLwsState_t g_lws_state;
extern volatile uint32_t lws_cal_tx_count;
extern volatile uint8_t lws_last_cal_ccw;

void CAN_LWS_Init(void);
void CAN_LWS_Process(void);
void CAN_LWS_GetState(CanLwsState_t *out);
HAL_StatusTypeDef CAN_LWS_SendConfigCcw(uint8_t ccw);

#ifdef __cplusplus
}
#endif

#endif /* __CAN_LWS_H__ */
