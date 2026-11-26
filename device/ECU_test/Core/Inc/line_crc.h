/**
  * @file    line_crc.h
  * @brief   CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) for the UART4 text line.
  *
  * The Pi (Uart_stm.LineGuard) recomputes it over everything before ",*" and drops lines that do not match.
  * Header-only so the CubeIDE and EWARM projects need no new source file, and a host test can compile it.
  * Check value: "123456789" -> 0x29B1.
  */
#ifndef __LINE_CRC_H__
#define __LINE_CRC_H__

#include <stdint.h>

static inline uint16_t LineCrc16(const char *data, int len)
{
  uint16_t crc = 0xFFFFU;
  int i;
  uint8_t b;

  for (i = 0; i < len; i++)
  {
    crc ^= (uint16_t)((uint8_t)data[i]) << 8;
    for (b = 0U; b < 8U; b++)
    {
      crc = (crc & 0x8000U) ? (uint16_t)((crc << 1) ^ 0x1021U) : (uint16_t)(crc << 1);
    }
  }
  return crc;
}

#endif /* __LINE_CRC_H__ */
