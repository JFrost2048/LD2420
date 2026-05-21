#include <WiFi.h>
#include <WiFiClient.h>
#include <HTTPClient.h>
const char* WIFI_SSID = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* SERVER_HOST = "YOUR_SERVER_IP";
const uint16_t SERVER_PORT = 8080;
const char* SENSOR_ID = "sensor-a";
// For this LD2420 variant, OT1/OT2 appear to be one-way UART-style outputs.
// Start with OT1 -> ESP32 RX pin. If no data comes in, move the OT wire to OT2.
static const int RADAR_RX_PIN = 16;
static const uint32_t RADAR_BAUD = 115200;
HardwareSerial RadarSerial(1);
String radarLine = "";
bool present = false;
bool moving = false;
bool stationary = false;
int movingDistanceCm = 0;
int stationaryDistanceCm = 0;
int movingEnergy = 0;
int stationaryEnergy = 0;
unsigned long lastSeenMs = 0;
unsigned long lastPostMs = 0;
const unsigned long SENSOR_TIMEOUT_MS = 2000;
const unsigned long POST_INTERVAL_MS = 300;
void printWiringGuide() {
  Serial.println("LD2420 text-output wiring guide:");
  Serial.println("  LD2420 3V3 -> ESP32 3V3");
  Serial.println("  LD2420 GND -> ESP32 GND");
  Serial.println("  LD2420 OT1 -> ESP32 GPIO16 (RX only)");
  Serial.println("  LD2420 RX  -> leave disconnected for now");
  Serial.println("  LD2420 OT2 -> leave disconnected for now");
  Serial.println("If no serial lines arrive, move only this one wire:");
  Serial.println("  LD2420 OT2 -> ESP32 GPIO16");
}
void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to Wi-Fi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("Wi-Fi connected, IP: ");
  Serial.println(WiFi.localIP());
}
void applyTimeout() {
  if (millis() - lastSeenMs > SENSOR_TIMEOUT_MS) {
    present = false;
    moving = false;
    stationary = false;
    movingDistanceCm = 0;
    stationaryDistanceCm = 0;
    movingEnergy = 0;
    stationaryEnergy = 0;
  }
}
void handleRadarLine(String line) {
  line.trim();
  if (line.length() == 0) {
    return;
  }
  Serial.print("RADAR: ");
  Serial.println(line);
  if (line == "ON") {
    present = true;
    moving = true;
    stationary = false;
    lastSeenMs = millis();
    return;
  }
  if (line == "OFF") {
    present = false;
    moving = false;
    stationary = false;
    movingDistanceCm = 0;
    stationaryDistanceCm = 0;
    movingEnergy = 0;
    stationaryEnergy = 0;
    return;
  }
  if (line.startsWith("Range ")) {
    int value = line.substring(6).toInt();
    if (value > 0) {
      present = true;
      moving = true;
      stationary = false;
      movingDistanceCm = value;
      stationaryDistanceCm = value;
      movingEnergy = 100;
      stationaryEnergy = 0;
      lastSeenMs = millis();
    }
    return;
  }
}
void readRadar() {
  while (RadarSerial.available()) {
    char c = (char)RadarSerial.read();
    if (c == '\r') {
      continue;
    }
    if (c == '\n') {
      handleRadarLine(radarLine);
      radarLine = "";
    } else {
      radarLine += c;
      if (radarLine.length() > 80) {
        radarLine = "";
      }
    }
  }
  applyTimeout();
}
String buildJsonPayload() {
  String json = "{";
  json += "\"sensor_id\":\"" + String(SENSOR_ID) + "\",";
  json += "\"timestamp_ms\":" + String(millis()) + ",";
  json += "\"present\":" + String(present ? "true" : "false") + ",";
  json += "\"moving\":" + String(moving ? "true" : "false") + ",";
  json += "\"stationary\":" + String(stationary ? "true" : "false") + ",";
  json += "\"moving_distance_cm\":" + String(movingDistanceCm) + ",";
  json += "\"stationary_distance_cm\":" + String(stationaryDistanceCm) + ",";
  json += "\"moving_energy\":" + String(movingEnergy) + ",";
  json += "\"stationary_energy\":" + String(stationaryEnergy);
  json += "}";
  return json;
}
void postRadarState() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }
  WiFiClient client;
  Serial.print("Checking TCP connection to ");
  Serial.print(SERVER_HOST);
  Serial.print(":");
  Serial.println(SERVER_PORT);
  if (!client.connect(SERVER_HOST, SERVER_PORT)) {
    Serial.println("TCP connect failed.");
    Serial.println("Possible causes:");
    Serial.println("  1. Python server is not running");
    Serial.println("  2. Windows firewall is blocking port 8080");
    Serial.println("  3. Phone hotspot/client isolation is blocking device-to-device access");
    return;
  }
  client.stop();
  String url = "http://" + String(SERVER_HOST) + ":" + String(SERVER_PORT) + "/api/sensor";
  String payload = buildJsonPayload();
  HTTPClient http;
  http.setTimeout(3000);
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  int statusCode = http.POST(payload);
  Serial.print("POST ");
  Serial.print(statusCode);
  if (statusCode <= 0) {
    Serial.print(" (");
    Serial.print(http.errorToString(statusCode));
    Serial.print(")");
  }
  Serial.print(" -> ");
  Serial.println(payload);
  http.end();
}
void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("SETUP START");
  printWiringGuide();
  connectWiFi();
  Serial.println("WIFI OK");

  RadarSerial.begin(RADAR_BAUD, SERIAL_8N1, RADAR_RX_PIN, -1);
  Serial.println("RADAR UART OK");
  Serial.print("Listening for LD2420 text output on GPIO");
  Serial.print(RADAR_RX_PIN);
  Serial.print(" at ");
  Serial.print(RADAR_BAUD);
  Serial.println(" baud");
  Serial.println("SETUP DONE");
}

void loop() {
  static unsigned long lastHeartbeatMs = 0;
  readRadar();

  unsigned long now = millis();
  if (now - lastHeartbeatMs >= 2000) {
    lastHeartbeatMs = now;
    Serial.println("LOOP HEARTBEAT");
  }
  if (now - lastPostMs >= POST_INTERVAL_MS) {
    lastPostMs = now;
    postRadarState();
  }
}
