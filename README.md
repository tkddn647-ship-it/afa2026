# AFA2026 계측 시스템

포뮬러 스타일 전기차의 **섀시·구동·조향 데이터를 한 파이프라인으로 수집·로깅·실시간 표시**하기 위한 계측 프로젝트입니다.

이 README는 `device/`(현장 장비) 중심으로, 하드웨어 구성부터 STM32 펌웨어·Raspberry Pi·서버 연동까지의 **설계 의도와 구현 구조**를 정리합니다.

관련 저장소: [tkddn647-ship-it/afa2026](https://github.com/tkddn647-ship-it/afa2026)

---

## 1. 한 줄 요약

> 차량의 리니어·가속도·인버터/BMS·조향 데이터를 **STM32F407 MCU에서 통합 수집(100 Hz)** 하고, UART로 Raspberry Pi에 넘겨 Hotspot Wi‑Fi로 서버에 로깅·실시간 표시·카메라 녹화까지 연결한다.

---

## 2. 전체 시스템 구조

```
[차량 센서/ECU들]                    [현장 보드]                         [네트워크]         [서버]
Linear FR/FL/RR/RL ──ADC──┐
LIS3DSH Accel ──────SPI───┤
Motor Inverter+BMS ─CAN1──┼──▶ STM32F407 MCU ──UART4 921600──▶ Raspberry Pi 5 ──Wi‑Fi──▶ Hotspot
Steering (LWS) ─────CAN2──┘         ▲                              │ Camera 15fps              │
Wheel speed (옵션) ─USART2─┘         │                              │                           ▼
                                      │                         data_send_server.py     Logger   :8000
                                      └─ 모든 텔레메트리 1차 집약                         Realtime :8011
                                                                                         Camera   :8012
```

설계의 핵심은 **“모든 차량 데이터는 먼저 MCU로 모인다”** 입니다.

| 구간 | 역할 |
|------|------|
| 센서 / CAN | 물리량·차량 버스 데이터 생성 |
| **STM32 MCU** | 샘플링·단위 환산·버퍼링·동기 타임스탬프 |
| **UART4** | MCU → Pi 단일 CSV 스트림 |
| **Raspberry Pi 5** | 파싱, 배치 HTTP 전송, 카메라 프레임 업로드 |
| **Hotspot** | 이동 환경 Wi‑Fi (아이폰 핫스팟 등으로 운용) |
| **서버** | 집 노트북에서 구동. CSV 로깅, 실시간 대시보드, 카메라 수신/녹화. 공유기 포트포워딩 + SSH로 원격 관리 |

배치도 이미지: [`afa2026_system_layout.png`](./afa2026_system_layout.png)

서버는 클라우드 전용 VM이 아니라 **집에 둔 노트북**을 서버로 쓰고, 공유기·방화벽·SSH를 열어 **밖에서도 본인 PC로 원격 접속·제어**하는 방식으로 운용했습니다. 자세한 절차는 [§8. 집 서버 원격 접속 (포트포워딩 + SSH)](#8-집-서버-원격-접속-포트포워딩--ssh) 참고.

---

## 3. 하드웨어 설계

### 3.1 MCU 보드

| 항목 | 내용 |
|------|------|
| MCU | **STM32F407** (Discovery / 커스텀 PCB 연동) |
| 클럭 | HSE 크리스털 → PLL (시스템 클럭). UART/CAN 비트 타이밍 정확도에 필요 |
| 워치독 | IWDG (~1 s) — 메인 루프 정지 시 리셋 |

### 3.2 센서·버스 핀맵 (펌웨어 기준)

#### 리니어 포텐셔미터 (서스펜션 변위)

| 채널 | MCU 핀 | ADC | 비고 |
|------|--------|-----|------|
| **FR** | PA1 | ADC1_IN1 | 3선: VCC / GND / Signal |
| **RR** | PA2 | ADC1_IN2 | |
| **RL** | PA3 | ADC1_IN3 | |
| **FL** | PA4 | ADC1_IN4 | |

- ADC 기준(VDDA)은 **3.3 V**
- 센서 공급/풀스케일 전압은 채널별로 다르며, **소프트웨어 vmax 캘리브레이션**으로 0–100 mm에 맞춤
- Discovery **PA0**은 유저 버튼용이라 리니어에 사용하지 않음

#### 가속도

| 센서 | 인터페이스 | 비고 |
|------|------------|------|
| LIS3DSH | SPI1 | ODR 상향(400 Hz급)으로 200/100 Hz 샘플에 대응 |

#### CAN

| 버스 | 속도 | 핀 (펌웨어) | 상대 |
|------|------|-------------|------|
| **CAN1** | 250 kbps | **PB8 RX / PB9 TX** | Cascadia 인버터 + Orion BMS |
| **CAN2** | 500 kbps | **PB12 / PB13** | Bosch LWS 조향 |

> 회로도/PCB와 핀이 어긋나면(예: CAN1이 PA11/PA12로만 배선) 수신이 안 됩니다. 펌웨어는 **PB8/PB9, PB12/PB13** 기준입니다.

#### UART

| 포트 | 보드레이트 | 핀 | 용도 |
|------|------------|-----|------|
| **UART4** | **921600** | PC10 TX / PC11 RX | Raspberry Pi 텔레메트리 |
| USART2 | 115200 | (휠스피드 아두이노 등) | 옵션 |

Pi 배선 (GPIO):

| STM32 | Raspberry Pi 5 |
|-------|----------------|
| PC10 (UART4 TX) | Pin 10 (GPIO15 RX) |
| PC11 (UART4 RX) | Pin 8 (GPIO14 TX) |
| GND | GND |

포트: `/dev/ttyAMA0` (`dtparam=uart0=on`)

### 3.3 전원·배치·실차 이슈 (설계 시 고려)

- MCU·Pi는 근거리(배전함 등)에 두고, 모터컨트롤러는 함 밖인 경우가 많음 → **UART 거리는 짧아 유리**, 다만 **고전류 GND/EMI**가 케이블로 타고 들어옴
- 리니어 케이블에서 **신호↔파워 교차/단락** 시 중간 위치에서도 ADC가 ~3.3 V(100 mm 고정)처럼 보임 → 하드웨어 배선 문제로 확인됨
- 플로팅 ADC 핀은 센서 미연결 시에도 중간값이 움직임 → 정상 현상
- 핫스팟 + 차량 이동 시 **Pi→서버 업로드**가 병목이 되어 로그 유효 Hz가 떨어질 수 있음 (MCU 샘플링과 별개)

---

## 4. 소프트웨어 아키텍처 (device)

### 4.1 디렉터리

```
device/
├── ECU_test/                 # STM32CubeIDE 프로젝트 (메인 MCU 펌웨어)
│   ├── Core/Inc|Src/         # 애플리케이션 코드
│   ├── Drivers/              # HAL / CMSIS
│   └── ECU_test.ioc          # CubeMX 설정
├── raspberry pi 5/           # Pi 수신·서버 전송·카메라
│   ├── Uart_stm.py           # UART CSV 파서
│   ├── data_send_server.py   # ingest / realtime 전송
│   ├── camera.py             # 카메라 프레임 업로드 (15fps 운용)
│   └── stm_ingest.env.example
├── wheel_speed/              # 휠스피드 아두이노 (옵션)
├── Fluid_Sensor/             # 유체 센서 스케치
├── esp32/ , esp32_motor_can/ # 보조/실험용
└── ...
```

### 4.2 STM32 펌웨어 — 슈퍼루프 설계

RTOS 없이 **메인 루프 + 논블로킹** 구조입니다 (`main.c`).

1. `MX_*_Init` — GPIO, CAN1/2, UART, ADC, SPI, DMA
2. `LIS3DSH` / `CAN_Vehicle` / `CAN_LWS` / `SensorUart` / `Uart4Tx` 초기화
3. 루프:
   - CAN FIFO 드레인·파싱
   - `SensorUart_Process()` — 100 Hz 샘플 적재, 20 Hz DMA flush
   - IWDG kick

#### 샘플링 / 전송

| 항목 | 값 | 파일 |
|------|-----|------|
| 샘플 주기 | **10 ms (100 Hz)** | `sensor_uart.h` / `.c` |
| UART flush | **20 Hz**, 배치 5샘플 | 동일 |
| TX | UART4 **DMA 논블로킹** | `uart4_tx.c` |
| Baud | **921600** | `usart.c` |

초기에는 200 Hz(5 ms)였으나, 실차·무선 부하를 고려해 **100 Hz**로 조정했습니다.

#### 리니어 ADC 처리 (`adc.c` / `adc.h`)

1. 채널당 **8회 평균**
2. **EMA** 필터 (노이즈 완화)
3. 전압 환산: `voltage_mv = raw × 3300 / 4095`
4. 위치 환산(채널별):

```text
position_mm = (voltage_mv / ADC_LINEAR_VMAX_xx_MV) × 100
```

풀 스트로크 실측으로 vmax를 맞춥니다. (예: 풀에서 88.6 mm가 나오면 `vmax_new = vmax_old × 0.886`)

최신 캘리 상수 예:

| 채널 | `ADC_LINEAR_VMAX_*_MV` |
|------|------------------------|
| FR | 1825 |
| RR | 1835 |
| RL | 1827 |
| FL | 1818 |

#### CAN 파싱 (`can_vehicle.c`, `can_lws.c`)

- Cascadia Motion 프로토콜(V5.9 계열): 온도·전류·전압·토크·Id/Iq·RPM 등 **int16 × 0.1** 환산 후 float 보관
- Orion BMS 커스텀 ID(0x081/0x082 등): 전압·전류·Ah·온도·CCL/DCL
- LWS: 조향각·조향 속도
- UART로 나가기 전에 **이미 공학 단위로 환산**됨 (Pi/서버에서 재스케일하지 않음)
- `can1` / `can2`: 최근 **500 ms** 내 수신 여부 (0/1)

#### UART CSV 한 줄 (순서 고정)

```text
stm_ms, FR, FL, RR, RL, x_g, y_g, z_g, ecu_temp,
steering_angle, steering_speed,
inv_temp_igt, inv_temp_motor, inv_motor_speed, inv_dc_current, inv_voltage,
motor_precharge, motor_main_contactor, motor_inverter_mode,
inv_shudder_torque, inv_id_feedback, inv_iq_feedback,
inv_torque_commanded, inv_torque_feedback, inv_id_command, inv_iq_command,
bms_charge, bms_voltage, bms_current, bms_ccl, bms_dcl, bms_temp_maxvalue, bms_capacity,
can1, can2
```

제어 라인(데이터 아님): `STM_READY`, `HB,...`, `LWS_CAL,...`

차속은 `inv_motor_speed × 0.0222328` (타이어 외경 460 mm, 감속비 3.9)로 **서버/Pi에서 계산**하는 것을 권장 (UART 필드 추가 불필요).

### 4.3 Raspberry Pi 5

| 스크립트 | 역할 |
|----------|------|
| `Uart_stm.py` | 921600 수신, CSV 파싱, 100 Hz/20 Hz 배치 개념 |
| `data_send_server.py` | 배치를 Logger `:8000/ingest` + (옵션) Realtime `:8011` 로 POST |
| `camera.py` | JPEG 프레임 업로드 (운용 **15 fps**) |
| `uart_test.py` | 보드레이트/배선 점검 |

예시:

```bash
python3 data_send_server.py \
  --port /dev/ttyAMA0 \
  --baud 921600 \
  --realtime-url http://<server>:8011/api/live/ingest
```

환경 변수 예시는 `stm_ingest.env.example` 참고.

### 4.4 서버 쪽 (연동만 요약)

| 서비스 | 포트 | 역할 |
|--------|------|------|
| `udp_logger` | 8000 | CSV 로깅 ingest |
| `udp_realtime` | 8011 | 실시간 대시보드 |
| camera | 8012 | 프레임 수신·녹화(ffmpeg) |

실차에서 로그 유효 Hz가 50대까지 떨어지는 경우, CSV의 `stm_ms`는 대부분 10 ms를 유지하는 반면 wall-clock 초당 행 수만 줄어든 패턴이 많았습니다 → **MCU 샘플링보다 Hotspot/서버 업로드·카메라 부하**가 원인인 경우가 많습니다. 센서 100 Hz를 유지하려면 카메라 fps/해상도를 먼저 낮추는 편이 유리합니다.

---

## 5. 핵심 설계 판단

### 5.1 MCU 중앙 통합 (채택)

- **이유:** 서스펜션·구동·조향을 **동일 `stm_ms` 타임라인**에 맞춤
- **장점:** 단일 UART, 디버그(Live Expressions)·확장 용이
- **리스크:** MCU/UART가 단일 장애점, CSV가 길어지면 UART/무선 부하 증가

### 5.2 대안이었던 분산 전송

- 센서·카메라를 각자 올리거나 주기만 다르게 하는 방식
- 무선 부하는 나눌 수 있으나 **시각 동기·배선·프로토콜 복잡도**가 커짐

### 5.3 샘플링 100 Hz / UART 921600

- 200 Hz → 100 Hz: 루프·UART·업로드 여유
- 460800 ↔ 921600: 용량 vs EMI/배선 안정성 트레이드오프. 근거리면 921600 가능

### 5.4 리니어 캘리브레이션을 소프트웨어로

- 채널마다 풀전압이 다름 (하드웨어·케이블·장착)
- 코드에서 채널별 `vmax`로 100 mm 맞춤 — **배선 불량(신호/파워 교차)은 캘리로 고칠 수 없음**

---

## 6. 디버그 체크리스트

### STM Live Expressions 예

```text
adc_linear_readings[0..3]   // FR,RR,RL,FL
adc_debug_raw[i]
g_can_vehicle_state.rx_total
g_lws_state.rx_count
uart4_tx_overflow
sensor_uart_overflow
main_loop_count
```

### 증상별

| 증상 | 우선 의심 |
|------|-----------|
| ADC만 나오고 CAN 0 | CAN 배선/트랜시버/핀맵, `can1`/`can2` |
| 중간인데 리니어 100 고정, raw≈4095 | 신호선–VCC 단락/교차 |
| 미연결 채널 값이 튐 | 플로팅 핀 (정상) |
| 로그 Hz만 떨어짐, `stm_ms`는 ~10 ms | Pi→서버 Wi‑Fi / 카메라 부하 |
| -553 mm 등 이상값 다수 | UART 줄 깨짐·컬럼 밀림 |

---

## 7. 빌드·플래시·실행

### STM32

1. STM32CubeIDE에서 `device/ECU_test` 오픈
2. Build → Debug/Run으로 보드에 플래시
3. UART4 921600, CAN 핀맵이 배선과 일치하는지 확인

### Raspberry Pi

```bash
cd "device/raspberry pi 5"
pip install pyserial requests
python3 data_send_server.py --port /dev/ttyAMA0 --baud 921600
```

카메라:

```bash
python3 camera.py --fps 15
```

---

## 8. 집 서버 원격 접속 (포트포워딩 + SSH)

계측 서버(`udp_logger` / `udp_realtime` / camera 등)는 **집 PC(노트북)** 에서 돌리고, 현장(차·Pi)은 Hotspot Wi‑Fi로 그 서버에 데이터를 올립니다.  
서버 노트북은 항상 옆에 둘 수 없으므로, **집 공유기 포트포워딩 + OS 방화벽에서 SSH(22) 허용**해 두고, 밖에서는 **공인 IP 또는 DDNS 도메인**으로 SSH 접속해 노트북을 원격 제어했습니다.

### 8.1 전체 흐름

```text
[밖: 본인 노트북 / 폰]
        │  ssh user@<공인IP또는도메인> -p 22
        ▼
[인터넷]
        ▼
[집 공유기]  ── 포트포워딩: 외부 22 → 서버노트북 사설IP:22
        │         (필요 시 8000/8011/8012 등 HTTP도 포워딩)
        ▼
[서버 노트북]  OpenSSH 서버 수신
        │
        ├─ 터미널로 서비스 재시작, 로그 확인, 코드 pull …
        └─ (터널/브라우저) 로거·실시간 페이지 점검
```

즉 **“서버 = 집 노트북”** 이고, **“원격 = SSH로 그 노트북에 들어가 조작”** 입니다.

### 8.2 서버 노트북 준비 (SSH 서버)

1. OS에 **OpenSSH Server** 설치·활성화  
   - Linux: `openssh-server` 설치 후 `sshd` 기동  
   - Windows: 선택적 기능에서 OpenSSH 서버 사용 가능
2. 로그인할 **계정/비밀번호 또는 SSH 키** 준비 (키 인증 권장)
3. 노트북이 절전으로 네트워크가 끊기지 않게 **절전/디스플레이 끄기 설정** 조정
4. 가능하면 공유기 DHCP에서 서버 PC에 **고정 사설 IP** (예: `192.168.0.50`) 예약

접속 예:

```bash
ssh <username>@<공인IP또는도메인>
# 포트가 22가 아니면
ssh -p <포트> <username>@<공인IP또는도메인>
```

접속되면 집 노트북 셸이 열리고, 여기서 `server/` 서비스 기동·재시작·로그 확인 등 **원격으로 서버를 조종**할 수 있습니다.

### 8.3 OS 방화벽 — 22번(SSH) 허용

노트북 방화벽에서 **인바운드 TCP 22** 를 허용해야 공유기까지 들어온 SSH가 PC에 도달합니다.

- Linux (`ufw` 예): `sudo ufw allow 22/tcp` 후 `sudo ufw enable`
- Windows: Windows Defender 방화벽 → 인바운드 규칙에서 OpenSSH(또는 TCP 22) 허용

계측 HTTP도 밖에서도 열려야 하면 동일하게 **8000 / 8011 / 8012** 등을 허용합니다.  
(현장 Pi → 서버만 쓰고, 본인은 SSH만 쓸 계획이면 HTTP는 LAN 전용으로 두고 SSH 터널로만 볼 수도 있습니다.)

### 8.4 집 공유기 포트포워딩

공유기 관리 페이지(보통 `192.168.0.1` / `192.168.1.1`)에서:

| 항목 | 설정 예 |
|------|---------|
| 서비스 이름 | SSH |
| 외부(WAN) 포트 | **22** (또는 보안상 다른 포트로 바꿔 내부 22로 변환) |
| 내부 IP | 서버 노트북 사설 IP |
| 내부 포트 | **22** |
| 프로토콜 | TCP |

추가로 로거/실시간/카메라를 외부에서 직접 열려면:

| 외부 포트 | 내부 | 용도 |
|-----------|------|------|
| 8000 | 노트북:8000 | Logger ingest / CSV |
| 8011 | 노트북:8011 | Realtime |
| 8012 | 노트북:8012 | Camera |

Pi의 `--url` / `--realtime-url` 에는 **공유기 공인 IP 또는 DDNS 도메인**을 넣습니다.  
예: `http://afa2026.ddns.net:8000/ingest`

### 8.5 공인 IP vs 도메인 (DDNS)

- **고정 공인 IP**면 IP로 바로 `ssh user@x.x.x.x`
- 가정용 회선은 IP가 자주 바뀌므로 **DDNS 도메인**(공유기 내장 DDNS 또는 외부 DDNS)을 묶어 두는 편이 편함  
  → 현장·원격 모두 `도메인:포트`로 접속·ingest

Pi/노트북이 Hotspot에 있어도, **목적지가 집 공인 IP/도메인**이면 인터넷 경유로 집 서버에 도달합니다.

### 8.6 실제로 한 일 (운용)

1. 집 노트북에 서버 스택 실행 (`start_servers.sh` 등)
2. 공유기에서 **22 → 노트북**, 필요 시 **8000/8011/8012** 포워딩
3. 노트북 방화벽에서 해당 포트 허용
4. 밖(또는 차 근처)에서 **SSH로 집 노트북 접속** → 서비스 상태 확인, 재시작, 설정 수정
5. 현장 Pi는 Hotspot으로 인터넷 올린 뒤, **도메인/공인 IP의 ingest URL**로 데이터·카메라 전송

이 구성으로 **서버 노트북을 집에 둔 채 원격으로 제어**하며 계측 파이프라인을 유지했습니다.

### 8.7 보안 주의 (필수)

- SSH를 인터넷에 열면 **무차별 대입 공격** 대상이 됩니다.
  - 가능하면 **키 인증만** 허용, 비밀번호 로그인 비활성
  - 외부 SSH 포트를 22 대신 **임의 포트**로 포워딩
  - `fail2ban` 등 차단, 강한 비밀번호/키 사용
- 공유기·PC 관리자 비밀번호를 기본값으로 두지 말 것
- README/깃에 **실제 공인 IP·계정·비밀번호·개인 도메인 크리덴셜을 올리지 말 것**
- 장기간 안 쓰면 포워딩·방화벽 규칙을 끄는 것을 권장

---

## 9. 라이선스 / 주의

- STM HAL/CMSIS는 ST 라이선스 조건을 따릅니다.
- `.env` / 실제 ingest URL·자격 증명은 커밋하지 마세요. `*.env.example`만 사용하세요.
- `Debug/` 빌드 산출물, `__pycache__`, 셸 히스토리는 저장소에서 제외하는 것을 권장합니다.
- 집 서버 SSH/포트포워딩은 편리하지만 노출면이 생기므로 [§8.7](#87-보안-주의-필수)을 지키세요.

---

## 10. 변경 이력 (device 관점 요약)

- STM32 슈퍼루프 + CAN1/2 파싱 + UART CSV 파이프라인
- 리니어 ADC  Averaging/EMA + 채널별 vmax 캘리
- UART DMA TX, baud 921600, 샘플 100 Hz / flush 20 Hz
- Pi `Uart_stm` / `data_send_server` / 카메라 연동
- `can1`/`can2` 링크 플래그, 인버터 토크·Id/Iq 필드 확장
- 실차 EMI·케이블·핫스팟 병목 경험을 반영한 운용 가이드
- 집 노트북 서버 + 공유기 포트포워딩 + SSH(22) 원격 제어 운용 문서화
