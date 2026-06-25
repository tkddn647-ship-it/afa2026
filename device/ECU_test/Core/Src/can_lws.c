/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    can_lws.c
  * @brief   Bosch Steering Wheel Angle Sensor (LWS) on CAN2
  ******************************************************************************
  */
/* USER CODE END Header */

#include "can_lws.h"
#include "can.h"

#define CAN2_FILTER_BANK_START  14U

#define LWS_ANGLE_INVALID_RAW   0x7FFF
#define LWS_SPEED_INVALID_RAW   0xFFU

#define LWS_ANGLE_SCALE_DEG     0.1f
#define LWS_SPEED_SCALE_DPS     4.0f

static CanLwsState_t lws_state;

volatile CanLwsState_t g_lws_state;
volatile uint32_t lws_cal_tx_count = 0U;
volatile uint8_t lws_last_cal_ccw = 0U;

static void CAN_LWS_SyncDebugState(void)
{
  g_lws_state = lws_state;
}

static void CAN_LWS_ParsePayload(const uint8_t *data, uint8_t dlc)
{
  uint16_t angle_u16;
  int16_t angle_raw;
  uint8_t speed_raw;
  uint8_t status;
  uint8_t ok_bit;
  uint8_t cal_bit;
  uint8_t trim_bit;

  if ((data == NULL) || (dlc < 4U))
  {
    return;
  }

  angle_u16 = (uint16_t)data[0] | ((uint16_t)data[1] << 8);
  angle_raw = (int16_t)angle_u16;
  speed_raw = data[2];
  status = data[3];

  ok_bit = status & 0x01U;
  cal_bit = (status >> 1) & 0x01U;
  trim_bit = (status >> 2) & 0x01U;

  lws_state.ok = ok_bit;
  lws_state.cal = cal_bit;
  lws_state.trim = trim_bit;
  lws_state.angle_valid = (uint8_t)((trim_bit != 0U) && (ok_bit != 0U) && (cal_bit != 0U) &&
                                    (angle_u16 != LWS_ANGLE_INVALID_RAW));
  lws_state.speed_valid = (uint8_t)((trim_bit != 0U) && (ok_bit != 0U) &&
                                    (speed_raw != LWS_SPEED_INVALID_RAW));

  if (lws_state.angle_valid != 0U)
  {
    lws_state.angle_deg = (float)angle_raw * LWS_ANGLE_SCALE_DEG;
  }

  if (lws_state.speed_valid != 0U)
  {
    lws_state.speed_dps = (float)speed_raw * LWS_SPEED_SCALE_DPS;
  }

  lws_state.last_rx_ms = HAL_GetTick();
  lws_state.rx_count++;
  CAN_LWS_SyncDebugState();
}

void CAN_LWS_Init(void)
{
  CAN_FilterTypeDef filter = {0};

  lws_state.angle_deg = 0.0f;
  lws_state.speed_dps = 0.0f;
  lws_state.ok = 0U;
  lws_state.cal = 0U;
  lws_state.trim = 0U;
  lws_state.angle_valid = 0U;
  lws_state.speed_valid = 0U;
  lws_state.last_rx_ms = 0U;
  lws_state.rx_count = 0U;
  CAN_LWS_SyncDebugState();

  filter.FilterBank = CAN2_FILTER_BANK_START;
  filter.FilterMode = CAN_FILTERMODE_IDMASK;
  filter.FilterScale = CAN_FILTERSCALE_32BIT;
  filter.FilterIdHigh = (uint16_t)(CAN_LWS_STD_ID << 5);
  filter.FilterIdLow = 0x0000U;
  filter.FilterMaskIdHigh = (uint16_t)(0x7FFU << 5);
  filter.FilterMaskIdLow = 0x0000U;
  filter.FilterFIFOAssignment = CAN_RX_FIFO0;
  filter.FilterActivation = ENABLE;
  filter.SlaveStartFilterBank = CAN2_FILTER_BANK_START;

  if (HAL_CAN_ConfigFilter(&hcan2, &filter) != HAL_OK)
  {
    return;
  }

  (void)HAL_CAN_Start(&hcan2);
}

void CAN_LWS_Process(void)
{
  CAN_RxHeaderTypeDef rx_header;
  uint8_t rx_data[8];

  while (HAL_CAN_GetRxFifoFillLevel(&hcan2, CAN_RX_FIFO0) > 0U)
  {
    if (HAL_CAN_GetRxMessage(&hcan2, CAN_RX_FIFO0, &rx_header, rx_data) != HAL_OK)
    {
      break;
    }

    if ((rx_header.IDE == CAN_ID_STD) && (rx_header.StdId == CAN_LWS_STD_ID))
    {
      CAN_LWS_ParsePayload(rx_data, rx_header.DLC);
    }
  }
}

void CAN_LWS_GetState(CanLwsState_t *out)
{
  if (out == NULL)
  {
    return;
  }

  *out = lws_state;
}

HAL_StatusTypeDef CAN_LWS_SendConfigCcw(uint8_t ccw)
{
  CAN_TxHeaderTypeDef tx_header;
  uint8_t tx_data[8] = {0};
  uint32_t mailbox;

  if (HAL_CAN_GetTxMailboxesFreeLevel(&hcan2) == 0U)
  {
    return HAL_BUSY;
  }

  tx_header.StdId = CAN_LWS_CONFIG_ID;
  tx_header.ExtId = 0U;
  tx_header.IDE = CAN_ID_STD;
  tx_header.RTR = CAN_RTR_DATA;
  tx_header.DLC = 2U;
  tx_header.TransmitGlobalTime = DISABLE;

  tx_data[0] = (uint8_t)(ccw & 0x07U);

  if (HAL_CAN_AddTxMessage(&hcan2, &tx_header, tx_data, &mailbox) != HAL_OK)
  {
    return HAL_ERROR;
  }

  lws_last_cal_ccw = ccw;
  lws_cal_tx_count++;
  return HAL_OK;
}
