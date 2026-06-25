/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    lis3dsh.c
  * @brief   LIS3DSH driver for STM32F407G-DISC1 (SPI1 + PE3 CS)
  ******************************************************************************
  */
/* USER CODE END Header */

#include "lis3dsh.h"
#include "spi.h"

#define LIS3DSH_REG_WHO_AM_I   0x0FU
#define LIS3DSH_REG_CTRL_REG4  0x20U
#define LIS3DSH_REG_CTRL_REG5  0x24U
#define LIS3DSH_REG_CTRL_REG6  0x25U
#define LIS3DSH_REG_STATUS     0x27U
#define LIS3DSH_REG_OUT_X_L    0x28U
#define LIS3DSH_REG_OUT_X_H    0x29U
#define LIS3DSH_REG_OUT_Y_L    0x2AU
#define LIS3DSH_REG_OUT_Y_H    0x2BU
#define LIS3DSH_REG_OUT_Z_L    0x2CU
#define LIS3DSH_REG_OUT_Z_H    0x2DU

#define LIS3DSH_SPI_READ       0x80U
#define LIS3DSH_SPI_DUMMY      0x00U

/* ST: 100Hz + XYZ enable, BDU off */
#define LIS3DSH_CTRL_REG4_CFG  0x67U
#define LIS3DSH_CTRL_REG6_CFG  0x10U
#define LIS3DSH_BOOT_FORCED    0x80U
#define LIS3DSH_STATUS_ZYXDA   0x08U

#define LIS3DSH_SPI_TIMEOUT_MS 100U

volatile LIS3DSH_Reading_t lis3dsh_reading;
volatile uint8_t lis3dsh_who_am_i = 0U;
volatile uint8_t lis3dsh_ready = 0U;
volatile uint8_t lis3dsh_debug_bytes[6];
volatile uint32_t lis3dsh_read_count = 0U;
volatile uint32_t lis3dsh_spi_fail_count = 0U;
volatile uint32_t lis3dsh_raw_checksum = 0U;
volatile uint32_t lis3dsh_new_sample_count = 0U;
volatile uint8_t lis3dsh_status_live = 0U;
volatile int16_t lis3dsh_x_raw_live = 0;
volatile int16_t lis3dsh_y_raw_live = 0;
volatile int16_t lis3dsh_z_raw_live = 0;

static uint8_t lis3dsh_spi_tx = 0U;
static uint8_t lis3dsh_spi_rx = 0U;
static int16_t lis3dsh_last_z_raw = 0;

static void LIS3DSH_CS_Low(void)
{
  HAL_GPIO_WritePin(LIS3DSH_CS_GPIO_Port, LIS3DSH_CS_Pin, GPIO_PIN_RESET);
}

static void LIS3DSH_CS_High(void)
{
  HAL_GPIO_WritePin(LIS3DSH_CS_GPIO_Port, LIS3DSH_CS_Pin, GPIO_PIN_SET);
}

static void LIS3DSH_SPI_Recover(void)
{
  LIS3DSH_CS_High();
  (void)HAL_SPI_Abort(&hspi1);
  hspi1.State = HAL_SPI_STATE_READY;
  hspi1.ErrorCode = HAL_SPI_ERROR_NONE;
}

static void LIS3DSH_CS_Init(void)
{
  GPIO_InitTypeDef gpio = {0};

  __HAL_RCC_GPIOE_CLK_ENABLE();

  gpio.Pin = LIS3DSH_CS_Pin;
  gpio.Mode = GPIO_MODE_OUTPUT_PP;
  gpio.Pull = GPIO_NOPULL;
  gpio.Speed = GPIO_SPEED_FREQ_HIGH;
  HAL_GPIO_Init(LIS3DSH_CS_GPIO_Port, &gpio);
  LIS3DSH_CS_High();
}

static uint8_t LIS3DSH_SPI_WriteRead(uint8_t byte)
{
  lis3dsh_spi_tx = byte;

  if (HAL_SPI_TransmitReceive(&hspi1, &lis3dsh_spi_tx, &lis3dsh_spi_rx, 1U, LIS3DSH_SPI_TIMEOUT_MS) != HAL_OK)
  {
    lis3dsh_spi_fail_count++;
    LIS3DSH_SPI_Recover();
    return 0U;
  }

  return lis3dsh_spi_rx;
}

static void LIS3DSH_WriteReg(uint8_t reg, uint8_t value)
{
  LIS3DSH_CS_Low();
  (void)LIS3DSH_SPI_WriteRead(reg);
  (void)LIS3DSH_SPI_WriteRead(value);
  LIS3DSH_CS_High();
}

static uint8_t LIS3DSH_ReadReg(uint8_t reg)
{
  uint8_t value;

  LIS3DSH_CS_Low();
  (void)LIS3DSH_SPI_WriteRead(reg | LIS3DSH_SPI_READ);
  value = LIS3DSH_SPI_WriteRead(LIS3DSH_SPI_DUMMY);
  LIS3DSH_CS_High();

  return value;
}

static int16_t LIS3DSH_Raw16FromBytes(uint8_t low, uint8_t high)
{
  return (int16_t)((uint16_t)high << 8 | low);
}

static float LIS3DSH_Raw16ToG(int16_t raw16)
{
  /* ST driver: mg = raw16 * sensitivity, g = mg / 1000 */
  return ((float)raw16 * LIS3DSH_SENSITIVITY_MG) / 1000.0f;
}

static void LIS3DSH_Reboot(void)
{
  uint8_t ctrl6 = LIS3DSH_ReadReg(LIS3DSH_REG_CTRL_REG6);

  ctrl6 |= LIS3DSH_BOOT_FORCED;
  LIS3DSH_WriteReg(LIS3DSH_REG_CTRL_REG6, ctrl6);
  HAL_Delay(5);
}

HAL_StatusTypeDef LIS3DSH_Init(void)
{
  uint8_t who_am_i = 0U;

  LIS3DSH_CS_Init();
  HAL_Delay(10);

  for (uint8_t attempt = 0U; attempt < 5U; attempt++)
  {
    who_am_i = LIS3DSH_ReadReg(LIS3DSH_REG_WHO_AM_I);
    if (who_am_i == LIS3DSH_WHO_AM_I_VALUE)
    {
      break;
    }
    HAL_Delay(5);
  }

  lis3dsh_who_am_i = who_am_i;
  if (who_am_i != LIS3DSH_WHO_AM_I_VALUE)
  {
    lis3dsh_ready = 0U;
    return HAL_ERROR;
  }

  LIS3DSH_Reboot();
  LIS3DSH_WriteReg(LIS3DSH_REG_CTRL_REG4, LIS3DSH_CTRL_REG4_CFG);
  LIS3DSH_WriteReg(LIS3DSH_REG_CTRL_REG5, 0x00U);
  LIS3DSH_WriteReg(LIS3DSH_REG_CTRL_REG6, LIS3DSH_CTRL_REG6_CFG);
  HAL_Delay(10);

  (void)LIS3DSH_ReadAccel(&lis3dsh_reading);
  lis3dsh_last_z_raw = lis3dsh_reading.z_raw;
  lis3dsh_ready = 1U;
  return HAL_OK;
}

HAL_StatusTypeDef LIS3DSH_ReadAccel(volatile LIS3DSH_Reading_t *reading)
{
  uint8_t xl;
  uint8_t xh;
  uint8_t yl;
  uint8_t yh;
  uint8_t zl;
  uint8_t zh;
  int16_t x_raw;
  int16_t y_raw;
  int16_t z_raw;
  uint32_t sum = 0U;

  if (reading == NULL)
  {
    return HAL_ERROR;
  }

  lis3dsh_status_live = LIS3DSH_ReadReg(LIS3DSH_REG_STATUS);

  xl = LIS3DSH_ReadReg(LIS3DSH_REG_OUT_X_L);
  xh = LIS3DSH_ReadReg(LIS3DSH_REG_OUT_X_H);
  yl = LIS3DSH_ReadReg(LIS3DSH_REG_OUT_Y_L);
  yh = LIS3DSH_ReadReg(LIS3DSH_REG_OUT_Y_H);
  zl = LIS3DSH_ReadReg(LIS3DSH_REG_OUT_Z_L);
  zh = LIS3DSH_ReadReg(LIS3DSH_REG_OUT_Z_H);

  lis3dsh_debug_bytes[0] = xl;
  lis3dsh_debug_bytes[1] = xh;
  lis3dsh_debug_bytes[2] = yl;
  lis3dsh_debug_bytes[3] = yh;
  lis3dsh_debug_bytes[4] = zl;
  lis3dsh_debug_bytes[5] = zh;

  x_raw = LIS3DSH_Raw16FromBytes(xl, xh);
  y_raw = LIS3DSH_Raw16FromBytes(yl, yh);
  z_raw = LIS3DSH_Raw16FromBytes(zl, zh);

  lis3dsh_x_raw_live = x_raw;
  lis3dsh_y_raw_live = y_raw;
  lis3dsh_z_raw_live = z_raw;

  reading->x_raw = x_raw;
  reading->y_raw = y_raw;
  reading->z_raw = z_raw;
  reading->x_g = LIS3DSH_Raw16ToG(x_raw);
  reading->y_g = LIS3DSH_Raw16ToG(y_raw);
  reading->z_g = LIS3DSH_Raw16ToG(z_raw);

  for (uint8_t i = 0U; i < 6U; i++)
  {
    sum = (sum << 1) ^ lis3dsh_debug_bytes[i];
  }
  lis3dsh_raw_checksum = sum;

  if (z_raw != lis3dsh_last_z_raw)
  {
    lis3dsh_last_z_raw = z_raw;
    lis3dsh_new_sample_count++;
  }

  lis3dsh_read_count++;
  return HAL_OK;
}
