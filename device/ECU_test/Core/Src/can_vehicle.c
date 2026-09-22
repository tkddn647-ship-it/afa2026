/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    can_vehicle.c
  * @brief   CAN1 vehicle bus — Cascadia Motion + Orion BMS 파싱
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

static int16_t can_i16_le(const uint8_t *data, uint8_t offset)
{
  return (int16_t)((uint16_t)data[offset] | ((uint16_t)data[offset + 1U] << 8));
}

static uint16_t can_u16_le(const uint8_t *data, uint8_t offset)
{
  return (uint16_t)data[offset] | ((uint16_t)data[offset + 1U] << 8);
}

static uint16_t can_u16_be(const uint8_t *data, uint8_t offset)
{
  return (uint16_t)(((uint16_t)data[offset] << 8) | (uint16_t)data[offset + 1U]);
}

static int16_t can_i16_be(const uint8_t *data, uint8_t offset)
{
  return (int16_t)can_u16_be(data, offset);
}

static float can_temp_c(const uint8_t *data, uint8_t offset)
{
  return (float)can_i16_le(data, offset) * 0.1f;
}

static float can_current_a(const uint8_t *data, uint8_t offset)
{
  return (float)can_i16_le(data, offset) * 0.1f;
}

static float can_voltage_v(const uint8_t *data, uint8_t offset)
{
  return (float)can_i16_le(data, offset) * 0.1f;
}

static float can_torque_nm(const uint8_t *data, uint8_t offset)
{
  return (float)can_i16_le(data, offset) * 0.1f;
}

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
  if (dlc < 2U)
  {
    return;
  }

  CAN_Vehicle_ParseOrionFailsafeWord(can_u16_le(data, 0U));
}

static void CAN_Vehicle_ParseBmsPack(const uint8_t *data, uint8_t dlc)
{
  CanBmsHvState_t *bms = &vehicle_state.bms;

  /*
   * Orion Utility 0x081 (Big Endian, Math ×1):
   *   B0 Blank
   *   B1 Pack Amphours (1B, Ah)
   *   B2-3 Pack Open Voltage (0.1V)
   *   B4-5 Pack Current (0.1A, signed)
   *   B6-7 Failsafe Status
   */
  if (dlc >= 2U)
  {
    bms->capacity_ah = (float)data[1];
  }
  if (dlc >= 4U)
  {
    bms->pack_voltage_v = (float)can_u16_be(data, 2U) * 0.1f;
  }
  if (dlc >= 6U)
  {
    bms->pack_current_a = (float)can_i16_be(data, 4U) * 0.1f;
  }
  if (dlc >= 8U)
  {
    CAN_Vehicle_ParseOrionFailsafeWord(can_u16_be(data, 6U));
  }

  bms->pack_rx_count++;
  bms->last_pack_ms = HAL_GetTick();
}

static void CAN_Vehicle_ParseBmsLimits(const uint8_t *data, uint8_t dlc)
{
  CanBmsHvState_t *bms = &vehicle_state.bms;

  /*
   * Orion Utility 0x082 (Big Endian, Math ×1):
   *   B0 High Temperature (°C, int8)
   *   B1 High Thermistor ID
   *   B2 Low Temperature (°C, int8)
   *   B3 Low Thermistor ID
   *   B4-5 Pack DCL (A)
   *   B6-7 Pack CCL (A)
   */
  if (dlc >= 1U)
  {
    bms->temp_high_c = (float)((int8_t)data[0]);
  }
  if (dlc >= 2U)
  {
    bms->temp_high_id = data[1];
  }
  if (dlc >= 3U)
  {
    bms->temp_low_c = (float)((int8_t)data[2]);
  }
  if (dlc >= 4U)
  {
    bms->temp_low_id = data[3];
  }
  if (dlc >= 6U)
  {
    bms->discharge_limit_a = (float)can_u16_be(data, 4U);
  }
  if (dlc >= 8U)
  {
    bms->charge_limit_a = (float)can_u16_be(data, 6U);
  }

  bms->limits_rx_count++;
  bms->last_limits_ms = HAL_GetTick();
}

static void CAN_Vehicle_ParseInvTempIgbt(const uint8_t *data, uint8_t dlc)
{
  CanMotorState_t *mc = &vehicle_state.motor;

  if (dlc >= 2U)
  {
    mc->igbt_a_c = can_temp_c(data, 0U);
  }
  if (dlc >= 4U)
  {
    mc->igbt_b_c = can_temp_c(data, 2U);
  }
  if (dlc >= 6U)
  {
    mc->igbt_c_c = can_temp_c(data, 4U);
  }
  if (dlc >= 8U)
  {
    mc->gate_driver_c = can_temp_c(data, 6U);
  }

  mc->temp_igbt_rx_count++;
  mc->last_rx_ms = HAL_GetTick();
}

static void CAN_Vehicle_ParseInvTempMotor(const uint8_t *data, uint8_t dlc)
{
  CanMotorState_t *mc = &vehicle_state.motor;

  if (dlc >= 2U)
  {
    mc->coolant_c = can_temp_c(data, 0U);
  }
  if (dlc >= 4U)
  {
    mc->hotspot_c = can_temp_c(data, 2U);
  }
  if (dlc >= 6U)
  {
    mc->motor_temp_c = can_temp_c(data, 4U);
  }
  if (dlc >= 8U)
  {
    mc->shudder_torque_nm = can_torque_nm(data, 6U);
  }

  mc->temp_motor_rx_count++;
  mc->last_rx_ms = HAL_GetTick();
}

static void CAN_Vehicle_ParseInvFlux(const uint8_t *data, uint8_t dlc)
{
  CanMotorState_t *mc = &vehicle_state.motor;

  /* 0x0A8: Flux cmd/fb + Id/Iq feedback (Current ×10) */
  if (dlc >= 6U)
  {
    mc->id_feedback_a = can_current_a(data, 4U);
  }
  if (dlc >= 8U)
  {
    mc->iq_feedback_a = can_current_a(data, 6U);
  }

  mc->flux_rx_count++;
  mc->last_rx_ms = HAL_GetTick();
}

static void CAN_Vehicle_ParseInvTorque(const uint8_t *data, uint8_t dlc)
{
  CanMotorState_t *mc = &vehicle_state.motor;

  if (dlc >= 2U)
  {
    mc->torque_commanded_nm = can_torque_nm(data, 0U);
  }
  if (dlc >= 4U)
  {
    mc->torque_feedback_nm = can_torque_nm(data, 2U);
  }

  mc->torque_rx_count++;
  mc->last_rx_ms = HAL_GetTick();
}

static void CAN_Vehicle_ParseInvIdIqCmd(const uint8_t *data, uint8_t dlc)
{
  CanMotorState_t *mc = &vehicle_state.motor;

  if (dlc >= 6U)
  {
    mc->id_command_a = can_current_a(data, 4U);
  }
  if (dlc >= 8U)
  {
    mc->iq_command_a = can_current_a(data, 6U);
  }

  mc->idiq_cmd_rx_count++;
  mc->last_rx_ms = HAL_GetTick();
}

static void CAN_Vehicle_ParseInvMotor(const uint8_t *data, uint8_t dlc)
{
  CanMotorState_t *mc = &vehicle_state.motor;

  if (dlc >= 2U)
  {
    mc->motor_angle_deg = (float)can_i16_le(data, 0U) * 0.1f;
  }
  if (dlc >= 4U)
  {
    mc->rpm = (float)can_i16_le(data, 2U);
  }
  if (dlc >= 6U)
  {
    mc->electrical_freq_hz = (float)can_i16_le(data, 4U) * 0.1f;
  }

  mc->motor_rx_count++;
  mc->last_rx_ms = HAL_GetTick();
}

static void CAN_Vehicle_ParseInvCurrent(const uint8_t *data, uint8_t dlc)
{
  CanMotorState_t *mc = &vehicle_state.motor;

  if (dlc >= 2U)
  {
    mc->phase_a_a = can_current_a(data, 0U);
  }
  if (dlc >= 4U)
  {
    mc->phase_b_a = can_current_a(data, 2U);
  }
  if (dlc >= 6U)
  {
    mc->phase_c_a = can_current_a(data, 4U);
  }
  if (dlc >= 8U)
  {
    mc->dc_bus_current_a = can_current_a(data, 6U);
  }

  mc->current_rx_count++;
  mc->last_rx_ms = HAL_GetTick();
}

static void CAN_Vehicle_ParseInvVoltage(const uint8_t *data, uint8_t dlc)
{
  CanMotorState_t *mc = &vehicle_state.motor;

  if (dlc >= 2U)
  {
    mc->dc_bus_voltage_v = can_voltage_v(data, 0U);
  }
  if (dlc >= 4U)
  {
    mc->output_voltage_v = can_voltage_v(data, 2U);
  }

  mc->voltage_rx_count++;
  mc->last_rx_ms = HAL_GetTick();
}

static void CAN_Vehicle_UpdateMotorStatusFromVsm(uint8_t vsm_state)
{
  CanMotorState_t *mc = &vehicle_state.motor;

  mc->vsm_state = vsm_state;

  /*
   * Cascadia VSM state (0x0AA Byte0) — precharge / main contactor 추정.
   * Torque/Speed(Run Mode)는 Byte4 에서 별도 파싱.
   */
  switch (vsm_state)
  {
    case 0U:
      mc->precharge_active = 0U;
      mc->main_contactor_closed = 0U;
      break;
    case 1U:
      mc->precharge_active = 1U;
      mc->main_contactor_closed = 0U;
      break;
    case 2U:
      mc->precharge_active = 0U;
      mc->main_contactor_closed = 1U;
      break;
    default:
      mc->precharge_active = (uint8_t)(vsm_state == 1U);
      mc->main_contactor_closed = (uint8_t)(vsm_state >= 2U);
      break;
  }
}

static void CAN_Vehicle_ParseInvStatus(const uint8_t *data, uint8_t dlc)
{
  CanMotorState_t *mc = &vehicle_state.motor;

  if (dlc >= 1U)
  {
    CAN_Vehicle_UpdateMotorStatusFromVsm(data[0]);
  }
  if (dlc >= 3U)
  {
    mc->inverter_state = data[2];
  }
  /* Cascadia 0x0AA Byte4 Bit0: Inverter Run Mode — 0=Torque, 1=Speed */
  if (dlc >= 5U)
  {
    mc->inverter_mode = (uint8_t)(data[4] & 0x01U);
  }
  if (dlc >= 7U)
  {
    mc->inverter_enabled = (uint8_t)((data[6] & 0x01U) != 0U);
  }

  mc->status_rx_count++;
  mc->last_rx_ms = HAL_GetTick();
}

static void CAN_Vehicle_ParseInvLimits(const uint8_t *data, uint8_t dlc)
{
  CanBmsHvState_t *bms = &vehicle_state.bms;

  if (dlc >= 2U)
  {
    bms->discharge_limit_a = (float)can_u16_le(data, 0U);
  }
  if (dlc >= 4U)
  {
    bms->charge_limit_a = (float)can_u16_le(data, 2U);
  }

  vehicle_state.motor.limits_rx_count++;
  vehicle_state.motor.last_rx_ms = HAL_GetTick();
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
  switch (std_id)
  {
    case CAN_INV_TEMP_IGBT_STD_ID:
      CAN_Vehicle_ParseInvTempIgbt(data, dlc);
      return;

    case CAN_INV_TEMP_MOTOR_STD_ID:
      CAN_Vehicle_ParseInvTempMotor(data, dlc);
      return;

    case CAN_INV_MOTOR_STD_ID:
      CAN_Vehicle_ParseInvMotor(data, dlc);
      return;

    case CAN_INV_CURRENT_STD_ID:
      CAN_Vehicle_ParseInvCurrent(data, dlc);
      return;

    case CAN_INV_VOLTAGE_STD_ID:
      CAN_Vehicle_ParseInvVoltage(data, dlc);
      return;

    case CAN_INV_FLUX_STD_ID:
      CAN_Vehicle_ParseInvFlux(data, dlc);
      return;

    case CAN_INV_STATUS_STD_ID:
      CAN_Vehicle_ParseInvStatus(data, dlc);
      return;

    case CAN_INV_TORQUE_STD_ID:
      CAN_Vehicle_ParseInvTorque(data, dlc);
      return;

    case CAN_INV_IDIQ_CMD_STD_ID:
      CAN_Vehicle_ParseInvIdIqCmd(data, dlc);
      return;

    case CAN_INV_LIMITS_STD_ID:
      CAN_Vehicle_ParseInvLimits(data, dlc);
      return;

    case CAN_BMS_PACK_STD_ID:
      CAN_Vehicle_ParseBmsPack(data, dlc);
      return;

    case CAN_BMS_LIMITS_STD_ID:
      CAN_Vehicle_ParseBmsLimits(data, dlc);
      return;

    default:
      break;
  }

  if ((CAN_BMS_FAILSAFE_STD_ID != 0U) && (std_id == CAN_BMS_FAILSAFE_STD_ID))
  {
    CAN_Vehicle_ParseBmsFailsafe(data, dlc);
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

float CAN_Vehicle_GetIgbtMaxC(void)
{
  const CanMotorState_t *mc = &vehicle_state.motor;
  float max_c = mc->igbt_a_c;

  if (mc->igbt_b_c > max_c)
  {
    max_c = mc->igbt_b_c;
  }
  if (mc->igbt_c_c > max_c)
  {
    max_c = mc->igbt_c_c;
  }

  return max_c;
}
