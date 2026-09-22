/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    uart4_tx.h
  * @brief   UART4 TX 링버퍼 + DMA 논블로킹 전송
  ******************************************************************************
  */
/* USER CODE END Header */

#ifndef __UART4_TX_H__
#define __UART4_TX_H__

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

void Uart4Tx_Init(void);
uint8_t Uart4Tx_Write(const uint8_t *data, uint16_t len);
uint16_t Uart4Tx_Pending(void);

extern volatile uint32_t uart4_tx_overflow;
extern volatile uint32_t uart4_tx_dma_fail;

#ifdef __cplusplus
}
#endif

#endif /* __UART4_TX_H__ */
