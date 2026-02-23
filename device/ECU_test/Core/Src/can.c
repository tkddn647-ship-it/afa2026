/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    can.c
  * @brief   This file provides code for the configuration
  * of the CAN instances.
  ******************************************************************************
  */
/* USER CODE END Header */
/* Includes ------------------------------------------------------------------*/
#include "can.h"

/* USER CODE BEGIN 0 */

/* USER CODE END 0 */

CAN_HandleTypeDef hcan1;

/* CAN1 init function */
void MX_CAN1_Init(void)
{

  /* USER CODE BEGIN CAN1_Init 0 */

  /* USER CODE END CAN1_Init 0 */

  /* USER CODE BEGIN CAN1_Init 1 */

  /* USER CODE END CAN1_Init 1 */
  hcan1.Instance = CAN1;
  hcan1.Init.Prescaler = 6;
  hcan1.Init.Mode = CAN_MODE_NORMAL;
  hcan1.Init.SyncJumpWidth = CAN_SJW_1TQ;
  hcan1.Init.TimeSeg1 = CAN_BS1_11TQ;
  hcan1.Init.TimeSeg2 = CAN_BS2_2TQ;
  hcan1.Init.TimeTriggeredMode = DISABLE;
  hcan1.Init.AutoBusOff = DISABLE;
  hcan1.Init.AutoWakeUp = ENABLE;
  hcan1.Init.AutoRetransmission = DISABLE;
  hcan1.Init.ReceiveFifoLocked = DISABLE;
  hcan1.Init.TransmitFifoPriority = DISABLE;
  if (HAL_CAN_Init(&hcan1) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN CAN1_Init 2 */

  /* USER CODE END CAN1_Init 2 */

}

void HAL_CAN_MspInit(CAN_HandleTypeDef* canHandle)
{

  GPIO_InitTypeDef GPIO_InitStruct = {0};
  if(canHandle->Instance==CAN1)
  {
  /* USER CODE BEGIN CAN1_MspInit 0 */

  /* USER CODE END CAN1_MspInit 0 */
    /* CAN1 clock enable */
    __HAL_RCC_CAN1_CLK_ENABLE();

    __HAL_RCC_GPIOD_CLK_ENABLE();
    /**CAN1 GPIO Configuration
    PD0     ------> CAN1_RX
    PD1     ------> CAN1_TX
    */
    GPIO_InitStruct.Pin = GPIO_PIN_0|GPIO_PIN_1;
    GPIO_InitStruct.Mode = GPIO_MODE_AF_PP;
    GPIO_InitStruct.Pull = GPIO_NOPULL;
    GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
    GPIO_InitStruct.Alternate = GPIO_AF9_CAN1;
    HAL_GPIO_Init(GPIOD, &GPIO_InitStruct);

    /* CAN1 interrupt Init */
    HAL_NVIC_SetPriority(CAN1_TX_IRQn, 0, 0);
    HAL_NVIC_EnableIRQ(CAN1_TX_IRQn);
    HAL_NVIC_SetPriority(CAN1_RX0_IRQn, 0, 0);
    HAL_NVIC_EnableIRQ(CAN1_RX0_IRQn);
    HAL_NVIC_SetPriority(CAN1_RX1_IRQn, 0, 0);
    HAL_NVIC_EnableIRQ(CAN1_RX1_IRQn);
    HAL_NVIC_SetPriority(CAN1_SCE_IRQn, 0, 0);
    HAL_NVIC_EnableIRQ(CAN1_SCE_IRQn);
  /* USER CODE BEGIN CAN1_MspInit 1 */

  /* USER CODE END CAN1_MspInit 1 */
  }
}

void HAL_CAN_MspDeInit(CAN_HandleTypeDef* canHandle)
{

  if(canHandle->Instance==CAN1)
  {
  /* USER CODE BEGIN CAN1_MspDeInit 0 */

  /* USER CODE END CAN1_MspDeInit 0 */
    /* Peripheral clock disable */
    __HAL_RCC_CAN1_CLK_DISABLE();

    /**CAN1 GPIO Configuration
    PD0     ------> CAN1_RX
    PD1     ------> CAN1_TX
    */
    HAL_GPIO_DeInit(GPIOD, GPIO_PIN_0|GPIO_PIN_1);

    /* CAN1 interrupt Deinit */
    HAL_NVIC_DisableIRQ(CAN1_TX_IRQn);
    HAL_NVIC_DisableIRQ(CAN1_RX0_IRQn);
    HAL_NVIC_DisableIRQ(CAN1_RX1_IRQn);
    HAL_NVIC_DisableIRQ(CAN1_SCE_IRQn);
  /* USER CODE BEGIN CAN1_MspDeInit 1 */

  /* USER CODE END CAN1_MspDeInit 1 */
  }
}

/* USER CODE BEGIN 1 */
int CAN1_Joyang_Setup(void) {
    CAN_FilterTypeDef CAN_FILTER;

    // 1. 필터 설정 (모든 메시지 다 받음)
    CAN_FILTER.FilterBank = 0; // CAN1은 0번 뱅크 사용
    CAN_FILTER.FilterMode = CAN_FILTERMODE_IDMASK;
    CAN_FILTER.FilterScale = CAN_FILTERSCALE_32BIT;
    CAN_FILTER.FilterIdHigh = 0x0000;
    CAN_FILTER.FilterIdLow = 0x0000;
    CAN_FILTER.FilterMaskIdHigh = 0x0000;
    CAN_FILTER.FilterMaskIdLow = 0x0000;
    CAN_FILTER.FilterFIFOAssignment = CAN_RX_FIFO0;
    CAN_FILTER.FilterActivation = ENABLE;
    CAN_FILTER.SlaveStartFilterBank = 14;

    if (HAL_CAN_ConfigFilter(&hcan1, &CAN_FILTER) != HAL_OK) {
        return 1;
    }

    // 2. CAN 시작
    if (HAL_CAN_Start(&hcan1) != HAL_OK) {
        return 2;
    }

    // 3. 인터럽트 활성화
    if (HAL_CAN_ActivateNotification(&hcan1, CAN_IT_RX_FIFO0_MSG_PENDING) != HAL_OK) return 3;
    if (HAL_CAN_ActivateNotification(&hcan1, CAN_IT_RX_FIFO0_FULL) != HAL_OK) return 4;
    if (HAL_CAN_ActivateNotification(&hcan1, CAN_IT_RX_FIFO0_OVERRUN) != HAL_OK) return 5;
    if (HAL_CAN_ActivateNotification(&hcan1, CAN_IT_BUSOFF) != HAL_OK) return 6;
    if (HAL_CAN_ActivateNotification(&hcan1, CAN_IT_ERROR) != HAL_OK) return 7;

    return 0; // 성공
}

/**
  * @brief 조양센서 보정 명령 전송 함수
  * @param cmd: 0x05(리셋), 0x03(영점설정)
  */
void LWS_Send_Command(uint8_t cmd)
{
    CAN_TxHeaderTypeDef txHeader;
    uint8_t txData[8] = {0};
    uint32_t txMailbox;

    txHeader.StdId = 0x7C0;         // 조양센서 보정 ID
    txHeader.RTR = CAN_RTR_DATA;
    txHeader.IDE = CAN_ID_STD;
    txHeader.DLC = 8;

    txData[0] = cmd;

    HAL_CAN_AddTxMessage(&hcan1, &txHeader, txData, &txMailbox);
}
/* USER CODE END 1 */
