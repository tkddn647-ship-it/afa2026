/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    sensor_uart.c
  * @brief   STM32 센서 200 Hz 버퍼링, 20 Hz UART4 전송
  *
 * ADC 채널 매핑:
 *   PA1/IN1 -> FR, PA2/IN2 -> FL, PA3/IN3 -> RR, PA4/IN4 -> RL
 *   PA0 = Discovery USER 버튼 (ADC 사용 안 함)
  *
 * UART4 보드레이트: 460800 (Pi 와 동일)
 * UART 한 줄 (Pi Uart_stm.py 호환):
 *   timestamp_ms,FR,FL,RR,RL,x_g,y_g,z_g,ecu_temp,steering_angle,steering_speed,wheel_rpm_right,wheel_rpm_left
 *   ecu_temp = STM32 칩 내부(다이) 온도(°C). steering_* = Bosch LWS (CAN2).
  ******************************************************************************
  */
/* USER CODE END Header */

#include "sensor_uart.h"
#include "adc.h"
#include "can_lws.h"
#include "lis3dsh.h"
#include "usart.h"
#include "wheel_speed_uart.h"
#include <stdio.h>
#include <string.h>

#define SENSOR_SAMPLE_INTERVAL_MS  5U
#define SENSOR_FLUSH_INTERVAL_MS   50U
#define SENSOR_UART_TIMEOUT_MS     30U

#define LINEAR_IDX_FR  0U
#define LINEAR_IDX_FL  1U
#define LINEAR_IDX_RR  2U
#define LINEAR_IDX_RL  3U

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
  pos += snprintf(&buf[pos], (size_t)(cap - pos), "%ld", (long)float_to_fixed(sample->wheel_rpm_right, 10));
  if (pos < cap)
  {
    buf[pos++] = ',';
  }
  pos += snprintf(&buf[pos], (size_t)(cap - pos), "%ld", (long)float_to_fixed(sample->wheel_rpm_left, 10));
  if ((pos < cap) && (pos > 0))
  {
    buf[pos++] = '\n';
    buf[pos] = '\0';
  }

  return pos;
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
  sample->wheel_rpm_right = WheelSpeedUart_GetWheelRpmRight();
  sample->wheel_rpm_left = WheelSpeedUart_GetWheelRpmLeft();
  data_count++;
}

static void SensorUart_SendSample(const SensorSample_t *sample)
{
  char line[144];
  int len;

  len = SensorUart_FormatSample(sample, line, (int)sizeof(line));
  if (len <= 0)
  {
    return;
  }

  if (HAL_UART_Transmit(&huart4, (uint8_t *)line, (uint16_t)len, SENSOR_UART_TIMEOUT_MS) == HAL_OK)
  {
    sensor_uart_tx_lines++;
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
  (void)HAL_UART_Transmit(&huart4, (uint8_t *)ready_msg, (uint8_t)(sizeof(ready_msg) - 1U), 100U);
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
    (void)HAL_UART_Transmit(&huart4, (uint8_t *)line, (uint16_t)len, SENSOR_UART_TIMEOUT_MS);
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
