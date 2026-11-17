/*
  QXR Wiegand Swipe Simulator - Uno agent firmware
  Arduino Uno + ScrewShield 1.0, controlled over USB serial by the Raspberry Pi.

  The Uno only does the time-critical part: putting Wiegand pulses on the wires.
  The Pi decides what to send and when, keeps the clock, and logs everything.

  Wiring
    R1 D0=2 D1=3 | R2 D0=4 D1=5 | R3 D0=6 D1=7 | R4 D0=8 D1=9
    Uno GND -> panel reader ground
    Feedback inputs (optional, dry contacts to GND): A0, A1, A2, A3
      e.g. wire the panel's lock relay (COM/NO) between A0 and GND.

  Serial: 115200 baud, commands end with newline. Every command gets exactly one
  reply line starting with OK or ERR. Lines starting with EVT can arrive at any time.

    PING                          -> OK PONG <version>
    CFG <pulse_us> <interval_us>  -> OK CFG <pulse_us> <interval_us>
    W <reader 1-4> <26|34> <fc> <card>
                                  -> OK W <reader> <start_ms> <end_ms>
    K <reader 1-4> <4|8> <keys> <gap_ms>      keys: 0-9 * #
                                  -> OK K <reader> <start_ms> <end_ms>
    B <reader 1-4> <bits>         bits: string of 0/1, up to 64 (raw frame, no parity added)
                                  -> OK B <reader> <start_ms> <end_ms>
    IN                            -> OK IN <a0> <a1> <a2> <a3>   (1 = contact closed)
    EVT IN <n 1-4> <0|1> <ms>     (sent by the Uno when a feedback input changes)
*/

#define FW_VERSION "1.0"
const long BAUD = 115200;

const uint8_t D0_PINS[4] = { 2, 4, 6, 8 };   // DATA0 (green)
const uint8_t D1_PINS[4] = { 3, 5, 7, 9 };   // DATA1 (white)
const uint8_t IN_PINS[4] = { A0, A1, A2, A3 };

unsigned int pulseUs = 40;
unsigned int intervalUs = 2000;

char line[96];
uint8_t lineLen = 0;

uint8_t inState[4];
uint8_t inRaw[4];
unsigned long inChanged[4];
const unsigned long DEBOUNCE_MS = 20;

// ---------------------------------------------------------------- Wiegand output

void sendBit(uint8_t r, bool one) {
  uint8_t pin = one ? D1_PINS[r] : D0_PINS[r];
  digitalWrite(pin, LOW);
  delayMicroseconds(pulseUs);
  digitalWrite(pin, HIGH);
  delayMicroseconds(intervalUs);
}

uint8_t countBits(uint32_t v, uint8_t from, uint8_t n) {
  uint8_t c = 0;
  for (uint8_t i = 0; i < n; i++) if ((v >> (from + i)) & 1UL) c++;
  return c;
}

void sendCard(uint8_t r, uint8_t bits, uint16_t fc, uint16_t card) {
  uint8_t dataBits = (bits == 26) ? 24 : 32;
  uint32_t data = (bits == 26) ? (((uint32_t)(fc & 0xFF) << 16) | card)
                               : (((uint32_t)fc << 16) | card);
  uint8_t half = dataBits / 2;
  bool even = countBits(data, half, half) & 1;
  bool odd  = !(countBits(data, 0, half) & 1);
  sendBit(r, even);
  for (int8_t i = dataBits - 1; i >= 0; i--) sendBit(r, (data >> i) & 1UL);
  sendBit(r, odd);
}

void sendKey(uint8_t r, uint8_t fmt, uint8_t k) {
  if (fmt == 8) for (int8_t i = 3; i >= 0; i--) sendBit(r, ((~k) >> i) & 1);
  for (int8_t i = 3; i >= 0; i--) sendBit(r, (k >> i) & 1);
}

// ---------------------------------------------------------------- command parsing

char* nextTok(char** p) {
  while (**p == ' ') (*p)++;
  if (!**p) return NULL;
  char* s = *p;
  while (**p && **p != ' ') (*p)++;
  if (**p) { **p = 0; (*p)++; }
  return s;
}

bool toLong(const char* s, long lo, long hi, long* out) {
  if (!s || !*s) return false;
  char* end;
  long v = strtol(s, &end, 10);
  if (*end || v < lo || v > hi) return false;
  *out = v;
  return true;
}

void replyTimes(const char* cmd, long r, unsigned long t0) {
  Serial.print(F("OK ")); Serial.print(cmd); Serial.print(' ');
  Serial.print(r); Serial.print(' ');
  Serial.print(t0); Serial.print(' ');
  Serial.println(millis());
}

void handle(char* s) {
  char* p = s;
  char* cmd = nextTok(&p);
  if (!cmd) return;
  for (char* c = cmd; *c; c++) *c = toupper(*c);
  long r, a, b, c;

  if (!strcmp(cmd, "PING")) {
    Serial.print(F("OK PONG ")); Serial.println(F(FW_VERSION));

  } else if (!strcmp(cmd, "CFG")) {
    if (!toLong(nextTok(&p), 10, 500, &a) || !toLong(nextTok(&p), 100, 16000, &b)) {
      Serial.println(F("ERR CFG needs: pulse_us 10-500, interval_us 100-16000")); return;
    }
    pulseUs = a; intervalUs = b;
    Serial.print(F("OK CFG ")); Serial.print(pulseUs); Serial.print(' '); Serial.println(intervalUs);

  } else if (!strcmp(cmd, "W")) {
    if (!toLong(nextTok(&p), 1, 4, &r) || !toLong(nextTok(&p), 26, 34, &a) || (a != 26 && a != 34) ||
        !toLong(nextTok(&p), 0, 65535, &b) || !toLong(nextTok(&p), 0, 65535, &c) || (a == 26 && b > 255)) {
      Serial.println(F("ERR W needs: reader 1-4, format 26|34, fc, card")); return;
    }
    unsigned long t0 = millis();
    sendCard(r - 1, a, b, c);
    replyTimes("W", r, t0);

  } else if (!strcmp(cmd, "K")) {
    char* keys;
    if (!toLong(nextTok(&p), 1, 4, &r) || !toLong(nextTok(&p), 4, 8, &a) || (a != 4 && a != 8) ||
        !(keys = nextTok(&p)) || !toLong(nextTok(&p), 0, 10000, &b)) {
      Serial.println(F("ERR K needs: reader 1-4, format 4|8, keys, gap_ms")); return;
    }
    for (char* k = keys; *k; k++)
      if (!isdigit(*k) && *k != '*' && *k != '#') { Serial.println(F("ERR K keys must be 0-9 * #")); return; }
    unsigned long t0 = millis();
    for (char* k = keys; *k; k++) {
      uint8_t code = isdigit(*k) ? *k - '0' : (*k == '*' ? 10 : 11);
      sendKey(r - 1, a, code);
      if (k[1]) delay(b);
    }
    replyTimes("K", r, t0);

  } else if (!strcmp(cmd, "B")) {
    char* bits;
    if (!toLong(nextTok(&p), 1, 4, &r) || !(bits = nextTok(&p)) || strlen(bits) > 64) {
      Serial.println(F("ERR B needs: reader 1-4, bits (0/1, max 64)")); return;
    }
    for (char* k = bits; *k; k++)
      if (*k != '0' && *k != '1') { Serial.println(F("ERR B bits must be 0 or 1")); return; }
    unsigned long t0 = millis();
    for (char* k = bits; *k; k++) sendBit(r - 1, *k == '1');
    replyTimes("B", r, t0);

  } else if (!strcmp(cmd, "IN")) {
    Serial.print(F("OK IN"));
    for (uint8_t i = 0; i < 4; i++) { Serial.print(' '); Serial.print(inState[i]); }
    Serial.println();

  } else {
    Serial.print(F("ERR unknown command ")); Serial.println(cmd);
  }
}

// ---------------------------------------------------------------- feedback inputs

void pollInputs() {
  unsigned long now = millis();
  for (uint8_t i = 0; i < 4; i++) {
    uint8_t v = digitalRead(IN_PINS[i]) == LOW ? 1 : 0;    // closed contact pulls to GND
    if (v != inRaw[i]) { inRaw[i] = v; inChanged[i] = now; }
    else if (v != inState[i] && now - inChanged[i] >= DEBOUNCE_MS) {
      inState[i] = v;
      Serial.print(F("EVT IN ")); Serial.print(i + 1); Serial.print(' ');
      Serial.print(v); Serial.print(' '); Serial.println(inChanged[i]);
    }
  }
}

// ---------------------------------------------------------------- main

void setup() {
  Serial.begin(BAUD);
  for (uint8_t i = 0; i < 4; i++) {
    pinMode(D0_PINS[i], OUTPUT); digitalWrite(D0_PINS[i], HIGH);   // Wiegand idle = high
    pinMode(D1_PINS[i], OUTPUT); digitalWrite(D1_PINS[i], HIGH);
    pinMode(IN_PINS[i], INPUT_PULLUP);
    inRaw[i] = inState[i] = digitalRead(IN_PINS[i]) == LOW ? 1 : 0;
  }
  Serial.print(F("EVT READY ")); Serial.println(F(FW_VERSION));
}

void loop() {
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\r') continue;
    if (ch == '\n') {
      line[lineLen] = 0;
      lineLen = 0;
      handle(line);
    } else if (lineLen < sizeof(line) - 1) {
      line[lineLen++] = ch;
    }
  }
  pollInputs();
}
