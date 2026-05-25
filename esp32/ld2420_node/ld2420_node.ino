#include <WiFi.h>
#include <WiFiClient.h>
#include <HTTPClient.h>
#include <Preferences.h>

const char* DEFAULT_SENSOR_ID = "unprovisioned";
const uint16_t DEFAULT_SERVER_PORT = 8080;
const unsigned long DEFAULT_POST_INTERVAL_MS = 300;

// For this LD2420 variant, OT1/OT2 appear to be one-way UART-style outputs.
// Start with OT1 -> ESP32 RX pin. If no data comes in, move the OT wire to OT2.
static const int RADAR_RX_PIN = 16;
static const uint32_t RADAR_BAUD = 115200;
HardwareSerial RadarSerial(1);
Preferences preferences;

struct DeviceConfig {
  String sensorId = DEFAULT_SENSOR_ID;
  String wifiSsid = "";
  String wifiPassword = "";
  String serverHost = "";
  uint16_t serverPort = DEFAULT_SERVER_PORT;
  unsigned long postIntervalMs = DEFAULT_POST_INTERVAL_MS;
};

DeviceConfig config;
String radarLine = "";
String provisionLine = "";
DeviceConfig pendingConfig;
bool provisioning = false;
bool present = false;
int distanceCm = 0;
unsigned long lastSeenMs = 0;
unsigned long lastPostMs = 0;
const unsigned long SENSOR_TIMEOUT_MS = 2000;

void loadConfig() {
  preferences.begin("ld2420", true);
  config.sensorId = preferences.getString("sensor_id", DEFAULT_SENSOR_ID);
  config.wifiSsid = preferences.getString("wifi_ssid", "");
  config.wifiPassword = preferences.getString("wifi_pass", "");
  config.serverHost = preferences.getString("server_host", "");
  config.serverPort = preferences.getUShort("server_port", DEFAULT_SERVER_PORT);
  config.postIntervalMs = preferences.getULong("post_ms", DEFAULT_POST_INTERVAL_MS);
  preferences.end();
}

void saveConfig(const DeviceConfig& nextConfig) {
  preferences.begin("ld2420", false);
  preferences.putString("sensor_id", nextConfig.sensorId);
  preferences.putString("wifi_ssid", nextConfig.wifiSsid);
  preferences.putString("wifi_pass", nextConfig.wifiPassword);
  preferences.putString("server_host", nextConfig.serverHost);
  preferences.putUShort("server_port", nextConfig.serverPort);
  preferences.putULong("post_ms", nextConfig.postIntervalMs);
  preferences.end();
}

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

bool hasNetworkConfig() {
  return config.wifiSsid.length() > 0 && config.serverHost.length() > 0;
}

void applyProvisionValue(String line) {
  int separator = line.indexOf('=');
  if (separator <= 0) {
    return;
  }

  String key = line.substring(0, separator);
  String value = line.substring(separator + 1);
  key.trim();
  value.trim();

  if (key == "sensor_id") {
    pendingConfig.sensorId = value;
  } else if (key == "wifi_ssid") {
    pendingConfig.wifiSsid = value;
  } else if (key == "wifi_password") {
    pendingConfig.wifiPassword = value;
  } else if (key == "server_host") {
    pendingConfig.serverHost = value;
  } else if (key == "server_port") {
    int port = value.toInt();
    if (port > 0 && port <= 65535) {
      pendingConfig.serverPort = (uint16_t)port;
    }
  } else if (key == "post_interval_ms") {
    unsigned long interval = value.toInt();
    if (interval >= 100) {
      pendingConfig.postIntervalMs = interval;
    }
  }
}

void handleProvisionLine(String line) {
  line.trim();
  if (line.length() == 0) {
    return;
  }

  if (line == "PROVISION_BEGIN") {
    provisioning = true;
    pendingConfig = config;
    Serial.println("PROVISION READY");
    return;
  }

  if (line == "PROVISION_END") {
    if (provisioning) {
      saveConfig(pendingConfig);
      Serial.println("PROVISION SAVED");
      delay(250);
      ESP.restart();
    }
    return;
  }

  if (provisioning) {
    applyProvisionValue(line);
  }
}

void readProvisioning() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') {
      continue;
    }
    if (c == '\n') {
      handleProvisionLine(provisionLine);
      provisionLine = "";
    } else {
      provisionLine += c;
      if (provisionLine.length() > 160) {
        provisionLine = "";
      }
    }
  }
}

bool connectWiFi() {
  if (!hasNetworkConfig()) {
    Serial.println("Missing provisioned Wi-Fi or server config.");
    return false;
  }
  if (WiFi.status() == WL_CONNECTED) {
    return true;
  }
  WiFi.mode(WIFI_STA);
  WiFi.begin(config.wifiSsid.c_str(), config.wifiPassword.c_str());
  Serial.print("Connecting to Wi-Fi");
  unsigned long startMs = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startMs < 15000) {
    readProvisioning();
    delay(500);
    Serial.print(".");
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println();
    Serial.println("Wi-Fi connection timed out.");
    return false;
  }
  Serial.println();
  Serial.print("Wi-Fi connected, IP: ");
  Serial.println(WiFi.localIP());
  return true;
}
void applyTimeout() {
  if (millis() - lastSeenMs > SENSOR_TIMEOUT_MS) {
    present = false;
    distanceCm = 0;
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
    lastSeenMs = millis();
    return;
  }
  if (line == "OFF") {
    present = false;
    distanceCm = 0;
    return;
  }
  if (line.startsWith("Range ")) {
    int value = line.substring(6).toInt();
    if (value > 0) {
      present = true;
      distanceCm = value;
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
  json += "\"sensor_id\":\"" + config.sensorId + "\",";
  json += "\"present\":" + String(present ? "true" : "false") + ",";
  json += "\"distance_cm\":" + String(distanceCm);
  json += "}";
  return json;
}
void postRadarState() {
  if (!connectWiFi()) {
    return;
  }
  WiFiClient client;
  Serial.print("Checking TCP connection to ");
  Serial.print(config.serverHost);
  Serial.print(":");
  Serial.println(config.serverPort);
  if (!client.connect(config.serverHost.c_str(), config.serverPort)) {
    Serial.println("TCP connect failed.");
    Serial.println("Possible causes:");
    Serial.println("  1. Python server is not running");
    Serial.println("  2. Windows firewall is blocking port 8080");
    Serial.println("  3. Phone hotspot/client isolation is blocking device-to-device access");
    return;
  }
  client.stop();
  String url = "http://" + config.serverHost + ":" + String(config.serverPort) + "/api/sensor";
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
  loadConfig();
  Serial.print("Sensor ID: ");
  Serial.println(config.sensorId);
  Serial.print("Post interval ms: ");
  Serial.println(config.postIntervalMs);
  printWiringGuide();

  RadarSerial.begin(RADAR_BAUD, SERIAL_8N1, RADAR_RX_PIN, -1);
  Serial.println("RADAR UART OK");
  Serial.print("Listening for LD2420 text output on GPIO");
  Serial.print(RADAR_RX_PIN);
  Serial.print(" at ");
  Serial.print(RADAR_BAUD);
  Serial.println(" baud");
  connectWiFi();
  Serial.println("SETUP DONE");
}

void loop() {
  static unsigned long lastHeartbeatMs = 0;
  readProvisioning();
  readRadar();

  unsigned long now = millis();
  if (now - lastHeartbeatMs >= 2000) {
    lastHeartbeatMs = now;
    Serial.println("LOOP HEARTBEAT");
  }
  if (now - lastPostMs >= config.postIntervalMs) {
    lastPostMs = now;
    postRadarState();
  }
}
