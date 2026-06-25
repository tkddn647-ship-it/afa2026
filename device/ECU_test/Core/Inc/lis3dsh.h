/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    lis3dsh.h
  * @brief   LIS3DSH driver for STM32F407G-DISC1 (SPI1 + PE3 CS)
  ******************************************************************************
  */
/* USER CODE END Header */

#ifndef __LIS3DSH_H__
#define __LIS3DSH_H__

#ifdef __cplusplus
extern "C" {
#endif

#include "main.h"

#define LIS3DSH_WHO_AM_I_VALUE  0x3FU
#define LIS3DSH_SENSITIVITY_MG  0.06f

typedef struct
{
  int16_t x_raw;
  int16_t y_raw;
  int16_t z_raw;
  float x_g;
  float y_g;
  float z_g;
} LIS3DSH_Reading_t;

extern volatile LIS3DSH_Reading_t lis3dsh_reading;
extern volatile uint8_t lis3dsh_who_am_i;
extern volatile uint8_t lis3dsh_ready;
extern volatile uint8_t lis3dsh_debug_bytes[6];
extern volatile uint32_t lis3dsh_read_count;
extern volatile uint32_t lis3dsh_spi_fail_count;
extern volatile uint32_t lis3dsh_raw_checksum;
extern volatile int16_t lis3dsh_x_raw_live;
extern volatile int16_t lis3dsh_y_raw_live;
extern volatile int16_t lis3dsh_z_raw_live;
extern volatile uint32_t lis3dsh_new_sample_count;
extern volatile uint8_t lis3dsh_status_live;

HAL_StatusTypeDef LIS3DSH_Init(void);
HAL_StatusTypeDef LIS3DSH_ReadAccel(volatile LIS3DSH_Reading_t *reading);

#ifdef __cplusplus
}
#endif

#endif /* __LIS3DSH_H__ */
