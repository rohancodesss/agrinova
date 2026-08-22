// AgriNova sensor probe: finds I2C devices and tests DHT11/DHT22 on A1
#include <Wire.h>
#include <DHT.h>

DHT dht22(A1, DHT22);
DHT dht11(A1, DHT11);

void setup() {
  Serial.begin(9600);
  Wire.begin();
  dht22.begin();
  dht11.begin();
  delay(2500);
  Serial.println("PROBE:START");
}

void loop() {
  // --- I2C scan ---
  int found = 0;
  for (byte addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.print("I2C:0x");
      if (addr < 16) Serial.print("0");
      Serial.println(addr, HEX);
      found++;
    }
  }
  if (!found) Serial.println("I2C:NONE");

  // --- DHT22 on A1 ---
  float h22 = dht22.readHumidity();
  float t22 = dht22.readTemperature();
  if (!isnan(h22) && !isnan(t22)) {
    Serial.print("DHT22:"); Serial.print(t22); Serial.print("C,"); Serial.print(h22); Serial.println("%");
  } else {
    Serial.println("DHT22:NONE");
  }
  delay(2200);

  // --- DHT11 on A1 ---
  float h11 = dht11.readHumidity();
  float t11 = dht11.readTemperature();
  if (!isnan(h11) && !isnan(t11)) {
    Serial.print("DHT11:"); Serial.print(t11); Serial.print("C,"); Serial.print(h11); Serial.println("%");
  } else {
    Serial.println("DHT11:NONE");
  }
  Serial.println("PROBE:END");
  delay(3000);
}
