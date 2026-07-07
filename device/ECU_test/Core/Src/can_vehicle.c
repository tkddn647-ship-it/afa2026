/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    can_vehicle.c
  * @brief   CAN1 vehicle bus — 스니핑 + BMS/MC 파싱 (ID는 can_vehicle.h 에 설정)
  ******************************************************************************
  */
/* USER CODE END Header */

#include "can_vehicle.h"
#include "can.h"
#include <string.h>

#define CAN1_FILTER_BANK        0U
#define CAN2_FILTER_BANK_START  14U

static CanVehicleState_t vehicle_state;

volatile CanVehicleState_t g_can_vehicle_state;

static void CAN_Vehicle_SyncDebugState(void)
{
  g_can_vehicle_state = vehicle_state;
}

/*
 * Orion BMS Failsafe Status (Utility 문서 bit0..6)
 * https://www.orionbms.com/manuals/utility_o2/bms_param_failsafe_statuses.html
 */
static void CAN_Vehicle_ParseOrionFailsafeWord(uint16_t word)
{
  CanBmsHvState_t *bms = &vehicle_state.bms;

  bms->failsafe_raw = word;
  bms->voltage_failsafe = (uint8_t)((word & 0x0001U) != 0U);
  bms->current_failsafe = (uint8_t)((word & 0x0002U) != 0U);
  bms->relay_failsafe = (uint8_t)((word & 0x0004U) != 0U);
  bms->cell_balancing = (uint8_t)((word & 0x0008U) != 0U);
  bms->interlock_failsafe = (uint8_t)((word & 0x0010U) != 0U);
  bms->thermistor_error = (uint8_t)((word & 0x0020U) != 0U);
  bms->input_power_failsafe = (uint8_t)((word & 0x0040U) != 0U);
  bms->failsafe_rx_count++;
  bms->last_failsafe_ms = HAL_GetTick();
}

static void CAN_Vehicle_ParseBmsFailsafe(const uint8_t *data, uint8_t dlc)
{
  uint16_t word;

  if (dlc < 2U)
  {
    return;
  }

  /* Orion 기본: failsafe status uint16 LE — DBC에서 endian 다르면 수정 */
  word = (uint16_t)data[0] | ((uint16_t)data[1] << 8);
  CAN_Vehicle_ParseOrionFailsafeWord(word);
}

static void CAN_Vehicle_ParseBmsTelemetry(const uint8_t *data, uint8_t dlc)
{
  CanBmsHvState_t *bms = &vehicle_state.bms;

  /*
   * TODO: DBC Export 후 바이트/스케일 맞추기.
   * 아래는 임시 raw→float (0.1 스케일 가정) — ID 매핑 전 스니핑용.
   */
  if (dlc >= 2U)
  {
    bms->soc_pct = (float)((uint16_t)data[0] | ((uint16_t)data[1] << 8)) * 0.1f;
  }
  if (dlc >= 4U)
  {
    bms->pack_voltage_v = (float)((uint16_t)data[2] | ((uint16_t)data[3] << 8)) * 0.1f;
  }
  if (dlc >= 6U)
  {
    bms->pack_current_a = (float)((int16_t)((uint16_t)data[4] | ((uint16_t)data[5] << 8))) * 0.1f;
  }
  if (dlc >= 8U)
  {
    bms->discharge_limit_a = (float)((uint16_t)data[6] | ((uint16_t)data[7] << 8)) * 0.1f;
  }

  bms->telemetry_rx_count++;
  bms->last_telemetry_ms = HAL_GetTick();
}

static void CAN_Vehicle_ParseMcStatus(const uint8_t *data, uint8_t dlc)
{
  CanMotorState_t *mc = &vehicle_state.motor;

  if (dlc < 1U)
  {
    return;
  }

  /*
   * TODO: 모터컨트롤러 DBC — 프리차지/메인컨택터/인버터모드 비트 위치 확인 후 수정.
   * 임시: byte0 비트필드 가정
   */
  mc->precharge_active = (uint8_t)((data[0] & 0x01U) != 0U);
  mc->main_contactor_closed = (uint8_t)((data[0] & 0x02U) != 0U);
  mc->inverter_mode = (uint8_t)((data[0] >> 2) & 0x0FU);

  if (dlc >= 2U)
  {
    mc->vsm_state = data[1];
  }
  if (dlc >= 4U)
  {
    mc->inv_state = (uint16_t)data[2] | ((uint16_t)data[3] << 8);
  }

  mc->status_rx_count++;
  mc->last_status_ms = HAL_GetTick();
}

static void CAN_Vehicle_ParseMcTelemetry(const uint8_t *data, uint8_t dlc)
{
  CanMotorState_t *mc = &vehicle_state.motor;

  if (dlc >= 2U)
  {
    mc->rpm = (float)((int16_t)((uint16_t)data[0] | ((uint16_t)data[1] << 8)));
  }
  if (dlc >= 4U)
  {
    mc->torque_nm = (float)((int16_t)((uint16_t)data[2] | ((uint16_t)data[3] << 8))) * 0.1f;
  }
  if (dlc >= 6U)
  {
    mc->temp_motor_c = (float)((int16_t)((uint16_t)data[4] | ((uint16_t)data[5] << 8))) * 0.1f;
  }
  if (dlc >= 8U)
  {
    mc->temp_igbt_c = (float)((int16_t)((uint16_t)data[6] | ((uint16_t)data[7] << 8))) * 0.1f;
  }

  mc->telemetry_rx_count++;
  mc->last_telemetry_ms = HAL_GetTick();
}

static void CAN_Vehicle_UpdateSniff(uint16_t std_id, const uint8_t *data, uint8_t dlc)
{
  uint8_t i;
  uint8_t empty = CAN_SNIFF_SLOT_COUNT;
  uint8_t oldest = 0U;
  uint32_t oldest_ms = 0xFFFFFFFFU;

  for (i = 0U; i < CAN_SNIFF_SLOT_COUNT; i++)
  {
    CanSniffSlot_t *slot = &vehicle_state.sniff[i];

    if (slot->std_id == std_id)
    {
      slot->hit_count++;
      slot->dlc = dlc;
      slot->last_ms = HAL_GetTick();
      (void)memcpy(slot->data, data, 8U);
      return;
    }

    if ((slot->std_id == 0U) && (empty == CAN_SNIFF_SLOT_COUNT))
    {
      empty = i;
    }

    if (slot->last_ms < oldest_ms)
    {
      oldest_ms = slot->last_ms;
      oldest = i;
    }
  }

  i = (empty < CAN_SNIFF_SLOT_COUNT) ? empty : oldest;

  vehicle_state.sniff[i].std_id = std_id;
  vehicle_state.sniff[i].hit_count = 1U;
  vehicle_state.sniff[i].dlc = dlc;
  vehicle_state.sniff[i].last_ms = HAL_GetTick();
  (void)memcpy(vehicle_state.sniff[i].data, data, 8U);
}

static void CAN_Vehicle_DispatchStd(uint16_t std_id, const uint8_t *data, uint8_t dlc)
{
  CAN_Vehicle_UpdateSniff(std_id, data, dlc);

  if ((CAN_BMS_FAILSAFE_STD_ID != 0U) && (std_id == CAN_BMS_FAILSAFE_STD_ID))
  {
    CAN_Vehicle_ParseBmsFailsafe(data, dlc);
    return;
  }

  if ((CAN_BMS_TELEMETRY_STD_ID != 0U) && (std_id == CAN_BMS_TELEMETRY_STD_ID))
  {
    CAN_Vehicle_ParseBmsTelemetry(data, dlc);
    return;
  }

  if ((CAN_MC_STATUS_STD_ID != 0U) && (std_id == CAN_MC_STATUS_STD_ID))
  {
    CAN_Vehicle_ParseMcStatus(data, dlc);
    return;
  }

  if ((CAN_MC_TELEMETRY_STD_ID != 0U) && (std_id == CAN_MC_TELEMETRY_STD_ID))
  {
    CAN_Vehicle_ParseMcTelemetry(data, dlc);
    return;
  }
}

void CAN_Vehicle_Init(void)
{
  CAN_FilterTypeDef filter = {0};

  (void)memset(&vehicle_state, 0, sizeof(vehicle_state));
  CAN_Vehicle_SyncDebugState();

  filter.FilterBank = CAN1_FILTER_BANK;
  filter.FilterMode = CAN_FILTERMODE_IDMASK;
  filter.FilterScale = CAN_FILTERSCALE_32BIT;
  filter.FilterIdHigh = 0x0000U;
  filter.FilterIdLow = 0x0000U;
  filter.FilterMaskIdHigh = 0x0000U;
  filter.FilterMaskIdLow = 0x0000U;
  filter.FilterFIFOAssignment = CAN_RX_FIFO0;
  filter.FilterActivation = ENABLE;
  filter.SlaveStartFilterBank = CAN2_FILTER_BANK_START;

  if (HAL_CAN_ConfigFilter(&hcan1, &filter) != HAL_OK)
  {
    return;
  }

  (void)HAL_CAN_Start(&hcan1);
}

void CAN_Vehicle_Process(void)
{
  CAN_RxHeaderTypeDef rx_header;
  uint8_t rx_data[8];

  while (HAL_CAN_GetRxFifoFillLevel(&hcan1, CAN_RX_FIFO0) > 0U)
  {
    if (HAL_CAN_GetRxMessage(&hcan1, CAN_RX_FIFO0, &rx_header, rx_data) != HAL_OK)
    {
      break;
    }

    vehicle_state.rx_total++;
    vehicle_state.last_rx_ms = HAL_GetTick();

    if (rx_header.IDE == CAN_ID_STD)
    {
      vehicle_state.rx_std++;
      CAN_Vehicle_DispatchStd((uint16_t)rx_header.StdId, rx_data, rx_header.DLC);
    }
    else
    {
      vehicle_state.rx_ext++;
      /* Extended ID도 스니핑: 하위 11bit를 slot 키로 사용 */
      CAN_Vehicle_UpdateSniff((uint16_t)(rx_header.ExtId & 0x7FFU), rx_data, rx_header.DLC);
    }

    CAN_Vehicle_SyncDebugState();
  }
}

void CAN_Vehicle_GetState(CanVehicleState_t *out)
{
  if (out != NULL)
  {
    *out = vehicle_state;
  }
}

const CanSniffSlot_t *CAN_Vehicle_GetSniffSlot(uint8_t index)
{
  if (index >= CAN_SNIFF_SLOT_COUNT)
  {
    return NULL;
  }

  return &vehicle_state.sniff[index];
}
