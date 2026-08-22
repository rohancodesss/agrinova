/*
  AgriNova Arduino Controller
  Controls servo and display, receives commands from Mac bridge over serial
  Expects commands: ROTATE, STOP, ANGLE:value
*/

#include <Servo.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <time.h>

// Servo config
Servo servo;
const int SERVO_PIN = 9;
const int SERVO_MIN = 0;
const int SERVO_MAX = 180;
int current_angle = 90;

// Display config (16x2 LCD I2C at address 0x27)
LiquidCrystal_I2C lcd(0x27, 16, 2);

// Servo rotation control
bool rotating = false;
int rotation_speed = 2;  // degrees per update
unsigned long last_update = 0;
const unsigned long UPDATE_INTERVAL = 50;  // ms

// Status tracking
String last_command = "IDLE";
unsigned long last_command_time = 0;

void setup() {
  Serial.begin(9600);

  // Initialize servo
  servo.attach(SERVO_PIN);
  servo.write(current_angle);

  // Initialize display
  lcd.init();
  lcd.backlight();
  lcd.setCursor(0, 0);
  lcd.print("AgriNova Ready");
  lcd.setCursor(0, 1);
  lcd.print("Waiting...");

  delay(1000);
  lcd.clear();

  Serial.println("[arduino] AgriNova servo controller started");
}

void loop() {
  // Handle serial commands from Mac bridge
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    process_command(cmd);
    last_command_time = millis();
  }

  // Update rotating servo
  if (rotating) {
    unsigned long now = millis();
    if (now - last_update >= UPDATE_INTERVAL) {
      last_update = now;
      update_rotation();
    }
  }

  // Update display every 500ms
  static unsigned long last_display = 0;
  if (millis() - last_display >= 500) {
    last_display = millis();
    update_display();
  }
}

void process_command(String cmd) {
  if (cmd == "ROTATE") {
    rotating = true;
    last_command = "ROTATING";
    Serial.println("[arduino] servo rotating");
  }
  else if (cmd == "STOP") {
    rotating = false;
    last_command = "STOPPED";
    Serial.println("[arduino] servo stopped");
  }
  else if (cmd.startsWith("ANGLE:")) {
    rotating = false;
    int angle = cmd.substring(6).toInt();
    angle = constrain(angle, SERVO_MIN, SERVO_MAX);
    current_angle = angle;
    servo.write(current_angle);
    last_command = "ANGLE:" + String(angle);
    Serial.println("[arduino] servo set to " + String(angle) + " degrees");
  }
  else if (cmd == "PING") {
    Serial.println("[arduino] PONG");
  }
  else {
    Serial.println("[arduino] unknown command: " + cmd);
  }
}

void update_rotation() {
  current_angle += rotation_speed;
  if (current_angle >= SERVO_MAX || current_angle <= SERVO_MIN) {
    rotation_speed = -rotation_speed;  // Reverse direction
    current_angle = constrain(current_angle, SERVO_MIN, SERVO_MAX);
  }
  servo.write(current_angle);
}

void update_display() {
  lcd.setCursor(0, 0);

  // Line 1: Status and angle
  if (rotating) {
    lcd.print("ROT: ");
    if (current_angle < 100) lcd.print(" ");
    lcd.print(current_angle);
    lcd.print("deg   ");
  } else {
    lcd.print("STOP: ");
    if (current_angle < 100) lcd.print(" ");
    lcd.print(current_angle);
    lcd.print("deg   ");
  }

  // Line 2: Last command / idle time
  lcd.setCursor(0, 1);
  unsigned long idle_time = (millis() - last_command_time) / 1000;

  if (idle_time < 60) {
    lcd.print("Cmd: ");
    String cmd_display = last_command;
    if (cmd_display.length() > 10) {
      cmd_display = cmd_display.substring(0, 10);
    }
    lcd.print(cmd_display);
    if (cmd_display.length() < 10) {
      for (int i = 0; i < (10 - cmd_display.length()); i++) {
        lcd.print(" ");
      }
    }
  } else {
    lcd.print("Idle ");
    lcd.print(idle_time);
    lcd.print("s      ");
  }
}
