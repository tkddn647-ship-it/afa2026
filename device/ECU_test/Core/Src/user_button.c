/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    user_button.c
  * @brief   Discovery USER 버튼 (PA0) — 메인 루프 폴링, LWS 영점 캘리 (비블로킹)
  *
  * 인터럽트/ HAL_Delay 사용 안 함 (누르면 전체 데이터 멈춤 방지).
  ******************************************************************************
  */
/* USER CODE END Header */

#include "user_button.h"
#include "can_lws.h"
#include "main.h"
#include "usart.h"
#include <string.h>

#define USER_BTN_DEBOUNCE_MS     40U
#define USER_BTN_CAL_GAP_MS      50U

volatile uint32_t user_button_press_count = 0U;
volatile uint32_t user_button_cal_request_count = 0U;

typedef enum
{
  USER_BTN_CAL_IDLE = 0,
  USER_BTN_CAL_WAIT_ZERO
} UserButtonCalState_t;

static UserButtonCalState_t cal_state = USER_BTN_CAL_IDLE;
static uint32_t cal_step_ms = 0U;
static uint8_t stable_released = 1U;
static uint8_t last_sample_pressed = 0U;
static uint32_t last_change_ms = 0U;

static uint8_t UserButton_IsPressed(void)
{
  return (HAL_GPIO_ReadPin(USER_BTN_GPIO_Port, USER_BTN_Pin) == GPIO_PIN_RESET) ? 1U : 0U;
}

static void UserButton_NotifyCal(uint8_t ok)
{
  static const char ok_msg[] = "LWS_CAL,OK\n";
  static const char fail_msg[] = "LWS_CAL,FAIL\n";
  const char *msg = ok ? ok_msg : fail_msg;
  uint16_t len = (uint16_t)strlen(msg);

  (void)HAL_UART_Transmit(&huart4, (const uint8_t *)msg, len, 30U);
}

static void UserButton_StartCalibrate(void)
{
  if (CAN_LWS_SendConfigCcw(CAN_LWS_CCW_RESET) != HAL_OK)
  {
    UserButton_NotifyCal(0U);
    return;
  }

  cal_state = USER_BTN_CAL_WAIT_ZERO;
  cal_step_ms = HAL_GetTick();
  user_button_cal_request_count++;
}

void UserButton_Init(void)
{
#if USER_BUTTON_ENABLE
  /* PA0: CubeMX MX_GPIO_Init() 에서 GPIO_INPUT + GPIOA 클럭 설정됨 */
#endif
}

void UserButton_Process(void)
{
#if USER_BUTTON_ENABLE
  uint32_t now = HAL_GetTick();
  uint8_t pressed_now;

  if (cal_state == USER_BTN_CAL_WAIT_ZERO)
  {
    if ((now - cal_step_ms) >= USER_BTN_CAL_GAP_MS)
    {
      uint8_t ok = (CAN_LWS_SendConfigCcw(CAN_LWS_CCW_ZERO) == HAL_OK) ? 1U : 0U;
      UserButton_NotifyCal(ok);
      cal_state = USER_BTN_CAL_IDLE;
    }
    return;
  }

  if (now < USER_BUTTON_BOOT_GUARD_MS)
  {
    return;
  }

  pressed_now = UserButton_IsPressed();

  if (pressed_now != last_sample_pressed)
  {
    last_change_ms = now;
    last_sample_pressed = pressed_now;
  }

  if ((now - last_change_ms) < USER_BTN_DEBOUNCE_MS)
  {
    return;
  }

  if ((stable_released != 0U) && (pressed_now != 0U))
  {
    stable_released = 0U;
    user_button_press_count++;
    UserButton_StartCalibrate();
  }
  else if ((stable_released == 0U) && (pressed_now == 0U))
  {
    stable_released = 1U;
  }
#endif
}
