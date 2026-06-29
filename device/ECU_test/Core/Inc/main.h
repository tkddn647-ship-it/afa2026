/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.h
  * @brief          : Header for main.c file.
  *                   This file contains the common defines of the application.
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2026 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */

/* Define to prevent recursive inclusion -------------------------------------*/
#ifndef __MAIN_H
#define __MAIN_H

#ifdef __cplusplus
extern "C" {
#endif

/* Includes ------------------------------------------------------------------*/
#include "stm32f4xx_hal.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */

/* USER CODE END Includes */

/* Exported types ------------------------------------------------------------*/
/* USER CODE BEGIN ET */

/* USER CODE END ET */

/* Exported constants --------------------------------------------------------*/
/* USER CODE BEGIN EC */

/* USER CODE END EC */

/* Exported macro ------------------------------------------------------------*/
/* USER CODE BEGIN EM */

/* USER CODE END EM */

/* Exported functions prototypes ---------------------------------------------*/
void Error_Handler(void);

/* USER CODE BEGIN EFP */

/* USER CODE END EFP */

/* Private defines -----------------------------------------------------------*/
#define Raspberry_pi_5_to_RX_Pin GPIO_PIN_10
#define Raspberry_pi_5_to_RX_GPIO_Port GPIOC
#define Raspberry_pi_5_to_TX_Pin GPIO_PIN_11
#define Raspberry_pi_5_to_TX_GPIO_Port GPIOC
#define Uno_to_RX_Pin GPIO_PIN_5
#define Uno_to_RX_GPIO_Port GPIOD
#define Uno_to_TX_Pin GPIO_PIN_6
#define Uno_to_TX_GPIO_Port GPIOD

/* USER CODE BEGIN Private defines */
#define LIS3DSH_CS_Pin GPIO_PIN_3
#define LIS3DSH_CS_GPIO_Port GPIOE

/* Discovery 파란 버튼 B1 = PA0 (CubeMX GPIO_INPUT, 폴링) */
#define BOARD_STM32F407G_DISCOVERY  1U

#define USER_BTN_Pin GPIO_PIN_0
#define USER_BTN_GPIO_Port GPIOA
#define USER_BTN_GPIO_CLK_ENABLE() __HAL_RCC_GPIOA_CLK_ENABLE()

#define USER_BUTTON_ENABLE 1U
#define USER_BUTTON_BOOT_GUARD_MS 2000U
/* USER CODE END Private defines */

#ifdef __cplusplus
}
#endif

#endif /* __MAIN_H */
