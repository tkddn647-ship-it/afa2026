/**
 * @brief Arduino Uno wheel speed UART (USART2 PD5/PD6, 115200)
 *        Line format: WPS,R,<pulse>,L,<pulse>\n  (20 Hz from Arduino)
 */

#include "wheel_speed_uart.h"
#include "usart.h"
#include <stdlib.h>
#include <string.h>

#define WHEEL_LINE_MAX  32U

static uint8_t rx_byte = 0U;
static char line_buf[WHEEL_LINE_MAX];
static uint8_t line_len = 0U;

static volatile float wheel_rpm_right = 0.0f;
static volatile float wheel_rpm_left = 0.0f;
static volatile uint32_t wheel_pulse_rx_lines = 0U;
static volatile uint32_t wheel_pulse_rx_errors = 0U;

static float wheel_speed_pulses_to_rpm(uint16_t pulses)
{
  if (WHEEL_TEETH_COUNT == 0U)
  {
    return 0.0f;
  }

  return ((float)pulses / (float)WHEEL_TEETH_COUNT)
         * (60000.0f / (float)WHEEL_SPEED_FLUSH_MS);
}

static void wheel_speed_restart_rx(void)
{
  (void)HAL_UART_Receive_IT(&huart2, &rx_byte, 1U);
}

static void wheel_speed_parse_line(void)
{
  const char *left_marker;

  line_buf[line_len] = '\0';

  if ((line_len >= 9U) && (strncmp(line_buf, "WPS,R,", 6) == 0))
  {
    left_marker = strstr(&line_buf[6], ",L,");
    if (left_marker != NULL)
    {
      const uint16_t right_pulses = (uint16_t)atoi(&line_buf[6]);
      const uint16_t left_pulses = (uint16_t)atoi(left_marker + 3);

      wheel_rpm_right = wheel_speed_pulses_to_rpm(right_pulses);
      wheel_rpm_left = wheel_speed_pulses_to_rpm(left_pulses);
      wheel_pulse_rx_lines++;
    }
    else
    {
      wheel_pulse_rx_errors++;
    }
  }
  else
  {
    wheel_pulse_rx_errors++;
  }

  line_len = 0U;
}

static void wheel_speed_rx_byte(uint8_t byte)
{
  const char c = (char)byte;

  if ((c == '\n') || (c == '\r'))
  {
    if (line_len > 0U)
    {
      wheel_speed_parse_line();
    }
    return;
  }

  if (line_len < (WHEEL_LINE_MAX - 1U))
  {
    line_buf[line_len] = c;
    line_len++;
  }
  else
  {
    line_len = 0U;
    wheel_pulse_rx_errors++;
  }
}

void WheelSpeedUart_Init(void)
{
  line_len = 0U;
  wheel_rpm_right = 0.0f;
  wheel_rpm_left = 0.0f;
  wheel_pulse_rx_lines = 0U;
  wheel_pulse_rx_errors = 0U;

  HAL_NVIC_SetPriority(USART2_IRQn, 6, 0);
  HAL_NVIC_EnableIRQ(USART2_IRQn);
  wheel_speed_restart_rx();
}

float WheelSpeedUart_GetWheelRpmRight(void)
{
  return wheel_rpm_right;
}

float WheelSpeedUart_GetWheelRpmLeft(void)
{
  return wheel_rpm_left;
}

uint32_t WheelSpeedUart_GetRxLineCount(void)
{
  return wheel_pulse_rx_lines;
}

void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart)
{
  if (huart->Instance == USART2)
  {
    wheel_speed_rx_byte(rx_byte);
    wheel_speed_restart_rx();
  }
}
