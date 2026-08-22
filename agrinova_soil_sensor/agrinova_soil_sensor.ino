/*
  AgriNova Arduino node
  - Soil moisture on A0
  - Servo control on A3 (HIGH/LOW)
  - Mist-maker relay on A2
  Talks to the Mac bridge over USB serial at 9600 baud.
*/

#include <DHT.h>

// Pin assignments
const int SOIL_MOISTURE_PIN = A0;
const int DHT_PIN = A1;     // DHT11 temperature/humidity (data pin)
const int SERVO_PIN = A3;   // digital output only (HIGH/LOW)
const int MIST_PIN  = A2;   // relay IN for the mist maker

// Most relay modules are ACTIVE LOW (IN=LOW closes the relay).
// If the mist runs when it should be OFF, flip this to false and re-upload.
const bool MIST_RELAY_ACTIVE_LOW = false;

DHT dht(DHT_PIN, DHT11);

// Soil calibration (raw ADC values)
const int SOIL_DRY = 800;
const int SOIL_WET = 400;

// State
bool servo_active = false;
bool mist_active  = false;
int  moisture_percent = 0;
int  moisture_raw = 0;
float temperature = 0;
float humidity = 0;
bool wet = false;
unsigned long last_reading = 0;
const unsigned long READING_INTERVAL = 5000;

void set_mist(bool on) {
  mist_active = on;
  bool level = MIST_RELAY_ACTIVE_LOW ? !on : on;
  digitalWrite(MIST_PIN, level ? HIGH : LOW);
}

void setup() {
  // Drive the relay to OFF before anything else so the mist never
  // sprays during boot.
  pinMode(MIST_PIN, OUTPUT);
  set_mist(false);

  pinMode(SERVO_PIN, OUTPUT);
  digitalWrite(SERVO_PIN, LOW);

  dht.begin();

  Serial.begin(9600);
  Serial.println("[arduino] AgriNova node: soil A0, DHT11 A1, mist relay A2, servo A3");
  Serial.println("[arduino] commands: PING, STATUS, MOISTURE?, ROTATE, STOP, MIST_ON, MIST_OFF, RAW?");
}

void loop() {
  if (millis() - last_reading >= READING_INTERVAL) {
    last_reading = millis();
    read_sensors();
    send_data();
  }

  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if (cmd.length() > 0) handle_command(cmd);
  }
}

void read_sensors() {
  int raw = analogRead(SOIL_MOISTURE_PIN);
  moisture_raw = raw;
  moisture_percent = map(raw, SOIL_DRY, SOIL_WET, 0, 100);
  moisture_percent = constrain(moisture_percent, 0, 100);
  wet = (moisture_percent >= 40);

  // DHT11: keep the last good reading if this one fails (it occasionally does)
  float t = dht.readTemperature();
  float h = dht.readHumidity();
  if (!isnan(t)) temperature = t;
  if (!isnan(h)) humidity = h;

  Serial.print("[sensor] moisture raw: ");
  Serial.print(raw);
  Serial.print(" -> ");
  Serial.print(moisture_percent);
  Serial.print("%  temp: ");
  Serial.print(temperature, 1);
  Serial.print("C  humidity: ");
  Serial.print(humidity, 0);
  Serial.println("%");
}

void send_data() {
  // DATA:moisture,temp,humidity,WET|DRY,raw,MIST_ON|MIST_OFF,SERVO_ON|SERVO_OFF
  Serial.print("DATA:");
  Serial.print(moisture_percent);
  Serial.print(",");
  Serial.print((int)temperature);
  Serial.print(".");
  Serial.print((int)(temperature * 10) % 10);
  Serial.print(",");
  Serial.print((int)humidity);
  Serial.print(",");
  Serial.print(wet ? "WET" : "DRY");
  Serial.print(",");
  Serial.print(moisture_raw);
  Serial.print(",");
  Serial.print(mist_active ? "MIST_ON" : "MIST_OFF");
  Serial.print(",");
  Serial.println(servo_active ? "SERVO_ON" : "SERVO_OFF");
}

void handle_command(String cmd) {
  if (cmd == "PING") {
    Serial.println("PONG");
  }
  else if (cmd == "RAW?") {
    Serial.print("RAW:");
    Serial.println(analogRead(SOIL_MOISTURE_PIN));
  }
  else if (cmd == "MOISTURE?") {
    Serial.print("MOISTURE:");
    Serial.println(moisture_percent);
  }
  else if (cmd == "STATUS") {
    send_data();
    Serial.print("[arduino] servo=");
    Serial.print(servo_active ? "ON" : "OFF");
    Serial.print(" mist=");
    Serial.println(mist_active ? "ON" : "OFF");
  }
  else if (cmd == "ROTATE") {
    digitalWrite(SERVO_PIN, HIGH);
    servo_active = true;
    Serial.println("[arduino] servo ON (A3 HIGH)");
  }
  else if (cmd == "STOP") {
    digitalWrite(SERVO_PIN, LOW);
    servo_active = false;
    Serial.println("[arduino] servo OFF (A3 LOW)");
  }
  else if (cmd == "MIST_ON") {
    set_mist(true);
    Serial.println("MIST:ON");
  }
  else if (cmd == "MIST_OFF") {
    set_mist(false);
    Serial.println("MIST:OFF");
  }
  else {
    Serial.print("[arduino] unknown command: ");
    Serial.println(cmd);
  }
}
