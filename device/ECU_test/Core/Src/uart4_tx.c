/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    uart4_tx.c
  * @brief   UART4 TX 링버퍼 + DMA 논블로킹 전송
  *
  * 메인 루프는 포맷된 바이트를 큐에만 넣고, DMA가 백그라운드로 전송한다.
  ******************************************************************************
  */
/* USER CODE END Header */

#include "uart4_tx.h"
#include "usart.h"

#define UART4_TX_RING_SIZE  4096U
#define UART4_TX_DMA_MAX    512U

static uint8_t uart4_tx_ring[UART4_TX_RING_SIZE];
static uint8_t uart4_tx_dma_chunk[UART4_TX_DMA_MAX];
static volatile uint16_t uart4_tx_head = 0U;
static volatile uint16_t uart4_tx_tail = 0U;
static volatile uint16_t uart4_tx_dma_len = 0U;
static volatile uint8_t uart4_tx_busy = 0U;

volatile uint32_t uart4_tx_overflow = 0U;
volatile uint32_t uart4_tx_dma_fail = 0U;

static void Uart4Tx_Kick(void);

static uint16_t Uart4Tx_NextIndex(uint16_t index)
{
  return (uint16_t)((index + 1U) % UART4_TX_RING_SIZE);
}

static uint16_t Uart4Tx_UsedLocked(void)
{
  if (uart4_tx_head >= uart4_tx_tail)
  {
    return (uint16_t)(uart4_tx_head - uart4_tx_tail);
  }
  return (uint16_t)(UART4_TX_RING_SIZE - (uart4_tx_tail - uart4_tx_head));
}

void Uart4Tx_Init(void)
{
  uart4_tx_head = 0U;
  uart4_tx_tail = 0U;
  uart4_tx_dma_len = 0U;
  uart4_tx_busy = 0U;
  uart4_tx_overflow = 0U;
  uart4_tx_dma_fail = 0U;
}

uint16_t Uart4Tx_Pending(void)
{
  uint16_t used;

  __disable_irq();
  used = Uart4Tx_UsedLocked();
  __enable_irq();
  return used;
}

uint8_t Uart4Tx_Write(const uint8_t *data, uint16_t len)
{
  uint16_t i;

  if ((data == NULL) || (len == 0U))
  {
    return 1U;
  }

  __disable_irq();
  for (i = 0U; i < len; i++)
  {
    uint16_t next = Uart4Tx_NextIndex(uart4_tx_head);
    if (next == uart4_tx_tail)
    {
      uart4_tx_overflow++;
      __enable_irq();
      Uart4Tx_Kick();
      return 0U;
    }
    uart4_tx_ring[uart4_tx_head] = data[i];
    uart4_tx_head = next;
  }
  __enable_irq();

  Uart4Tx_Kick();
  return 1U;
}

static void Uart4Tx_Kick(void)
{
  uint16_t count = 0U;
  uint16_t t;

  __disable_irq();
  if (uart4_tx_busy != 0U)
  {
    __enable_irq();
    return;
  }

  t = uart4_tx_tail;
  while ((t != uart4_tx_head) && (count < UART4_TX_DMA_MAX))
  {
    uart4_tx_dma_chunk[count] = uart4_tx_ring[t];
    count++;
    t = Uart4Tx_NextIndex(t);
  }

  if (count == 0U)
  {
    __enable_irq();
    return;
  }

  uart4_tx_dma_len = count;
  uart4_tx_busy = 1U;
  __enable_irq();

  if (HAL_UART_Transmit_DMA(&huart4, uart4_tx_dma_chunk, count) != HAL_OK)
  {
    uart4_tx_dma_fail++;
    __disable_irq();
    uart4_tx_busy = 0U;
    uart4_tx_dma_len = 0U;
    __enable_irq();
  }
}

void HAL_UART_TxCpltCallback(UART_HandleTypeDef *huart)
{
  if ((huart == NULL) || (huart->Instance != UART4))
  {
    return;
  }

  __disable_irq();
  uart4_tx_tail = (uint16_t)((uart4_tx_tail + uart4_tx_dma_len) % UART4_TX_RING_SIZE);
  uart4_tx_dma_len = 0U;
  uart4_tx_busy = 0U;
  __enable_irq();

  Uart4Tx_Kick();
}

void HAL_UART_ErrorCallback(UART_HandleTypeDef *huart)
{
  if ((huart == NULL) || (huart->Instance != UART4))
  {
    return;
  }

  uart4_tx_dma_fail++;
  (void)HAL_UART_AbortTransmit(huart);

  __disable_irq();
  uart4_tx_busy = 0U;
  uart4_tx_dma_len = 0U;
  __enable_irq();

  Uart4Tx_Kick();
}
