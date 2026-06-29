#ifndef __WHEEL_SPEED_UART_H__
#define __WHEEL_SPEED_UART_H__

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

#define WHEEL_SPEED_FLUSH_MS  50U
#define WHEEL_TEETH_COUNT     48U

void WheelSpeedUart_Init(void);
float WheelSpeedUart_GetWheelRpmRight(void);
float WheelSpeedUart_GetWheelRpmLeft(void);
uint32_t WheelSpeedUart_GetRxLineCount(void);

#ifdef __cplusplus
}
#endif

#endif /* __WHEEL_SPEED_UART_H__ */
