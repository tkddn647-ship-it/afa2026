/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : Main program body (UART Integration Ver)
  ******************************************************************************
  */
/* USER CODE END Header */
/* Includes ------------------------------------------------------------------*/
#include "main.h"
#include "can.h"
#include "i2c.h"
#include "usart.h"
#include "gpio.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */
#include <stdio.h>  // sprintf 사용을 위해 추가
#include <string.h> // memset 등 사용을 위해 추가
#include <stdlib.h> // atoi 사용을 위해 추가
/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */
typedef struct {
    int16_t x, y, z;
    float x_g, y_g, z_g;
} ADXL345_Data;
/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */
#define ADXL345_ADDRESS     (0x53 << 1) // SDO 접지시 0x53, VCC시 0x1D. (0x53<<1 = 0xA6)
#define ADXL345_DEVID       0x00
#define ADXL345_POWER_CTL   0x2D
#define ADXL345_DATA_FORMAT 0x31
#define ADXL345_DATAX0      0x32
/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/

/* USER CODE BEGIN PV */
// [CAN 관련 변수]
volatile uint16_t speed_raw = 0;
volatile uint16_t steering_raw = 0;
volatile uint32_t can_rx_count = 0;

CAN_RxHeaderTypeDef rxHeader;
uint8_t rxData[8];

// [UART - ESP32 수신용 (Linear Data)]
// 가정: ESP32가 ASCII 숫자로 보내고 끝에 '\n'을 붙임 (예: "125\n")
uint8_t esp_rx_byte;          // 1바이트 수신 버퍼
char esp_rx_buffer[20];       // 문자열 조립 버퍼
volatile uint8_t esp_rx_index = 0;
volatile float linear_sensor_val = 0.0f;
ADXL345_Data accel_data;
uint8_t adxl345_ok = 0; // 초기화 성공 플래그
// [UART - 라즈베리파이 송신용]
char tx_buffer[64]; // 보낼 문자열 버퍼

/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
/* USER CODE BEGIN PFP */
HAL_StatusTypeDef ADXL345_WriteRegister(I2C_HandleTypeDef *hi2c, uint16_t dev_addr, uint8_t reg, uint8_t value);
HAL_StatusTypeDef ADXL345_Init(I2C_HandleTypeDef *hi2c, uint16_t dev_addr);
HAL_StatusTypeDef ADXL345_Read(I2C_HandleTypeDef *hi2c, uint16_t dev_addr, ADXL345_Data *data);
/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */
// [ADXL345 레지스터 쓰기]
HAL_StatusTypeDef ADXL345_WriteRegister(I2C_HandleTypeDef *hi2c, uint16_t dev_addr, uint8_t reg, uint8_t value)
{
    return HAL_I2C_Mem_Write(hi2c, dev_addr, reg, I2C_MEMADD_SIZE_8BIT, &value, 1, 100);
}

// [ADXL345 초기화]
HAL_StatusTypeDef ADXL345_Init(I2C_HandleTypeDef *hi2c, uint16_t dev_addr)
{
    uint8_t id = 0;
    HAL_StatusTypeDef status;

    HAL_Delay(100); // 센서 안정화 대기

    // 1. Device ID 확인 (0xE5가 읽혀야 함)
    status = HAL_I2C_Mem_Read(hi2c, dev_addr, ADXL345_DEVID, I2C_MEMADD_SIZE_8BIT, &id, 1, 1000);
    if (status != HAL_OK || id != 0xE5) {
        return HAL_ERROR;
    }

    // 2. 데이터 포맷 설정 (±16g, Full resolution 모드 추천 - 여기선 원본 0x0B 사용)
    // 0x0B: Full Res, +/-16g (원본 코드 설정 유지)
    if (ADXL345_WriteRegister(hi2c, dev_addr, ADXL345_DATA_FORMAT, 0x0B) != HAL_OK) {
        return HAL_ERROR;
    }

    // 3. 측정 모드 활성화
    if (ADXL345_WriteRegister(hi2c, dev_addr, ADXL345_POWER_CTL, 0x08) != HAL_OK) {
        return HAL_ERROR;
    }

    return HAL_OK;
}

// [ADXL345 데이터 읽기]
HAL_StatusTypeDef ADXL345_Read(I2C_HandleTypeDef *hi2c, uint16_t dev_addr, ADXL345_Data *data)
{
    uint8_t buf[6];

    // DATAX0 부터 6바이트 읽기
    if (HAL_I2C_Mem_Read(hi2c, dev_addr, ADXL345_DATAX0, I2C_MEMADD_SIZE_8BIT, buf, 6, 100) != HAL_OK) {
        return HAL_ERROR;
    }

    // 데이터 조합
    data->x = (int16_t)(buf[1] << 8 | buf[0]);
    data->y = (int16_t)(buf[3] << 8 | buf[2]);
    data->z = (int16_t)(buf[5] << 8 | buf[4]);

    // g 단위 변환 (Full resolution + 16g 모드 기준 factor는 0.0039 ~ 0.004)
    data->x_g = data->x * 0.0039f;
    data->y_g = data->y * 0.0039f;
    data->z_g = data->z * 0.0039f;

    return HAL_OK;
}
/* USER CODE END 0 */

/**
  * @brief  The application entry point.
  * @retval int
  */
int main(void)
{

  /* USER CODE BEGIN 1 */

  /* USER CODE END 1 */

  /* MCU Configuration--------------------------------------------------------*/

  /* Reset of all peripherals, Initializes the Flash interface and the Systick. */
  HAL_Init();

  /* USER CODE BEGIN Init */

  /* USER CODE END Init */

  /* Configure the system clock */
  SystemClock_Config();

  /* USER CODE BEGIN SysInit */

  /* USER CODE END SysInit */

  /* Initialize all configured peripherals */
  MX_GPIO_Init();
  MX_CAN1_Init();
  MX_USART1_UART_Init();
  MX_I2C1_Init();
  MX_USART2_UART_Init();
  MX_UART4_Init();
  /* USER CODE BEGIN 2 */

  // 1. I2C 리슨(EnableListen) 코드 삭제함 (더 이상 안 씀)

  // 2. UART 수신 인터럽트 시작 (ESP32 -> UART4)
  HAL_UART_Receive_IT(&huart4, &esp_rx_byte, 1);

  // 3. CAN 필터 설정 및 시작
  if (CAN1_Joyang_Setup() != 0) {
        Error_Handler();
  }

  HAL_Delay(500);

  // 조향각 센서 영점 조절 (필요 시 유지)
  LWS_Send_Command(0x05);
  HAL_Delay(100);
  LWS_Send_Command(0x03);
  HAL_Delay(100);
  // 3. ADXL345 초기화 (I2C1 포트 사용: PB6, PB7)
  if (ADXL345_Init(&hi2c1, ADXL345_ADDRESS) == HAL_OK) {
      adxl345_ok = 1;
  } else {
        // 초기화 실패 시 처리 (디버깅용 LED 등 필요 시 추가)
      adxl345_ok = 0;
  }
  /* USER CODE END 2 */

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  while (1)
    {
      /* USER CODE END WHILE */

      /* USER CODE BEGIN 3 */
          // 1. 가속도 센서 읽기
          if (adxl345_ok) {
              ADXL345_Read(&hi2c1, ADXL345_ADDRESS, &accel_data);
          } else {
              // 센서 에러 시 0으로 초기화
              accel_data.x_g = 0; accel_data.y_g = 0; accel_data.z_g = 0;
              // 재시도 로직을 넣고 싶다면 여기서 ADXL345_Init을 시도할 수 있음
          }

          // 2. 보낼 데이터 포맷팅
          // 포맷: "$TEL,조향각,리니어값,AccX,AccY,AccZ\r\n"
          memset(tx_buffer, 0, sizeof(tx_buffer));

          // 가속도 값을 소수점 2자리로 표현 (필요시 정수로 변환하여 %d 사용 가능)
          sprintf(tx_buffer, "$TEL,%d,%d,%.2f,%d,%d,%d\r\n",
                  steering_raw,
				  speed_raw,
                  linear_sensor_val,
                  accel_data.x_g,
                  accel_data.y_g,
                  accel_data.z_g);

          // 3. 라즈베리파이로 UART 전송 (USART1 사용)
          HAL_UART_Transmit(&huart1, (uint8_t*)tx_buffer, strlen(tx_buffer), 10);

          // 전송 주기 조절 (20Hz 정도)
          HAL_Delay(50);
    }
    /* USER CODE END 3 */
}

/**
  * @brief System Clock Configuration
  * @retval None
  */
void SystemClock_Config(void)
{
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};

  /** Configure the main internal regulator output voltage
  */
  __HAL_RCC_PWR_CLK_ENABLE();
  __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE1);

  /** Initializes the RCC Oscillators according to the specified parameters
  * in the RCC_OscInitTypeDef structure.
  */
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSE;
  RCC_OscInitStruct.HSEState = RCC_HSE_ON;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSE;
  RCC_OscInitStruct.PLL.PLLM = 4;
  RCC_OscInitStruct.PLL.PLLN = 168;
  RCC_OscInitStruct.PLL.PLLP = RCC_PLLP_DIV2;
  RCC_OscInitStruct.PLL.PLLQ = 4;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    Error_Handler();
  }

  /** Initializes the CPU, AHB and APB buses clocks
  */
  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_SYSCLK
                              |RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV4;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV2;

  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_5) != HAL_OK)
  {
    Error_Handler();
  }
}

/* USER CODE BEGIN 4 */

// [1] CAN 수신 인터럽트 콜백
void HAL_CAN_RxFifo0MsgPendingCallback(CAN_HandleTypeDef *hcan) {
    if (hcan->Instance == CAN1) {
        if (HAL_CAN_GetRxMessage(hcan, CAN_RX_FIFO0, &rxHeader, rxData) == HAL_OK) {
            // 조향각 센서 ID 확인 (0x2B0)
            if (rxHeader.StdId == 0x2B0) {
                 // 1. 조향각 (Byte 0, 1)
                 steering_raw = (uint16_t)((rxData[1] << 8) | rxData[0]);

                 // 2. [추가] 조향 속도 (Byte 2, 3)
                 // 데이터시트 Byte Order: LSB (Intel) -> Byte 2가 Low, Byte 3이 High
                 speed_raw = (uint16_t)((rxData[3] << 8) | rxData[2]);

                 can_rx_count++;
            }
        }
    }
}

// [2] UART 수신 인터럽트 콜백 (ESP32 데이터 수신용)
// 가정: ESP32가 UART4에 연결되어 있음
void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart)
{
    if (huart->Instance == UART4) // ESP32가 연결된 UART 채널 확인
    {
        // 수신된 바이트가 개행문자(\n)인지 확인 (패킷의 끝)
        if (esp_rx_byte == '\n')
        {
            esp_rx_buffer[esp_rx_index] = '\0'; // 문자열 종료
            linear_sensor_val = atof(esp_rx_buffer); // 문자열 -> 정수 변환하여 전역 변수 저장
            esp_rx_index = 0; // 인덱스 초기화
        }
        else
        {
            // 버퍼 오버플로우 방지 및 데이터 저장
            if (esp_rx_index < sizeof(esp_rx_buffer) - 1) {
                esp_rx_buffer[esp_rx_index++] = esp_rx_byte;
            }
        }

        // 다음 바이트 수신 대기 (인터럽트 재활성화)
        HAL_UART_Receive_IT(&huart4, &esp_rx_byte, 1);
    }
}

// I2C 관련 콜백들은 이제 사용하지 않으므로 삭제하거나 비워둠
/* USER CODE END 4 */

/**
  * @brief  This function is executed in case of error occurrence.
  * @retval None
  */
void Error_Handler(void)
{
  /* USER CODE BEGIN Error_Handler_Debug */
  /* User can add his own implementation to report the HAL error return state */
  __disable_irq();
  while (1)
  {
  }
  /* USER CODE END Error_Handler_Debug */
}
#ifdef USE_FULL_ASSERT
/**
  * @brief  Reports the name of the source file and the source line number
  *         where the assert_param error has occurred.
  * @param  file: pointer to the source file name
  * @param  line: assert_param error line source number
  * @retval None
  */
void assert_failed(uint8_t *file, uint32_t line)
{
  /* USER CODE BEGIN 6 */
  /* User can add his own implementation to report the file name and line number,
     ex: printf("Wrong parameters value: file %s on line %d\r\n", file, line) */
  /* USER CODE END 6 */
}
#endif /* USE_FULL_ASSERT */
