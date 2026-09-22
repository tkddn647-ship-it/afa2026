/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    sensor_uart.c
 * @brief   STM32 센서 100 Hz 버퍼링, 20 Hz UART4(DMA) 전송
 *
 * ADC 채널 매핑 (채널별 풀스케일 ~1.82–1.84V = 100mm):
 *   PA1/IN1 -> FR, PA2/IN2 -> RR, PA3/IN3 -> RL, PA4/IN4 -> FL
 *   PA0 = Discovery USER 버튼 (ADC 사용 안 함)
 *
 * UART4 보드레이트: 921600 (Pi 와 동일), TX는 DMA 논블로킹 / flush 20 Hz (5샘플)
 * UART 한 줄 (Pi Uart_stm.py 호환):
 *   timestamp_ms,FR,FL,RR,RL,x_g,y_g,z_g,ecu_temp,steering_angle,steering_speed,
 *   inv_temp_igt,inv_temp_motor,inv_motor_speed,inv_dc_current,inv_voltage,
 *   motor_precharge,motor_main_contactor,motor_inverter_mode(0=Torque/1=Speed),
 *   inv_shudder_torque,inv_id_feedback,inv_iq_feedback,
 *   inv_torque_commanded,inv_torque_feedback,inv_id_command,inv_iq_command,
 *   bms_charge,bms_voltage,bms_current,bms_ccl,bms_dcl,bms_temp_max,bms_capacity,
 *   can1,can2 (1=최근 CAN 수신 / 0=끊김)
  ******************************************************************************
  */
/* USER CODE END Header */

#include "sensor_uart.h"
#include "adc.h"
#include "can_vehicle.h"
#include "can_lws.h"
#include "lis3dsh.h"
#include "uart4_tx.h"
#include "usart.h"
#include <stdio.h>
#include <string.h>

#define SENSOR_SAMPLE_INTERVAL_MS  10U
#define SENSOR_FLUSH_INTERVAL_MS   (1000U / SENSOR_FLUSH_RATE_HZ)
#define CAN_LINK_TIMEOUT_MS        500U

/* CAN1 없이 UART→Pi→서버 경로 디버그: 1이면 RPM을 강제로 1 전송 */
#define SENSOR_DEBUG_FORCE_MOTOR_RPM  0
#define SENSOR_DEBUG_MOTOR_RPM_VALUE  1.0f

#define LINEAR_IDX_FR  0U
#define LINEAR_IDX_RR  1U
#define LINEAR_IDX_RL  2U
#define LINEAR_IDX_FL  3U

SensorSample_t data[SENSOR_BATCH_SIZE];
volatile uint8_t data_count = 0U;
volatile uint32_t sensor_uart_tx_lines = 0U;
volatile uint32_t sensor_uart_overflow = 0U;

static uint32_t last_sample_ms = 0U;
static uint32_t last_flush_ms = 0U;
static uint32_t last_heartbeat_ms = 0U;

#define SENSOR_HEARTBEAT_MS  500U
#define ECU_TEMP_UPDATE_MS     200U

static float cached_ecu_temp_c = 0.0f;
static uint32_t last_ecu_temp_ms = 0U;

static int32_t float_to_fixed(float value, int32_t scale)
{
  if (value >= 0.0f)
  {
    return (int32_t)(value * (float)scale + 0.5f);
  }
  return (int32_t)(value * (float)scale - 0.5f);
}

static int append_mm(char *buf, int pos, int cap, int32_t centi_mm)
{
  int32_t whole;
  int32_t frac;
  char sign = '\0';

  if (centi_mm < 0)
  {
    sign = '-';
    centi_mm = -centi_mm;
  }

  whole = centi_mm / 100;
  frac = centi_mm % 100;

  if (sign != '\0')
  {
    pos += snprintf(&buf[pos], (size_t)(cap - pos), "%c", sign);
  }
  pos += snprintf(&buf[pos], (size_t)(cap - pos), "%ld.%02ld", (long)whole, (long)frac);
  return pos;
}

static int append_g(char *buf, int pos, int cap, int32_t milli_g)
{
  int32_t whole;
  int32_t frac;
  char sign = '\0';

  if (milli_g < 0)
  {
    sign = '-';
    milli_g = -milli_g;
  }

  whole = milli_g / 1000;
  frac = milli_g % 1000;

  if (sign != '\0')
  {
    pos += snprintf(&buf[pos], (size_t)(cap - pos), "%c", sign);
  }
  pos += snprintf(&buf[pos], (size_t)(cap - pos), "%ld.%03ld", (long)whole, (long)frac);
  return pos;
}

static int append_int(char *buf, int pos, int cap, int32_t value)
{
  return pos + snprintf(&buf[pos], (size_t)(cap - pos), "%ld", (long)value);
}

static int append_temp_c(char *buf, int pos, int cap, int32_t deci_c)
{
  int32_t whole;
  int32_t frac;
  char sign = '\0';

  if (deci_c < 0)
  {
    sign = '-';
    deci_c = -deci_c;
  }

  whole = deci_c / 10;
  frac = deci_c % 10;

  if (sign != '\0')
  {
    pos += snprintf(&buf[pos], (size_t)(cap - pos), "%c", sign);
  }
  pos += snprintf(&buf[pos], (size_t)(cap - pos), "%ld.%ld", (long)whole, (long)frac);
  return pos;
}

static int SensorUart_FormatSample(const SensorSample_t *sample, char *buf, int cap)
{
  int pos = 0;

  if ((sample == NULL) || (buf == NULL) || (cap <= 0))
  {
    return 0;
  }

  pos += snprintf(buf, (size_t)cap, "%lu,", (unsigned long)sample->timestamp_ms);
  pos = append_mm(buf, pos, cap, float_to_fixed(sample->FR, 100));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_mm(buf, pos, cap, float_to_fixed(sample->FL, 100));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_mm(buf, pos, cap, float_to_fixed(sample->RR, 100));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_mm(buf, pos, cap, float_to_fixed(sample->RL, 100));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_g(buf, pos, cap, float_to_fixed(sample->x_g, 1000));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_g(buf, pos, cap, float_to_fixed(sample->y_g, 1000));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_g(buf, pos, cap, float_to_fixed(sample->z_g, 1000));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_temp_c(buf, pos, cap, float_to_fixed(sample->ecu_temp_c, 10));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_temp_c(buf, pos, cap, float_to_fixed(sample->steering_angle_deg, 10));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos += snprintf(&buf[pos], (size_t)(cap - pos), "%ld", (long)float_to_fixed(sample->steering_speed_dps, 1));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_temp_c(buf, pos, cap, float_to_fixed(sample->inv_temp_igt_c, 10));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_temp_c(buf, pos, cap, float_to_fixed(sample->inv_temp_motor_c, 10));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_int(buf, pos, cap, float_to_fixed(sample->inv_motor_speed_rpm, 1));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_temp_c(buf, pos, cap, float_to_fixed(sample->inv_dc_current_a, 10));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_temp_c(buf, pos, cap, float_to_fixed(sample->inv_voltage_v, 10));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_int(buf, pos, cap, (int32_t)sample->motor_precharge);
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_int(buf, pos, cap, (int32_t)sample->motor_main_contactor);
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_int(buf, pos, cap, (int32_t)sample->motor_inverter_mode);
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_temp_c(buf, pos, cap, float_to_fixed(sample->inv_shudder_torque_nm, 10));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_temp_c(buf, pos, cap, float_to_fixed(sample->inv_id_feedback_a, 10));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_temp_c(buf, pos, cap, float_to_fixed(sample->inv_iq_feedback_a, 10));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_temp_c(buf, pos, cap, float_to_fixed(sample->inv_torque_commanded_nm, 10));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_temp_c(buf, pos, cap, float_to_fixed(sample->inv_torque_feedback_nm, 10));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_temp_c(buf, pos, cap, float_to_fixed(sample->inv_id_command_a, 10));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_temp_c(buf, pos, cap, float_to_fixed(sample->inv_iq_command_a, 10));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_temp_c(buf, pos, cap, float_to_fixed(sample->bms_charge_pct, 10));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_temp_c(buf, pos, cap, float_to_fixed(sample->bms_voltage_v, 10));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_temp_c(buf, pos, cap, float_to_fixed(sample->bms_current_a, 10));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_int(buf, pos, cap, float_to_fixed(sample->bms_ccl_a, 1));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_int(buf, pos, cap, float_to_fixed(sample->bms_dcl_a, 1));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_temp_c(buf, pos, cap, float_to_fixed(sample->bms_temp_max_c, 10));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_temp_c(buf, pos, cap, float_to_fixed(sample->bms_capacity_ah, 10));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_int(buf, pos, cap, (int32_t)sample->can1_link);
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos = append_int(buf, pos, cap, (int32_t)sample->can2_link);
  if ((pos < cap) && (pos > 0))
  {
    buf[pos++] = '\n';
    buf[pos] = '\0';
  }

  return pos;
}

static void SensorUart_FillCanFields(SensorSample_t *sample)
{
  CanVehicleState_t can_state;
  CanLwsState_t lws;
  uint32_t now;

  if (sample == NULL)
  {
    return;
  }

  now = HAL_GetTick();
  CAN_Vehicle_GetState(&can_state);
  CAN_LWS_GetState(&lws);

  sample->inv_temp_igt_c = CAN_Vehicle_GetIgbtMaxC();
  sample->inv_temp_gatedriver_c = can_state.motor.gate_driver_c;
  sample->inv_temp_coolant_c = can_state.motor.coolant_c;
  sample->inv_temp_hotspot_c = can_state.motor.hotspot_c;
  sample->inv_temp_motor_c = can_state.motor.motor_temp_c;
  sample->inv_motor_speed_rpm = can_state.motor.rpm;
  sample->inv_motor_angle_deg = can_state.motor.motor_angle_deg;
  sample->inv_dc_current_a = can_state.motor.dc_bus_current_a;
  sample->inv_a_current_a = can_state.motor.phase_a_a;
  sample->inv_b_current_a = can_state.motor.phase_b_a;
  sample->inv_c_current_a = can_state.motor.phase_c_a;
  sample->inv_voltage_v = can_state.motor.dc_bus_voltage_v;
  sample->inv_voltage_output_v = can_state.motor.output_voltage_v;
  sample->motor_precharge = can_state.motor.precharge_active;
  sample->motor_main_contactor = can_state.motor.main_contactor_closed;
  sample->motor_inverter_mode = can_state.motor.inverter_mode;
  sample->inv_shudder_torque_nm = can_state.motor.shudder_torque_nm;
  sample->inv_id_feedback_a = can_state.motor.id_feedback_a;
  sample->inv_iq_feedback_a = can_state.motor.iq_feedback_a;
  sample->inv_torque_commanded_nm = can_state.motor.torque_commanded_nm;
  sample->inv_torque_feedback_nm = can_state.motor.torque_feedback_nm;
  sample->inv_id_command_a = can_state.motor.id_command_a;
  sample->inv_iq_command_a = can_state.motor.iq_command_a;

  sample->bms_charge_pct = can_state.bms.soc_pct;
  sample->bms_capacity_ah = can_state.bms.capacity_ah;
  sample->bms_voltage_v = can_state.bms.pack_voltage_v;
  sample->bms_current_a = can_state.bms.pack_current_a;
  sample->bms_ccl_a = can_state.bms.charge_limit_a;
  sample->bms_dcl_a = can_state.bms.discharge_limit_a;
  sample->bms_temp_max_c = can_state.bms.temp_high_c;
  sample->bms_temp_max_id = can_state.bms.temp_high_id;
  sample->bms_temp_internal_c = can_state.bms.temp_internal_c;

  sample->can1_link =
      ((can_state.last_rx_ms != 0U) && ((now - can_state.last_rx_ms) <= CAN_LINK_TIMEOUT_MS))
          ? 1U
          : 0U;
  sample->can2_link =
      ((lws.last_rx_ms != 0U) && ((now - lws.last_rx_ms) <= CAN_LINK_TIMEOUT_MS)) ? 1U : 0U;

#if SENSOR_DEBUG_FORCE_MOTOR_RPM
  /* CAN 미연결 디버그: inv_motor_speed 자리에 고정 RPM 전송 */
  sample->inv_motor_speed_rpm = SENSOR_DEBUG_MOTOR_RPM_VALUE;
#endif
}

static void SensorUart_CaptureSample(void)
{
  SensorSample_t *sample;

  if (data_count >= SENSOR_BATCH_SIZE)
  {
    sensor_uart_overflow++;
    return;
  }

  ADC_ReadAllLinearSensors();
  if (lis3dsh_ready != 0U)
  {
    (void)LIS3DSH_ReadAccel(&lis3dsh_reading);
  }

  sample = &data[data_count];
  sample->timestamp_ms = HAL_GetTick();
  sample->FR = adc_linear_readings[LINEAR_IDX_FR].position_mm;
  sample->FL = adc_linear_readings[LINEAR_IDX_FL].position_mm;
  sample->RR = adc_linear_readings[LINEAR_IDX_RR].position_mm;
  sample->RL = adc_linear_readings[LINEAR_IDX_RL].position_mm;
  sample->x_g = lis3dsh_reading.x_g;
  sample->y_g = lis3dsh_reading.y_g;
  sample->z_g = lis3dsh_reading.z_g;
  sample->ecu_temp_c = cached_ecu_temp_c;
  {
    CanLwsState_t lws;
    CAN_LWS_GetState(&lws);
    sample->steering_angle_deg = lws.angle_deg;
    sample->steering_speed_dps = lws.speed_dps;
  }
  SensorUart_FillCanFields(sample);
  data_count++;
}

static void SensorUart_SendSample(const SensorSample_t *sample)
{
  char line[512];
  int len;

  len = SensorUart_FormatSample(sample, line, (int)sizeof(line));
  if (len <= 0)
  {
    return;
  }

  if (Uart4Tx_Write((const uint8_t *)line, (uint16_t)len) != 0U)
  {
    sensor_uart_tx_lines++;
  }
  else
  {
    sensor_uart_overflow++;
  }
}

static void SensorUart_Flush(void)
{
  uint8_t i;

  for (i = 0U; i < data_count; i++)
  {
    SensorUart_SendSample(&data[i]);
  }
  data_count = 0U;
}

void SensorUart_Init(void)
{
  static const char ready_msg[] = "STM_READY\n";

  memset(data, 0, sizeof(data));
  data_count = 0U;
  sensor_uart_tx_lines = 0U;
  sensor_uart_overflow = 0U;
  last_sample_ms = HAL_GetTick();
  last_flush_ms = HAL_GetTick();
  last_ecu_temp_ms = HAL_GetTick();
  cached_ecu_temp_c = ADC_ReadMcuTempC();
  (void)Uart4Tx_Write((const uint8_t *)ready_msg, (uint16_t)(sizeof(ready_msg) - 1U));
}

static void SensorUart_SendHeartbeat(void)
{
  char line[48];
  int len;
  CanLwsState_t lws;

  CAN_LWS_GetState(&lws);
  len = snprintf(
      line,
      sizeof(line),
      "HB,%lu,%lu,%u\n",
      (unsigned long)HAL_GetTick(),
      (unsigned long)lws.rx_count,
      (unsigned int)lws.cal);
  if (len > 0)
  {
    (void)Uart4Tx_Write((const uint8_t *)line, (uint16_t)len);
  }
}

void SensorUart_Process(void)
{
  uint32_t now = HAL_GetTick();

  CAN_LWS_Process();

  if ((now - last_heartbeat_ms) >= SENSOR_HEARTBEAT_MS)
  {
    last_heartbeat_ms = now;
    SensorUart_SendHeartbeat();
  }

  if ((now - last_ecu_temp_ms) >= ECU_TEMP_UPDATE_MS)
  {
    last_ecu_temp_ms = now;
    cached_ecu_temp_c = ADC_ReadMcuTempC();
  }

  if ((now - last_sample_ms) >= SENSOR_SAMPLE_INTERVAL_MS)
  {
    last_sample_ms = now;
    SensorUart_CaptureSample();
  }

  if (data_count >= SENSOR_BATCH_SIZE)
  {
    SensorUart_Flush();
    last_flush_ms = now;
    return;
  }

  if ((now - last_flush_ms) >= SENSOR_FLUSH_INTERVAL_MS)
  {
    last_flush_ms = now;
    SensorUart_Flush();
  }
}
