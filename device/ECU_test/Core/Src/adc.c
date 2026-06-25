/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    adc.c
  * @brief   This file provides code for the configuration
  *          of the ADC instances.
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
/* Includes ------------------------------------------------------------------*/
#include "adc.h"

/* USER CODE BEGIN 0 */

volatile ADC_LinearReading_t adc_linear_readings[ADC_LINEAR_SENSOR_COUNT];

static const uint32_t adc_linear_hal_channels[ADC_LINEAR_SENSOR_COUNT] =
{
  ADC_CHANNEL_1, /* FR / PA1 */
  ADC_CHANNEL_2, /* FL / PA2 */
  ADC_CHANNEL_3, /* RR / PA3 */
  ADC_CHANNEL_4  /* RL / PA4 */
};

#define ADC_LINEAR_SAMPLE_COUNT  4U
#define ADC_LINEAR_TIMEOUT_MS    100U
#define ADC_MCU_TEMP_SAMPLE_COUNT  4U
#define ADC_VREFINT_SAMPLE_COUNT   4U
#define VDDA_MV_MIN                2900U
#define VDDA_MV_MAX                3600U

volatile float adc_ecu_temp_c = 0.0f;
volatile uint16_t adc_vdda_mv = (uint16_t)TEMPSENSOR_CAL_VREFANALOG;
volatile uint16_t adc_mcu_temp_raw = 0U;

volatile uint32_t adc_read_fail_count = 0U;
volatile uint16_t adc_debug_raw[ADC_LINEAR_SENSOR_COUNT];

static void ADC_EnsureAnalogPins(void)
{
  GPIO_InitTypeDef gpio = {0};

  __HAL_RCC_GPIOA_CLK_ENABLE();
  gpio.Pin = GPIO_PIN_1 | GPIO_PIN_2 | GPIO_PIN_3 | GPIO_PIN_4;
  gpio.Mode = GPIO_MODE_ANALOG;
  gpio.Pull = GPIO_NOPULL;
  HAL_GPIO_Init(GPIOA, &gpio);
}

static uint16_t ADC_ReadHalChannel(uint32_t hal_channel)
{
  ADC_ChannelConfTypeDef sConfig = {0};
  uint16_t value = 0U;

  if ((hadc1.State & HAL_ADC_STATE_READY) == 0U)
  {
    (void)HAL_ADC_Stop(&hadc1);
    hadc1.State = HAL_ADC_STATE_READY;
    hadc1.ErrorCode = HAL_ADC_ERROR_NONE;
  }

  sConfig.Channel = hal_channel;
  sConfig.Rank = 1;
  sConfig.SamplingTime = ADC_SAMPLETIME_480CYCLES;
  if (HAL_ADC_ConfigChannel(&hadc1, &sConfig) != HAL_OK)
  {
    adc_read_fail_count++;
    return 0U;
  }

  if (HAL_ADC_Start(&hadc1) != HAL_OK)
  {
    adc_read_fail_count++;
    return 0U;
  }

  if (HAL_ADC_PollForConversion(&hadc1, ADC_LINEAR_TIMEOUT_MS) != HAL_OK)
  {
    adc_read_fail_count++;
    (void)HAL_ADC_Stop(&hadc1);
    hadc1.State = HAL_ADC_STATE_READY;
    return 0U;
  }

  value = (uint16_t)HAL_ADC_GetValue(&hadc1);
  (void)HAL_ADC_Stop(&hadc1);
  hadc1.State = HAL_ADC_STATE_READY;
  return value;
}

static uint16_t ADC_ReadChannelOnce(ADC_LinearChannel_t channel)
{
  if (channel >= ADC_LINEAR_SENSOR_COUNT)
  {
    return 0U;
  }

  return ADC_ReadHalChannel(adc_linear_hal_channels[channel]);
}

static uint16_t ADC_ReadInternalRawAvg(uint32_t hal_channel, uint8_t sample_count)
{
  uint32_t sum = 0U;

  if (sample_count == 0U)
  {
    return 0U;
  }

  for (uint8_t i = 0U; i < sample_count; i++)
  {
    sum += ADC_ReadHalChannel(hal_channel);
  }

  return (uint16_t)(sum / sample_count);
}

static uint16_t ADC_ReadVddaMv(void)
{
  const uint16_t vrefint_cal = *VREFINT_CAL_ADDR;
  const uint16_t vrefint_raw = ADC_ReadInternalRawAvg(ADC_CHANNEL_VREFINT, ADC_VREFINT_SAMPLE_COUNT);
  uint32_t vdda_mv;

  if ((vrefint_cal == 0U) || (vrefint_raw == 0U))
  {
    return (uint16_t)TEMPSENSOR_CAL_VREFANALOG;
  }

  vdda_mv = ((uint32_t)vrefint_cal * TEMPSENSOR_CAL_VREFANALOG) / (uint32_t)vrefint_raw;
  if (vdda_mv < VDDA_MV_MIN)
  {
    vdda_mv = VDDA_MV_MIN;
  }
  else if (vdda_mv > VDDA_MV_MAX)
  {
    vdda_mv = VDDA_MV_MAX;
  }

  return (uint16_t)vdda_mv;
}

/* USER CODE END 0 */

ADC_HandleTypeDef hadc1;

/* ADC1 init function */
void MX_ADC1_Init(void)
{

  /* USER CODE BEGIN ADC1_Init 0 */

  /* USER CODE END ADC1_Init 0 */

  ADC_ChannelConfTypeDef sConfig = {0};

  /* USER CODE BEGIN ADC1_Init 1 */

  /* USER CODE END ADC1_Init 1 */

  /** Configure the global features of the ADC (Clock, Resolution, Data Alignment and number of conversion)
  */
  hadc1.Instance = ADC1;
  hadc1.Init.ClockPrescaler = ADC_CLOCK_SYNC_PCLK_DIV4;
  hadc1.Init.Resolution = ADC_RESOLUTION_12B;
  hadc1.Init.ScanConvMode = DISABLE;
  hadc1.Init.ContinuousConvMode = DISABLE;
  hadc1.Init.DiscontinuousConvMode = DISABLE;
  hadc1.Init.ExternalTrigConvEdge = ADC_EXTERNALTRIGCONVEDGE_NONE;
  hadc1.Init.ExternalTrigConv = ADC_SOFTWARE_START;
  hadc1.Init.DataAlign = ADC_DATAALIGN_RIGHT;
  hadc1.Init.NbrOfConversion = 1;
  hadc1.Init.DMAContinuousRequests = DISABLE;
  hadc1.Init.EOCSelection = ADC_EOC_SINGLE_CONV;
  if (HAL_ADC_Init(&hadc1) != HAL_OK)
  {
    Error_Handler();
  }

  /** Configure for the selected ADC regular channel its corresponding rank in the sequencer and its sample time.
  */
  sConfig.Channel = ADC_CHANNEL_1;
  sConfig.Rank = 1;
  sConfig.SamplingTime = ADC_SAMPLETIME_3CYCLES;
  if (HAL_ADC_ConfigChannel(&hadc1, &sConfig) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN ADC1_Init 2 */

  /* USER CODE END ADC1_Init 2 */

}

void HAL_ADC_MspInit(ADC_HandleTypeDef* adcHandle)
{

  GPIO_InitTypeDef GPIO_InitStruct = {0};
  if(adcHandle->Instance==ADC1)
  {
  /* USER CODE BEGIN ADC1_MspInit 0 */

  /* USER CODE END ADC1_MspInit 0 */
    /* ADC1 clock enable */
    __HAL_RCC_ADC1_CLK_ENABLE();

    __HAL_RCC_GPIOA_CLK_ENABLE();
    /**ADC1 GPIO Configuration
    PA1     ------> ADC1_IN1
    PA2     ------> ADC1_IN2
    PA3     ------> ADC1_IN3
    PA4     ------> ADC1_IN4
    */
    GPIO_InitStruct.Pin = GPIO_PIN_1|GPIO_PIN_2|GPIO_PIN_3|GPIO_PIN_4;
    GPIO_InitStruct.Mode = GPIO_MODE_ANALOG;
    GPIO_InitStruct.Pull = GPIO_NOPULL;
    HAL_GPIO_Init(GPIOA, &GPIO_InitStruct);

  /* USER CODE BEGIN ADC1_MspInit 1 */

  /* USER CODE END ADC1_MspInit 1 */
  }
}

void HAL_ADC_MspDeInit(ADC_HandleTypeDef* adcHandle)
{

  if(adcHandle->Instance==ADC1)
  {
  /* USER CODE BEGIN ADC1_MspDeInit 0 */

  /* USER CODE END ADC1_MspDeInit 0 */
    /* Peripheral clock disable */
    __HAL_RCC_ADC1_CLK_DISABLE();

    /**ADC1 GPIO Configuration
    PA1     ------> ADC1_IN1
    PA2     ------> ADC1_IN2
    PA3     ------> ADC1_IN3
    PA4     ------> ADC1_IN4
    */
    HAL_GPIO_DeInit(GPIOA, GPIO_PIN_1|GPIO_PIN_2|GPIO_PIN_3|GPIO_PIN_4);

  /* USER CODE BEGIN ADC1_MspDeInit 1 */

  /* USER CODE END ADC1_MspDeInit 1 */
  }
}

/* USER CODE BEGIN 1 */

void ADC_LinearSensor_Init(void)
{
  for (uint8_t i = 0U; i < ADC_LINEAR_SENSOR_COUNT; i++)
  {
    adc_linear_readings[i].raw = 0U;
    adc_linear_readings[i].voltage_mv = 0U;
    adc_linear_readings[i].position_mm = 0.0f;
    adc_debug_raw[i] = 0U;
  }

  ADC_EnsureAnalogPins();
  ADC_ReadAllLinearSensors();
}

uint16_t ADC_ReadRaw(ADC_LinearChannel_t channel)
{
  uint32_t sum = 0U;

  if (channel >= ADC_LINEAR_SENSOR_COUNT)
  {
    return 0U;
  }

  for (uint8_t i = 0U; i < ADC_LINEAR_SAMPLE_COUNT; i++)
  {
    sum += ADC_ReadChannelOnce(channel);
  }

  return (uint16_t)(sum / ADC_LINEAR_SAMPLE_COUNT);
}

uint16_t ADC_RawToVoltageMv(uint16_t raw)
{
  return (uint16_t)(((uint32_t)raw * ADC_VREF_MV) / ADC_MAX_RAW);
}

float ADC_RawToPositionMm(uint16_t raw)
{
  return ((float)raw / (float)ADC_MAX_RAW) * ADC_LINEAR_STROKE_MM;
}

void ADC_ReadLinearSensor(ADC_LinearChannel_t channel, volatile ADC_LinearReading_t *reading)
{
  if ((channel >= ADC_LINEAR_SENSOR_COUNT) || (reading == NULL))
  {
    return;
  }

  reading->raw = ADC_ReadRaw(channel);
  reading->voltage_mv = ADC_RawToVoltageMv(reading->raw);
  reading->position_mm = ADC_RawToPositionMm(reading->raw);
}

void ADC_ReadAllLinearSensors(void)
{
  ADC_EnsureAnalogPins();

  for (uint8_t i = 0U; i < ADC_LINEAR_SENSOR_COUNT; i++)
  {
    const uint16_t raw = ADC_ReadChannelOnce((ADC_LinearChannel_t)i);
    adc_debug_raw[i] = raw;
    adc_linear_readings[i].raw = raw;
    adc_linear_readings[i].voltage_mv = ADC_RawToVoltageMv(raw);
    adc_linear_readings[i].position_mm = ADC_RawToPositionMm(raw);
  }
}

static int16_t ADC_RawToMcuTempC(uint16_t raw, uint16_t vdda_mv)
{
  const uint16_t cal1 = *TEMPSENSOR_CAL1_ADDR;
  const uint16_t cal2 = *TEMPSENSOR_CAL2_ADDR;
  int32_t ts_data_scaled;

  if ((cal2 <= cal1) || (vdda_mv == 0U))
  {
    return 25;
  }

  /* ST RM0090: TS_ADC_DATA 를 실제 Vdda 기준으로 보정 후 공장 cal 과 비교 */
  ts_data_scaled = ((int32_t)raw * (int32_t)vdda_mv) / (int32_t)TEMPSENSOR_CAL_VREFANALOG;

  return (int16_t)((((ts_data_scaled - (int32_t)cal1)
                     * (TEMPSENSOR_CAL2_TEMP - TEMPSENSOR_CAL1_TEMP))
                    / ((int32_t)cal2 - (int32_t)cal1))
                   + TEMPSENSOR_CAL1_TEMP);
}

float ADC_ReadMcuTempC(void)
{
  const uint16_t vdda_mv = ADC_ReadVddaMv();
  const uint16_t raw = ADC_ReadInternalRawAvg(ADC_CHANNEL_TEMPSENSOR, ADC_MCU_TEMP_SAMPLE_COUNT);
  const float temp_c = (float)ADC_RawToMcuTempC(raw, vdda_mv);

  adc_vdda_mv = vdda_mv;
  adc_mcu_temp_raw = raw;
  adc_ecu_temp_c = temp_c;
  return temp_c;
}

/* USER CODE END 1 */
