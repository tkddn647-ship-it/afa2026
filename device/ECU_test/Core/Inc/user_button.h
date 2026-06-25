/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    user_button.h
  * @brief   보드 USER 버튼(파란 버튼) — LWS 조향각 영점 캘리브레이션
  ******************************************************************************
  */
/* USER CODE END Header */

#ifndef __USER_BUTTON_H__
#define __USER_BUTTON_H__

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

extern volatile uint32_t user_button_press_count;
extern volatile uint32_t user_button_cal_request_count;

void UserButton_Init(void);
void UserButton_Process(void);

#ifdef __cplusplus
}
#endif

#endif /* __USER_BUTTON_H__ */
