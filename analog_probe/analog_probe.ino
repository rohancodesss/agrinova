// Prints A0..A5 raw every 500 ms.
void setup() { Serial.begin(9600); }
void loop() {
  Serial.print("A0="); Serial.print(analogRead(A0));
  Serial.print(" A1="); Serial.print(analogRead(A1));
  Serial.print(" A2="); Serial.print(analogRead(A2));
  Serial.print(" A3="); Serial.print(analogRead(A3));
  Serial.print(" A4="); Serial.print(analogRead(A4));
  Serial.print(" A5="); Serial.println(analogRead(A5));
  delay(500);
}
