/* ========================================
 * AFA-26 APPS - DIFF 전용 (밴드 안에서만 판정)
 *
 *   판정 구간  APS1 1.20 ~ 3.40V
 *              APS2 0.60 ~ 1.70V
 *   구간 밖은 판정 안 함 -> D8 LOW
 * ======================================== */

#define SAMPLE_PERIOD_MS   100

const int aps1Pin     = A0;
const int aps2Pin     = A1;
const int errorOutPin = 8;

const float VREF = 5.0;

const float APS1_CT    = 0.60;
const float APS1_RANGE = 2.90;
const float APS2_CT    = 0.30;
const float APS2_RANGE = 1.45;

const float PLAUS_PCT = 10.0;

// 판정 구간  이 안에서만 판정함
const float APS1_BAND_LO = 1.20,  APS1_BAND_HI = 3.40;
const float APS2_BAND_LO = 0.60,  APS2_BAND_HI = 1.70;

// 단선 검출용
// const float APS1_MIN = 0.30, APS1_MAX = 3.80;
// const float APS2_MIN = 0.15, APS2_MAX = 2.10;

const int ERROR_COUNT_THRESHOLD = (100 / SAMPLE_PERIOD_MS) + 1;

const unsigned long PRINT_INTERVAL_MS = 200;

int errorCount = 0;
unsigned long lastPrintMs = 0;
bool d8State = false;

void setup() {
  Serial.begin(115200);
  pinMode(errorOutPin, OUTPUT);
  digitalWrite(errorOutPin, LOW);

  Serial.println(F("=== AFA-26 APPS ==="));
  Serial.println(F("band  V1 1.20~3.40V   V2 0.60~1.70V"));
  Serial.print(F("tolerance +/-")); Serial.print(PLAUS_PCT, 0);
  Serial.println(F("%"));
  Serial.println();
}

void loop() {
  float volt1 = analogRead(aps1Pin) * (VREF / 1023.0);
  float volt2 = analogRead(aps2Pin) * (VREF / 1023.0);

  // volt1 = 4.5;   // 오류 강제 테스트

  float pos1 = (volt1 - APS1_CT) * 100.0 / APS1_RANGE;
  float pos2 = (volt2 - APS2_CT) * 100.0 / APS2_RANGE;
  float err  = pos1 - pos2;

  bool inBand = (volt1 >= APS1_BAND_LO) && (volt1 <= APS1_BAND_HI) &&
                (volt2 >= APS2_BAND_LO) && (volt2 <= APS2_BAND_HI);

  bool diffFault = false;
  if (inBand) {
    diffFault = (err <= -PLAUS_PCT) || (err >= PLAUS_PCT);
  }

  if (diffFault) {
    if (errorCount < 100) errorCount++;
    if (errorCount >= ERROR_COUNT_THRESHOLD) {
      digitalWrite(errorOutPin, HIGH);
      d8State = true;
    }
  }
  else {
    errorCount = 0;
    digitalWrite(errorOutPin, LOW);
    d8State = false;
  }

  unsigned long now = millis();
  if (now - lastPrintMs >= PRINT_INTERVAL_MS) {
    lastPrintMs = now;

    Serial.print(F("V1:"));   Serial.print(volt1, 3);
    Serial.print(F("  V2:")); Serial.print(volt2, 3);

    // 페달 위치 및 편차
    Serial.print(F("  P1:")); Serial.print(pos1, 1);
    Serial.print(F("% P2:")); Serial.print(pos2, 1);
    Serial.print(F("%  DIFF:")); Serial.print(err, 1);
    Serial.print(F("%"));

    Serial.print(inBand ? F("  [ON ]") : F("  [off]"));

    if (d8State) {
      Serial.print(F("   FAULT"));
    }
    else if (errorCount > 0) {
      Serial.print(F("   ERR ")); Serial.print(errorCount);
      Serial.print(F("/"));       Serial.print(ERROR_COUNT_THRESHOLD);
    }
    else {
      Serial.print(F("   OK"));
    }

    Serial.print(F("   D8="));
    Serial.println(d8State ? F("HIGH") : F("LOW"));
  }

  delay(SAMPLE_PERIOD_MS);
}