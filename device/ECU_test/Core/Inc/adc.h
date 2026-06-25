/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    adc.h
  * @brief   This file contains all the function prototypes for
  *          the adc.c file
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
#ifndef __ADC_H__
#define __ADC_H__

#ifdef __cplusplus
extern "C" {
#endif

/* Includes ------------------------------------------------------------------*/
#include "main.h"

/* USER CODE BEGIN Includes */

/* USER CODE END Includes */

extern ADC_HandleTypeDef hadc1;

/* USER CODE BEGIN Private defines */

#define ADC_LINEAR_SENSOR_COUNT  4U
#define ADC_LINEAR_STROKE_MM     100.0f
#define ADC_VREF_MV              3300U
#define ADC_MAX_RAW              4095U

typedef enum
{
  ADC_LINEAR_CH0 = 0, /* PA1 / ADC1_IN1 -> FR */
  ADC_LINEAR_CH1 = 1, /* PA2 / ADC1_IN2 -> FL */
  ADC_LINEAR_CH2 = 2, /* PA3 / ADC1_IN3 -> RR */
  ADC_LINEAR_CH3 = 3  /* PA4 / ADC1_IN4 -> RL */
} ADC_LinearChannel_t;

typedef struct
{
  uint16_t raw;
  uint16_t voltage_mv;
  float position_mm;
} ADC_LinearReading_t;

extern volatile ADC_LinearReading_t adc_linear_readings[ADC_LINEAR_SENSOR_COUNT];
extern volatile uint32_t adc_read_fail_count;
extern volatile uint16_t adc_debug_raw[ADC_LINEAR_SENSOR_COUNT];
extern volatile float adc_ecu_temp_c;
extern volatile uint16_t adc_vdda_mv;
extern volatile uint16_t adc_mcu_temp_raw;

/* USER CODE END Private defines */

void MX_ADC1_Init(void);

/* USER CODE BEGIN Prototypes */

void ADC_LinearSensor_Init(void);
uint16_t ADC_ReadRaw(ADC_LinearChannel_t channel);
uint16_t ADC_RawToVoltageMv(uint16_t raw);
float ADC_RawToPositionMm(uint16_t raw);
void ADC_ReadLinearSensor(ADC_LinearChannel_t channel, volatile ADC_LinearReading_t *reading);
void ADC_ReadAllLinearSensors(void);
float ADC_ReadMcuTempC(void); /* STM32 칩 내부(다이) 온도 °C, VREFINT 보정 */

/* USER CODE END Prototypes */

#ifdef __cplusplus
}
#endif

#endif /* __ADC_H__ */

