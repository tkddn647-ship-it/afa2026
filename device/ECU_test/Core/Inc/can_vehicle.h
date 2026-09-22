/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    can_vehicle.h
  * @brief   CAN1 @ 250 kbps — BMS(HV) + Cascadia Motion 인버터 버스
  ******************************************************************************
  */
/* USER CODE END Header */

#ifndef __CAN_VEHICLE_H__
#define __CAN_VEHICLE_H__

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>
#include "stm32f4xx_hal.h"

#define CAN_VEHICLE_BITRATE_KBPS  250U
#define CAN_SNIFF_SLOT_COUNT      16U

/* Cascadia Motion (CAN Protocol V5_9) */
#define CAN_INV_TEMP_IGBT_STD_ID    0x0A0U
#define CAN_INV_TEMP_MOTOR_STD_ID   0x0A2U /* + Torque Shudder @ B6-7 */
#define CAN_INV_MOTOR_STD_ID        0x0A5U
#define CAN_INV_CURRENT_STD_ID      0x0A6U
#define CAN_INV_VOLTAGE_STD_ID      0x0A7U
#define CAN_INV_FLUX_STD_ID         0x0A8U /* Id/Iq feedback */
#define CAN_INV_STATUS_STD_ID       0x0AAU
#define CAN_INV_TORQUE_STD_ID       0x0ACU /* Commanded / Feedback torque */
#define CAN_INV_IDIQ_CMD_STD_ID     0x0ADU /* Id/Iq command */
#define CAN_INV_LIMITS_STD_ID       0x202U

/*
 * Orion BMS Utility 커스텀 (Big Endian, Math ×1):
 * 0x81: Amphours / Open V / Current / Failsafe
 * 0x82: High/Low temp+id / DCL / CCL
 */
#define CAN_BMS_PACK_STD_ID         0x081U
#define CAN_BMS_LIMITS_STD_ID       0x082U
#define CAN_BMS_FAILSAFE_STD_ID     0x000U

typedef struct
{
  uint8_t voltage_failsafe;
  uint8_t current_failsafe;
  uint8_t relay_failsafe;
  uint8_t cell_balancing;
  uint8_t interlock_failsafe;
  uint8_t thermistor_error;
  uint8_t input_power_failsafe;
  uint16_t failsafe_raw;

  float soc_pct;
  float capacity_ah;
  float pack_voltage_v;
  float pack_current_a;
  float charge_limit_a;
  float discharge_limit_a;
  float temp_high_c;
  uint8_t temp_high_id;
  float temp_low_c;
  uint8_t temp_low_id;
  float temp_internal_c;

  uint32_t failsafe_rx_count;
  uint32_t pack_rx_count;
  uint32_t limits_rx_count;
  uint32_t last_failsafe_ms;
  uint32_t last_pack_ms;
  uint32_t last_limits_ms;
} CanBmsHvState_t;

typedef struct
{
  float igbt_a_c;
  float igbt_b_c;
  float igbt_c_c;
  float gate_driver_c;
  float coolant_c;
  float hotspot_c;
  float motor_temp_c;
  float shudder_torque_nm;

  float motor_angle_deg;
  float rpm;
  float electrical_freq_hz;

  float phase_a_a;
  float phase_b_a;
  float phase_c_a;
  float dc_bus_current_a;

  float dc_bus_voltage_v;
  float output_voltage_v;

  float id_feedback_a;
  float iq_feedback_a;
  float id_command_a;
  float iq_command_a;
  float torque_commanded_nm;
  float torque_feedback_nm;

  uint8_t vsm_state;
  uint8_t inverter_state;
  uint8_t inverter_enabled;
  uint8_t precharge_active;
  uint8_t main_contactor_closed;
  uint8_t inverter_mode; /* Cascadia Run Mode: 0=Torque, 1=Speed */

  uint32_t temp_igbt_rx_count;
  uint32_t temp_motor_rx_count;
  uint32_t motor_rx_count;
  uint32_t current_rx_count;
  uint32_t voltage_rx_count;
  uint32_t flux_rx_count;
  uint32_t torque_rx_count;
  uint32_t idiq_cmd_rx_count;
  uint32_t status_rx_count;
  uint32_t limits_rx_count;
  uint32_t last_rx_ms;
} CanMotorState_t;

typedef struct
{
  uint16_t std_id;
  uint32_t hit_count;
  uint8_t dlc;
  uint8_t data[8];
  uint32_t last_ms;
} CanSniffSlot_t;

typedef struct
{
  uint32_t rx_total;
  uint32_t rx_std;
  uint32_t rx_ext;
  uint32_t last_rx_ms;
  CanBmsHvState_t bms;
  CanMotorState_t motor;
  CanSniffSlot_t sniff[CAN_SNIFF_SLOT_COUNT];
} CanVehicleState_t;

extern volatile CanVehicleState_t g_can_vehicle_state;

void CAN_Vehicle_Init(void);
void CAN_Vehicle_Process(void);
void CAN_Vehicle_GetState(CanVehicleState_t *out);
const CanSniffSlot_t *CAN_Vehicle_GetSniffSlot(uint8_t index);

float CAN_Vehicle_GetIgbtMaxC(void);

#ifdef __cplusplus
}
#endif

#endif /* __CAN_VEHICLE_H__ */
