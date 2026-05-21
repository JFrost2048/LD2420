# ESP32 + LD2420 Position Monitor

`ESP32 + LD2420` 4세트에서 읽은 거리 데이터를 서버로 모아 웹에서 사람 위치를 추정하는 예제입니다.

## 구성

- `esp32/ld2420_node/ld2420_node.ino`
- `server/server.py`
- `server/config.example.json`
- `server/public/index.html`

## ESP32 설정

업로드 전 아래 값만 각 보드에 맞게 수정합니다.

```cpp
const char* WIFI_SSID = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* SERVER_HOST = "YOUR_SERVER_IP";
const uint16_t SERVER_PORT = 8080;
const char* SENSOR_ID = "sensor-a";
```

센서 ID 예시:

- `sensor-a`
- `sensor-b`
- `sensor-c`
- `sensor-d`

## 배선

지금 프로젝트에서 사용한 LD2420 5핀 모듈 기준:

- `3V3 -> ESP32 3V3`
- `GND -> ESP32 GND`
- `OT1` 또는 `OT2 -> ESP32 GPIO16`
- `RX`는 기본 예제에서 미사용

모듈에 따라 `OT1` 대신 `OT2`가 실제 출력 핀일 수 있습니다.

## 서버 실행

설정 예시 파일 복사:

```powershell
Copy-Item .\server\config.example.json .\server\config.json
```

실행:

```powershell
python .\server\server.py
```

브라우저:

- `http://127.0.0.1:8080`

## 참고

- `server/config.json`은 개인 환경 설정 파일이라 기본적으로 git에 포함하지 않습니다.
- 방 크기와 센서 좌표는 웹 UI에서 수정하거나 `server/config.json`에서 직접 바꿀 수 있습니다.
