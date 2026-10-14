/*
  100 PSI pressure transducer -> Seeed Studio XIAO

  Sensor: 3-wire, 5V supply, 0.5V (0 psi) .. 4.5V (100 psi) output.
    Red    -> XIAO 5V pin  (only live when powered over USB / 5V)
    Black  -> XIAO GND
    Yellow/Green (signal) -> voltage divider -> XIAO A0

  The XIAO ADC pins are 3.3V MAX. The sensor output reaches 4.5V
  (and ~5V if a wire fails), so it MUST go through a divider:

      signal ---[ R1 = 10k ]---+---[ R2 = 20k ]--- GND
                               |
                               A0

  Divider ratio = R2 / (R1 + R2) = 0.667  -> 4.5V becomes 3.0V, 5V becomes 3.33V.
  Adding a 0.1uF capacitor from A0 to GND helps reduce noise.
*/

// ---------- Configuration ----------
const int   SENSOR_PIN     = A0;
const float R1             = 10000.0;   // ohms, signal -> A0
const float R2             = 20000.0;   // ohms, A0 -> GND
const float V_MIN          = 0.5;       // sensor output at 0 psi (volts)
const float V_MAX          = 4.5;       // sensor output at full scale (volts)
const float P_MAX_PSI      = 100.0;     // sensor full-scale range
const int   SAMPLES        = 32;        // readings averaged per measurement
const unsigned long PERIOD_MS = 250;    // time between printed readings

// Set true to measure the 0 psi offset at boot.
// The sensor must be open to the air (no pressure) when the board starts.
const bool  AUTO_ZERO      = true;

// Board-specific ADC settings (ESP32 boards use calibrated millivolt reads)
#if !defined(ARDUINO_ARCH_ESP32)
const float ADC_REF_V      = 3.3;
const int   ADC_BITS       = 12;
#endif

float zeroOffsetV = 0.0;   // correction learned during auto-zero

// Returns the voltage at the A0 pin, averaged over SAMPLES readings
float readPinVoltage() {
  uint32_t total = 0;
  for (int i = 0; i < SAMPLES; i++) {
#if defined(ARDUINO_ARCH_ESP32)
    total += analogReadMilliVolts(SENSOR_PIN);
#else
    total += analogRead(SENSOR_PIN);
#endif
    delayMicroseconds(200);
  }
  float avg = (float)total / SAMPLES;

#if defined(ARDUINO_ARCH_ESP32)
  return avg / 1000.0;
#else
  return avg * ADC_REF_V / ((1 << ADC_BITS) - 1);
#endif
}

// Converts the pin voltage back to the sensor's actual output voltage
float readSensorVoltage() {
  return readPinVoltage() * (R1 + R2) / R2;
}

float voltageToPsi(float v) {
  float psi = (v - V_MIN - zeroOffsetV) * P_MAX_PSI / (V_MAX - V_MIN);
  if (psi < 0) psi = 0;   // hide small negative readings caused by noise
  return psi;
}

void setup() {
  Serial.begin(115200);
  unsigned long start = millis();
  while (!Serial && millis() - start < 3000) { }  // wait briefly for USB serial

#if defined(ARDUINO_ARCH_ESP32)
  analogSetPinAttenuation(SENSOR_PIN, ADC_11db);  // full ~0-3.1V range
#else
  analogReadResolution(ADC_BITS);
#endif

  if (AUTO_ZERO) {
    delay(500);  // let the sensor settle after power-up
    float v = readSensorVoltage();
    // Only trust the zero if it's close to the expected 0.5V
    if (v > 0.3 && v < 0.8) {
      zeroOffsetV = v - V_MIN;
    }
    Serial.print("Zero offset (V): ");
    Serial.println(zeroOffsetV, 3);
  }

  Serial.println("volts,psi,bar,kPa");
}

void loop() {
  float v = readSensorVoltage();

  // Under ~0.2V usually means a disconnected/broken signal wire;
  // above ~4.8V usually means a short or a faulty sensor.
  if (v < 0.2) {
    Serial.println("FAULT: signal too low - check wiring / 5V supply");
  } else if (v > 4.8) {
    Serial.println("FAULT: signal too high - check sensor");
  } else {
    float psi = voltageToPsi(v);
    Serial.print(v, 3);               Serial.print(',');
    Serial.print(psi, 2);             Serial.print(',');
    Serial.print(psi * 0.0689476, 3); Serial.print(',');
    Serial.println(psi * 6.89476, 1);
  }

  delay(PERIOD_MS);
}
