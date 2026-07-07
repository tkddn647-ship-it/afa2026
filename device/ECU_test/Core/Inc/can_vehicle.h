/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    can_vehicle.h
  * @brief   CAN1 @ 250 kbps — BMS(HV) + Motor Controller 버스
  *
  * ID를 모를 때: g_can_sniff_slots[] / rx_total 로 스니핑
  * ID 확정 후: 아래 CAN_*_STD_ID 매크로에 DBC 값 입력
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

/*
 * Orion BMS Utility → Export DBC 에서 확인한 Standard ID (0 = 미사용)
 * 작년 HV 화면: V,A,R,B,I,T,P + 잔량/용량/전압/전류/DCL/최고온도
 */
#define CAN_BMS_FAILSAFE_STD_ID     0x000U
#define CAN_BMS_TELEMETRY_STD_ID    0x000U

/*
 * Motor 화면: Precharge / Main contactor / Inverter mode + RPM/토크/온도
 */
#define CAN_MC_STATUS_STD_ID        0x000U
#define CAN_MC_TELEMETRY_STD_ID     0x000U

typedef struct
{
  /* Orion Failsafe Status word (bit0..6) — ID 확정 시 파싱 */
  uint8_t voltage_failsafe;       /* V */
  uint8_t current_failsafe;       /* A */
  uint8_t relay_failsafe;         /* R */
  uint8_t cell_balancing;         /* B */
  uint8_t interlock_failsafe;     /* I */
  uint8_t thermistor_error;       /* T */
  uint8_t input_power_failsafe;   /* P */
  uint16_t failsafe_raw;

  float soc_pct;           /* 잔량 % */
  float capacity_ah;       /* 용량 Ah */
  float pack_voltage_v;    /* 전압 V */
  float pack_current_a;    /* 전류 A */
  float discharge_limit_a; /* DCL A */
  float temp_high_c;       /* 최고 온도 °C */
  uint8_t temp_high_id;    /* 최고 온도 셀/써미스터 # */

  uint32_t failsafe_rx_count;
  uint32_t telemetry_rx_count;
  uint32_t last_failsafe_ms;
  uint32_t last_telemetry_ms;
} CanBmsHvState_t;

typedef struct
{
  uint8_t precharge_active;
  uint8_t main_contactor_closed;
  uint8_t inverter_mode;     /* 인버터 모드 raw (DBC 정의 후 enum) */
  uint16_t vsm_state;         /* VSM raw */
  uint16_t inv_state;         /* INV raw */

  float rpm;
  float torque_nm;
  float temp_motor_c;
  float temp_igbt_c;

  uint32_t status_rx_count;
  uint32_t telemetry_rx_count;
  uint32_t last_status_ms;
  uint32_t last_telemetry_ms;
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

#ifdef __cplusplus
}
#endif

#endif /* __CAN_VEHICLE_H__ */
