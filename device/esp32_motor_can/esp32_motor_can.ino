/**
 * ESP32 + WC-MCU230 — Cascadia CAN (V5_9)
 * WiFi 없음. 시리얼로 ID별 바이트 위치 + 파싱 값 출력.
 *
 * 배선: 3.3V→VCC | GND→GND | GPIO5→CTX | GPIO4→CRX | CANH/L @ 250kbps
 */

#include <math.h>
#include <string.h>
#include "driver/twai.h"

#define CAN_BITRATE_K  250
#define CAN_TX_GPIO    5
#define CAN_RX_GPIO    4

/* Cascadia V5_9 default offset 0x0A0 */
static const uint32_t ID_0A0 = 0x0A0; /* Temperatures #1 */
static const uint32_t ID_0A2 = 0x0A2; /* Temperatures #3 */
static const uint32_t ID_0A5 = 0x0A5; /* Motor Position */
static const uint32_t ID_0A6 = 0x0A6; /* Current */
static const uint32_t ID_0A7 = 0x0A7; /* Voltage */
static const uint32_t ID_0AA = 0x0AA; /* Internal States */

static const uint32_t PRINT_MS = 500;

struct MotorState {
  float igbt_a_c, igbt_b_c, igbt_c_c;
  float motor_temp_c;
  float rpm;
  float dc_i_a;
  float dc_v_v;
  uint8_t vsm;
  uint8_t inv_state;
  uint8_t precharge;
  uint8_t main_contactor;
  uint8_t run_mode; /* 0=Torque 1=Speed */
  uint8_t raw_0a0[8], raw_0a2[8], raw_0a5[8], raw_0a6[8], raw_0a7[8], raw_0aa[8];
  uint8_t dlc_0a0, dlc_0a2, dlc_0a5, dlc_0a6, dlc_0a7, dlc_0aa;
  uint32_t rx_any, rx_0a0, rx_0a2, rx_0a5, rx_0a6, rx_0a7, rx_0aa;
};

static MotorState g = {};
static uint32_t g_last_print = 0;
static uint32_t g_bus_error = 0;

static int16_t le_i16(const uint8_t *d, uint8_t off)
{
  return (int16_t)((uint16_t)d[off] | ((uint16_t)d[off + 1] << 8));
}

static float x10(const uint8_t *d, uint8_t off)
{
  return (float)le_i16(d, off) * 0.1f;
}

static void save_raw(uint8_t *dst, uint8_t *dlc_out, const uint8_t *src, uint8_t dlc)
{
  memset(dst, 0, 8);
  uint8_t n = (dlc > 8) ? 8 : dlc;
  memcpy(dst, src, n);
  *dlc_out = n;
}

static void update_vsm(uint8_t vsm)
{
  g.vsm = vsm;
  /* PDF: 1=Precharge Init, 2=Precharge Active, 3=Complete, 5=Ready, 6=Running, 7=Fault blink */
  g.precharge = (vsm == 1 || vsm == 2) ? 1 : 0;
  g.main_contactor = (vsm >= 3 && vsm <= 6) ? 1 : 0;
}

static float igbt_max()
{
  float m = g.igbt_a_c;
  if (g.igbt_b_c > m) m = g.igbt_b_c;
  if (g.igbt_c_c > m) m = g.igbt_c_c;
  return m;
}

/*
 * Cascadia V5_9 Little-Endian:
 *  Temperature / Current / High Voltage = int16 * 0.1
 *  Motor Speed (RPM) = int16 * 1
 */
static void parse_known(uint32_t id, const uint8_t *data, uint8_t dlc)
{
  switch (id) {
    case ID_0A0:
      /* B0-1 IGBT A, B2-3 IGBT B, B4-5 IGBT C, B6-7 GateDriver  (°C×10) */
      save_raw(g.raw_0a0, &g.dlc_0a0, data, dlc);
      if (dlc >= 2) g.igbt_a_c = x10(data, 0);
      if (dlc >= 4) g.igbt_b_c = x10(data, 2);
      if (dlc >= 6) g.igbt_c_c = x10(data, 4);
      g.rx_0a0++;
      break;

    case ID_0A2:
      /* B0-1 Coolant, B2-3 Hotspot, B4-5 MotorTemp (°C×10), B6-7 TorqueShudder */
      save_raw(g.raw_0a2, &g.dlc_0a2, data, dlc);
      if (dlc >= 6) g.motor_temp_c = x10(data, 4);
      g.rx_0a2++;
      break;

    case ID_0A5:
      /* B0-1 MotorAngle(°×10), B2-3 MotorSpeed(RPM), B4-5 ElecFreq(Hz×10) */
      save_raw(g.raw_0a5, &g.dlc_0a5, data, dlc);
      if (dlc >= 4) g.rpm = (float)le_i16(data, 2);
      g.rx_0a5++;
      break;

    case ID_0A6:
      /* B0-1 PhA, B2-3 PhB, B4-5 PhC, B6-7 DC Bus Current (A×10) */
      save_raw(g.raw_0a6, &g.dlc_0a6, data, dlc);
      if (dlc >= 8) g.dc_i_a = x10(data, 6);
      g.rx_0a6++;
      break;

    case ID_0A7:
      /* B0-1 DC Bus V (V×10), B2-3 Output V, B4-5 VAB/Vd, B6-7 VBC/Vq */
      save_raw(g.raw_0a7, &g.dlc_0a7, data, dlc);
      if (dlc >= 2) g.dc_v_v = x10(data, 0);
      g.rx_0a7++;
      break;

    case ID_0AA:
      /* B0 VSM, B2 InverterState, B4 bit0 RunMode(0=Trq/1=Spd) */
      save_raw(g.raw_0aa, &g.dlc_0aa, data, dlc);
      if (dlc >= 1) update_vsm(data[0]);
      if (dlc >= 3) g.inv_state = data[2];
      if (dlc >= 5) g.run_mode = (uint8_t)(data[4] & 0x01);
      g.rx_0aa++;
      break;

    default:
      break;
  }
}

static void print_bytes(const char *label, const uint8_t *raw, uint8_t dlc)
{
  Serial.printf("  %-10s raw[%u]=", label, dlc);
  for (uint8_t i = 0; i < dlc; i++) Serial.printf("%02X ", raw[i]);
  if (dlc == 0) Serial.print("(no rx yet)");
  Serial.println();
}

static bool can_init()
{
  twai_general_config_t gcfg = TWAI_GENERAL_CONFIG_DEFAULT(
      (gpio_num_t)CAN_TX_GPIO, (gpio_num_t)CAN_RX_GPIO, TWAI_MODE_NORMAL);
  twai_timing_config_t tcfg = TWAI_TIMING_CONFIG_250KBITS();
  twai_filter_config_t fcfg = TWAI_FILTER_CONFIG_ACCEPT_ALL();

  if (twai_driver_install(&gcfg, &tcfg, &fcfg) != ESP_OK) return false;
  if (twai_start() != ESP_OK) return false;

  twai_reconfigure_alerts(
      TWAI_ALERT_RX_DATA | TWAI_ALERT_BUS_ERROR | TWAI_ALERT_RX_QUEUE_FULL, NULL);

  Serial.printf("[can] NORMAL %dkbps TX=GPIO%d RX=GPIO%d\n",
                CAN_BITRATE_K, CAN_TX_GPIO, CAN_RX_GPIO);
  return true;
}

static void can_poll()
{
  uint32_t alerts = 0;
  if (twai_read_alerts(&alerts, 0) == ESP_OK) {
    if (alerts & TWAI_ALERT_BUS_ERROR) g_bus_error++;
  }

  twai_message_t msg;
  while (twai_receive(&msg, 0) == ESP_OK) {
    if (msg.extd || msg.rtr) continue;
    g.rx_any++;
    parse_known(msg.identifier, msg.data, msg.data_length_code);
  }
}

static void print_byte_map()
{
  Serial.println();
  Serial.println("======== Cascadia V5_9 바이트 맵 (LE) ========");
  Serial.println("ID   | Bytes | 필드              | 스케일");
  Serial.println("-----+-------+-------------------+--------------");
  Serial.println("0xA0 | 0-1   | IGBT Module A     | C x10");
  Serial.println("0xA0 | 2-3   | IGBT Module B     | C x10");
  Serial.println("0xA0 | 4-5   | IGBT Module C     | C x10");
  Serial.println("0xA0 | 6-7   | Gate Driver Temp  | C x10");
  Serial.println("0xA2 | 0-1   | Coolant           | C x10");
  Serial.println("0xA2 | 2-3   | Hot Spot          | C x10");
  Serial.println("0xA2 | 4-5   | Motor Temp   [*]  | C x10");
  Serial.println("0xA5 | 0-1   | Motor Angle       | deg x10");
  Serial.println("0xA5 | 2-3   | Motor Speed  [*]  | RPM x1");
  Serial.println("0xA5 | 4-5   | Electrical Freq   | Hz x10");
  Serial.println("0xA6 | 0-1   | Phase A Current   | A x10");
  Serial.println("0xA6 | 2-3   | Phase B Current   | A x10");
  Serial.println("0xA6 | 4-5   | Phase C Current   | A x10");
  Serial.println("0xA6 | 6-7   | DC Bus Current[*] | A x10");
  Serial.println("0xA7 | 0-1   | DC Bus Voltage[*] | V x10");
  Serial.println("0xA7 | 2-3   | Output Voltage    | V x10");
  Serial.println("0xAA | 0     | VSM State         | enum");
  Serial.println("0xAA | 2     | Inverter State    | enum");
  Serial.println("0xAA | 4.0   | Run Mode     [*]  | 0=Trq 1=Spd");
  Serial.println("[*] = 우리가 로그/서버에 쓰는 값");
  Serial.println("==============================================");
  Serial.println();
}

static void print_status()
{
  Serial.println("---------- 파싱 결과 (바이트 → 값) ----------");

  print_bytes("0xA0", g.raw_0a0, g.dlc_0a0);
  Serial.printf("       B0-1 IGBT_A=%.1fC  B2-3 IGBT_B=%.1fC  B4-5 IGBT_C=%.1fC  max=%.1fC\n",
                g.igbt_a_c, g.igbt_b_c, g.igbt_c_c, igbt_max());

  print_bytes("0xA2", g.raw_0a2, g.dlc_0a2);
  Serial.printf("       B4-5 MotorTemp=%.1fC\n", g.motor_temp_c);

  print_bytes("0xA5", g.raw_0a5, g.dlc_0a5);
  Serial.printf("       B2-3 RPM=%.0f\n", g.rpm);

  print_bytes("0xA6", g.raw_0a6, g.dlc_0a6);
  Serial.printf("       B6-7 DC_Current=%.1fA\n", g.dc_i_a);

  print_bytes("0xA7", g.raw_0a7, g.dlc_0a7);
  Serial.printf("       B0-1 DC_Voltage=%.1fV\n", g.dc_v_v);

  print_bytes("0xAA", g.raw_0aa, g.dlc_0aa);
  Serial.printf("       B0 VSM=%u  B2 InvState=%u  B4bit0 Mode=%u(%s)  pre=%u mc=%u\n",
                (unsigned)g.vsm, (unsigned)g.inv_state,
                (unsigned)g.run_mode, g.run_mode ? "Speed" : "Torque",
                (unsigned)g.precharge, (unsigned)g.main_contactor);

  Serial.printf("rx ANY=%lu | A0=%lu A2=%lu A5=%lu A6=%lu A7=%lu AA=%lu | bus_err=%lu\n",
                (unsigned long)g.rx_any,
                (unsigned long)g.rx_0a0, (unsigned long)g.rx_0a2, (unsigned long)g.rx_0a5,
                (unsigned long)g.rx_0a6, (unsigned long)g.rx_0a7, (unsigned long)g.rx_0aa,
                (unsigned long)g_bus_error);
  if (g.rx_any == 0) {
    Serial.println("!! 수신 0 — TX/RX 교차 또는 ACK/배선 확인");
  }
  Serial.println();
}

void setup()
{
  Serial.begin(115200);
  delay(500);
  Serial.println();
  Serial.println("=== ESP32 Cascadia CAN byte-map parse ===");
  print_byte_map();
  if (!can_init()) Serial.println("[fatal] CAN init fail");
  g_last_print = millis();
}

void loop()
{
  can_poll();
  uint32_t now = millis();
  if (now - g_last_print >= PRINT_MS) {
    g_last_print = now;
    print_status();
  }
}
