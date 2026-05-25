# ESP32 + LD2420 Position Monitor

ESP32 nodes read simple LD2420 text output and post distance readings to the local Python server.

## Files

- `esp32/ld2420_node/ld2420_node.ino`: ESP32 firmware
- `server/server.py`: local HTTP server, tracker, and firmware/provisioning API
- `server/config.example.json`: example room/sensor config
- `server/public/index.html`: browser dashboard

## Wiring

For the LD2420 variant used here:

- `LD2420 3V3 -> ESP32 3V3`
- `LD2420 GND -> ESP32 GND`
- `LD2420 OT1` or `OT2 -> ESP32 GPIO16`
- `LD2420 RX` is not used

Start with `OT1`. If no radar lines arrive, move only that wire to `OT2`.

## Device Payload

The firmware posts only the values the server needs:

```json
{
  "sensor_id": "sensor-a",
  "present": true,
  "distance_cm": 120
}
```

By default it posts every `300 ms`. If the radar is silent for `2000 ms`, the node reports no presence and distance `0`.

## Provisioning

Wi-Fi, server address, sensor id, server port, and post interval are stored on the ESP32 with `Preferences`.

Use the dashboard's `Firmware & Provisioning` panel to flash and provision a device. Changing those settings later should only require provisioning again, not editing and rebuilding the firmware source.

## Server

Copy the example config once:

```powershell
Copy-Item .\server\config.example.json .\server\config.json
```

Run:

```powershell
python .\server\server.py
```

Open:

- `http://127.0.0.1:8080`
