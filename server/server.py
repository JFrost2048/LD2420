import json
import math
import queue
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
CONFIG_PATH = BASE_DIR / "config.json"
CALIBRATION_PATH = BASE_DIR / "calibration_samples.jsonl"
PROJECT_DIR = BASE_DIR.parent
FIRMWARE_SOURCE_PATH = PROJECT_DIR / "esp32" / "ld2420_node" / "ld2420_node.ino"
ARDUINO_CLI_CANDIDATES = [
    Path("C:/Program Files/Arduino CLI/arduino-cli.exe"),
    Path.home() / "AppData/Local/Programs/Arduino IDE/resources/app/lib/backend/resources/arduino-cli.exe",
]
FIRMWARE_JOBS = {}
FIRMWARE_JOBS_LOCK = threading.Lock()
PREFERRED_SERVER_HOST = "192.168.1.106"


def sensor_name_from_config(item: dict) -> str:
    return str(item.get("name") or item.get("id") or "").strip()


def normalize_room(room: dict) -> dict:
    return {
        "width_cm": max(50, int(room["width_cm"])),
        "height_cm": max(50, int(room["height_cm"])),
        "grid_step_cm": max(5, int(room.get("grid_step_cm", 10))),
        "smoothing": min(1.0, max(0.0, float(room.get("smoothing", 0.35)))),
    }


def normalize_sensor_config(item: dict, fallback_position: Optional[dict] = None) -> dict:
    sensor_name = sensor_name_from_config(item)
    if not sensor_name:
        raise ValueError("Sensor name cannot be empty")

    fallback_position = fallback_position or {"x_cm": 30, "y_cm": 30}
    label = str(item.get("label") or item.get("name") or sensor_name).strip() or sensor_name
    return {
        "id": sensor_name,
        "name": sensor_name,
        "label": label,
        "x_cm": int(item.get("x_cm", fallback_position["x_cm"])),
        "y_cm": int(item.get("y_cm", fallback_position["y_cm"])),
    }


def normalize_config(config: dict) -> dict:
    room = normalize_room(config["room"])
    server = dict(config.get("server", {"host": "0.0.0.0", "port": 8080}))
    sensors = []
    seen = set()
    raw_sensors = config.get("sensors", [])
    if isinstance(raw_sensors, dict):
        sensor_items = []
        for sensor_name, item in raw_sensors.items():
            sensor_item = dict(item or {})
            sensor_item.setdefault("name", sensor_name)
            sensor_item.setdefault("id", sensor_name)
            sensor_items.append(sensor_item)
    else:
        sensor_items = list(raw_sensors)

    for item in sensor_items:
        sensor = normalize_sensor_config(item)
        if sensor["id"] in seen:
            continue
        sensors.append(sensor)
        seen.add(sensor["id"])

    return {
        "room": room,
        "server": server,
        "sensors": sensors,
    }


def config_for_disk(config: dict) -> dict:
    normalized = normalize_config(config)
    sensors = {}
    for sensor in normalized["sensors"]:
        sensor_name = sensor["name"]
        sensors[sensor_name] = {
            "label": sensor["label"],
            "x_cm": sensor["x_cm"],
            "y_cm": sensor["y_cm"],
        }

    return {
        "room": normalized["room"],
        "server": normalized["server"],
        "sensors": sensors,
    }


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Missing config file: {CONFIG_PATH}\n"
            "Copy server/config.example.json to server/config.json first."
        )

    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return normalize_config(json.load(handle))


CONFIG = load_config()


def save_config(config: dict) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as handle:
        json.dump(config_for_disk(config), handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def append_calibration_sample(sample: dict) -> None:
    with CALIBRATION_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(sample, ensure_ascii=False, sort_keys=True))
        handle.write("\n")


def load_calibration_samples() -> List[dict]:
    if not CALIBRATION_PATH.exists():
        return []

    samples = []
    with CALIBRATION_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                samples.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return samples


def calibration_distance_map(sample: dict) -> Dict[str, float]:
    distances = {}
    for sensor in sample.get("state", {}).get("sensors", []):
        distance = sensor.get("selected_distance_cm")
        if distance is None:
            distance = max(sensor.get("moving_distance_cm") or 0, sensor.get("stationary_distance_cm") or 0)
        if distance:
            distances[str(sensor.get("id"))] = float(distance)
    return distances


def get_lan_ip() -> str:
    if PREFERRED_SERVER_HOST:
        return PREFERRED_SERVER_HOST

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def find_arduino_cli() -> Optional[Path]:
    for candidate in ARDUINO_CLI_CANDIDATES:
        if candidate.exists():
            return candidate

    resolved = shutil.which("arduino-cli")
    return Path(resolved) if resolved else None


def list_serial_ports() -> List[dict]:
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-CimInstance Win32_SerialPort | "
            "Select-Object DeviceID,Name,Description,PNPDeviceID | "
            "ConvertTo-Json -Depth 3"
        ),
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return []

    if result.returncode != 0 or not result.stdout.strip():
        return []

    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    if isinstance(parsed, dict):
        parsed = [parsed]

    ports = []
    for item in parsed:
        ports.append(
            {
                "port": item.get("DeviceID"),
                "name": item.get("Name"),
                "description": item.get("Description"),
                "pnp_device_id": item.get("PNPDeviceID"),
            }
        )
    return ports


def command_output(*parts: object) -> str:
    return "".join(str(part or "") for part in parts)


def provision_firmware(port: str, payload: dict) -> str:
    lines = [
        "PROVISION_BEGIN",
        f"sensor_id={str(payload.get('sensor_id', '')).strip()}",
        f"wifi_ssid={str(payload.get('wifi_ssid', '')).strip()}",
        f"wifi_password={str(payload.get('wifi_password', ''))}",
        f"server_host={str(payload.get('server_host', '')).strip() or get_lan_ip()}",
        f"server_port={int(payload.get('server_port', 8080) or 8080)}",
        f"post_interval_ms={int(payload.get('post_interval_ms', 300) or 300)}",
        "PROVISION_END",
    ]

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, newline="\n") as handle:
        provision_path = Path(handle.name)
        handle.write("\n".join(lines))
        handle.write("\n")

    script = r"""
$portName = $args[0]
$linesPath = $args[1]
$serial = New-Object System.IO.Ports.SerialPort $portName, 115200, ([System.IO.Ports.Parity]::None), 8, ([System.IO.Ports.StopBits]::One)
$serial.NewLine = "`n"
$serial.DtrEnable = $true
$serial.RtsEnable = $true
for ($attempt = 0; $attempt -lt 10; $attempt++) {
  try {
    $serial.Open()
    break
  } catch {
    Start-Sleep -Milliseconds 1000
  }
}
if (-not $serial.IsOpen) {
  throw "Could not open serial port $portName for provisioning"
}
Start-Sleep -Milliseconds 2500
Get-Content -LiteralPath $linesPath | ForEach-Object {
  $serial.WriteLine($_)
  Start-Sleep -Milliseconds 80
}
Start-Sleep -Milliseconds 500
$serial.Close()
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script, port, str(provision_path)],
            capture_output=True,
            text=True,
            timeout=25,
        )
        if result.returncode != 0:
            raise RuntimeError(command_output(result.stdout, result.stderr).strip())
        return command_output(result.stdout, result.stderr)
    finally:
        provision_path.unlink(missing_ok=True)


def run_firmware_job(job_id: str, payload: dict) -> None:
    cli = find_arduino_cli()
    if cli is None:
        update_firmware_job(job_id, status="failed", error="arduino-cli not found")
        return

    port = str(payload.get("port", "")).strip()
    sensor_id = str(payload.get("sensor_id", "")).strip()
    wifi_ssid = str(payload.get("wifi_ssid", "")).strip()
    wifi_password = str(payload.get("wifi_password", ""))
    server_host = str(payload.get("server_host", "")).strip() or get_lan_ip()
    fqbn = str(payload.get("fqbn", "esp32:esp32:esp32s3")).strip()

    if not port:
        update_firmware_job(job_id, status="failed", error="Missing COM port")
        return
    if not sensor_id:
        update_firmware_job(job_id, status="failed", error="Missing sensor id")
        return
    if not wifi_ssid:
        update_firmware_job(job_id, status="failed", error="Missing Wi-Fi SSID")
        return

    try:
        update_firmware_job(job_id, status="compiling")

        compile_cmd = [str(cli), "compile", "--fqbn", fqbn, str(FIRMWARE_SOURCE_PATH.parent)]
        compile_result = subprocess.run(compile_cmd, capture_output=True, text=True, timeout=300)
        if compile_result.returncode != 0:
            update_firmware_job(
                job_id,
                status="failed",
                error="Compile failed",
                output=command_output(compile_result.stdout, compile_result.stderr)[-8000:],
            )
            return

        update_firmware_job(job_id, status="uploading")
        upload_cmd = [str(cli), "upload", "-p", port, "--fqbn", fqbn, str(FIRMWARE_SOURCE_PATH.parent)]
        upload_result = subprocess.run(upload_cmd, capture_output=True, text=True, timeout=180)
        if upload_result.returncode != 0:
            update_firmware_job(
                job_id,
                status="failed",
                error="Upload failed",
                output=command_output(upload_result.stdout, upload_result.stderr)[-8000:],
            )
            return

        update_firmware_job(job_id, status="provisioning")
        provision_output = provision_firmware(port, payload)

        update_firmware_job(
            job_id,
            status="done",
            output=command_output(
                compile_result.stdout,
                compile_result.stderr,
                upload_result.stdout,
                upload_result.stderr,
                provision_output,
            )[-8000:],
        )
    except Exception as exc:
        update_firmware_job(job_id, status="failed", error=str(exc))


def update_firmware_job(job_id: str, **updates: object) -> None:
    with FIRMWARE_JOBS_LOCK:
        job = FIRMWARE_JOBS.setdefault(job_id, {})
        job.update(updates)
        job["updated_at_ms"] = int(time.time() * 1000)


SENSOR_STALE_MS = 3000
SENSOR_FILTER_TAU_SEC = 0.65
SENSOR_QUEUE_LIMIT = 8
POSITION_INTERVAL_SEC = 0.10
POSITION_MAX_SPEED_CM_SEC = 180.0


@dataclass
class SensorReading:
    present: bool
    distance_cm: int
    signal_strength: int
    remote_ip: Optional[str]
    device_timestamp_ms: int
    received_ms: int


class SensorNode:
    def __init__(self, sensor_id: str) -> None:
        self.sensor_id = sensor_id
        self.lock = threading.Lock()
        self.queue: queue.Queue[SensorReading] = queue.Queue(maxsize=SENSOR_QUEUE_LIMIT)
        self.stop_event = threading.Event()
        self.last_update_ms = 0
        self.device_timestamp_ms = 0
        self.present = False
        self.raw_distance_cm = 0
        self.signal_strength = 0
        self.filtered_distance_cm: Optional[float] = None
        self.base_weight = 0.0
        self.last_remote_ip: Optional[str] = None
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def submit(self, payload: dict) -> None:
        reading = SensorReading(
            present=bool(payload.get("present", False)),
            distance_cm=int(
                payload.get(
                    "distance_cm",
                    max(
                        int(payload.get("moving_distance_cm", 0) or 0),
                        int(payload.get("stationary_distance_cm", 0) or 0),
                    ),
                )
                or 0
            ),
            signal_strength=int(
                payload.get(
                    "signal_strength",
                    max(
                        int(payload.get("moving_energy", 0) or 0),
                        int(payload.get("stationary_energy", 0) or 0),
                    ),
                )
                or 0
            ),
            remote_ip=payload.get("_remote_ip"),
            device_timestamp_ms=int(payload.get("timestamp_ms", 0) or 0),
            received_ms=int(time.time() * 1000),
        )
        if reading.signal_strength <= 0 and reading.present and reading.distance_cm > 0:
            reading.signal_strength = 100

        while self.queue.full():
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break
        self.queue.put_nowait(reading)

    def stop(self) -> None:
        self.stop_event.set()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                reading = self.queue.get(timeout=0.2)
            except queue.Empty:
                self._expire_if_stale()
                continue
            self._apply_reading(reading)

    def _apply_reading(self, reading: SensorReading) -> None:
        with self.lock:
            previous_ms = self.last_update_ms or reading.received_ms
            dt_sec = max(0.001, (reading.received_ms - previous_ms) / 1000.0)
            self.last_update_ms = reading.received_ms
            self.device_timestamp_ms = reading.device_timestamp_ms
            self.last_remote_ip = reading.remote_ip
            self.raw_distance_cm = reading.distance_cm
            self.signal_strength = reading.signal_strength

            if not reading.present or reading.distance_cm <= 0:
                self.present = False
                self.filtered_distance_cm = None
                self.base_weight = 0.0
                return

            alpha = 1.0 - math.exp(-dt_sec / SENSOR_FILTER_TAU_SEC)
            if self.filtered_distance_cm is None:
                self.filtered_distance_cm = float(reading.distance_cm)
            else:
                self.filtered_distance_cm += alpha * (
                    float(reading.distance_cm) - self.filtered_distance_cm
                )

            self.present = True
            self.base_weight = clamp(reading.signal_strength / 100.0, 0.20, 1.0)

    def _expire_if_stale(self) -> None:
        now_ms = int(time.time() * 1000)
        with self.lock:
            if self.last_update_ms and now_ms - self.last_update_ms > SENSOR_STALE_MS:
                self.present = False
                self.filtered_distance_cm = None
                self.base_weight = 0.0

    def get_distance(self, now_ms: Optional[int] = None) -> Optional[dict]:
        now_ms = now_ms or int(time.time() * 1000)
        with self.lock:
            age_ms = now_ms - self.last_update_ms if self.last_update_ms else None
            if (
                age_ms is None
                or age_ms > SENSOR_STALE_MS
                or not self.present
                or self.filtered_distance_cm is None
            ):
                return None

            freshness = math.exp(-age_ms / 1200.0)
            weight = self.base_weight * freshness
            if weight <= 0.05:
                return None

            return {
                "id": self.sensor_id,
                "distance_cm": self.filtered_distance_cm,
                "weight": weight,
                "age_ms": age_ms,
            }

    def export_state(self, now_ms: int) -> dict:
        with self.lock:
            age_ms = now_ms - self.last_update_ms if self.last_update_ms else None
            freshness = math.exp(-age_ms / 1200.0) if age_ms is not None else 0.0
            selected_weight = self.base_weight * freshness if self.present else 0.0
            return {
                "present": self.present,
                "distance_cm": self.raw_distance_cm,
                "signal_strength": self.signal_strength,
                "moving": self.present,
                "stationary": False,
                "moving_distance_cm": self.raw_distance_cm,
                "stationary_distance_cm": 0,
                "moving_energy": self.signal_strength,
                "stationary_energy": 0,
                "selected_distance_cm": self.filtered_distance_cm,
                "selected_weight": selected_weight,
                "last_remote_ip": self.last_remote_ip,
                "last_update_ms": self.last_update_ms,
                "device_timestamp_ms": self.device_timestamp_ms,
                "age_ms": age_ms,
            }


def clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


class Tracker:
    def __init__(self, config: dict) -> None:
        self.lock = threading.Lock()
        self.room = {}
        self.sensor_positions = {}
        self.sensor_nodes: Dict[str, SensorNode] = {}
        self.last_estimate = None
        self.stop_event = threading.Event()
        self.apply_config(config)
        self.position_thread = threading.Thread(target=self._position_loop, daemon=True)
        self.position_thread.start()

    def apply_config(self, config: dict) -> None:
        self.room = dict(config["room"])
        self.sensor_positions = {
            sensor_name_from_config(item): normalize_sensor_config(item)
            for item in config.get("sensors", [])
            if sensor_name_from_config(item)
        }
        existing_nodes = self.sensor_nodes
        self.sensor_nodes = {}

        for sensor_id in self.sensor_positions.keys():
            existing_node = existing_nodes.pop(sensor_id, None)
            self.sensor_nodes[sensor_id] = existing_node or SensorNode(sensor_id)

        for node in existing_nodes.values():
            node.stop()

    def update_config(self, config: dict) -> None:
        with self.lock:
            self.apply_config(config)
            self.last_estimate = None

    def default_sensor_position(self) -> dict:
        width_cm = int(self.room.get("width_cm", 500))
        height_cm = int(self.room.get("height_cm", 400))
        margin = min(30, max(0, width_cm // 10), max(0, height_cm // 10))
        corners = [
            (margin, margin),
            (width_cm - margin, margin),
            (width_cm - margin, height_cm - margin),
            (margin, height_cm - margin),
        ]
        x_cm, y_cm = corners[len(self.sensor_positions) % len(corners)]
        return {"x_cm": int(x_cm), "y_cm": int(y_cm)}

    def ensure_sensor(self, sensor_id: str) -> bool:
        if sensor_id in self.sensor_nodes:
            return False

        position = self.default_sensor_position()
        self.sensor_positions[sensor_id] = {
            "id": sensor_id,
            "name": sensor_id,
            "label": sensor_id,
            "x_cm": position["x_cm"],
            "y_cm": position["y_cm"],
        }
        self.sensor_nodes[sensor_id] = SensorNode(sensor_id=sensor_id)
        self.last_estimate = None
        return True

    def export_sensor_config(self) -> List[dict]:
        with self.lock:
            return [dict(sensor) for sensor in self.sensor_positions.values()]

    def update_sensor(self, payload: dict) -> dict:
        sensor_id = str(payload.get("sensor_id", "")).strip()
        if not sensor_id:
            raise ValueError("Missing sensor_id")

        with self.lock:
            created = self.ensure_sensor(sensor_id)
            self.sensor_nodes[sensor_id].submit(payload)
            estimate = self.last_estimate
            return {
                "ok": True,
                "sensor_id": sensor_id,
                "created": created,
                "estimate": estimate,
            }

    def _position_loop(self) -> None:
        while not self.stop_event.is_set():
            self.recalculate_estimate()
            time.sleep(POSITION_INTERVAL_SEC)

    def active_measurements(self) -> tuple[List[dict], int, int]:
        now_ms = int(time.time() * 1000)
        with self.lock:
            width_cm = int(self.room["width_cm"])
            height_cm = int(self.room["height_cm"])
            positions = {sensor_id: dict(cfg) for sensor_id, cfg in self.sensor_positions.items()}
            nodes = dict(self.sensor_nodes)

        active = []
        for sensor_id, node in nodes.items():
            distance = node.get_distance(now_ms)
            if distance is None:
                continue
            cfg = positions.get(sensor_id)
            if cfg is None:
                continue
            active.append(
                {
                    "id": sensor_id,
                    "x_cm": float(cfg["x_cm"]),
                    "y_cm": float(cfg["y_cm"]),
                    "distance_cm": float(distance["distance_cm"]),
                    "weight": float(distance["weight"]),
                    "age_ms": distance["age_ms"],
                }
            )

        return active, width_cm, height_cm

    def estimate_from_two_circles(
        self,
        active: List[dict],
        width_cm: int,
        height_cm: int,
    ) -> Optional[dict]:
        first, second = active[0], active[1]
        x0, y0, r0 = first["x_cm"], first["y_cm"], first["distance_cm"]
        x1, y1, r1 = second["x_cm"], second["y_cm"], second["distance_cm"]
        dx = x1 - x0
        dy = y1 - y0
        center_distance = math.hypot(dx, dy)
        if center_distance <= 0:
            return None

        a = (r0 * r0 - r1 * r1 + center_distance * center_distance) / (2.0 * center_distance)
        h_squared = r0 * r0 - a * a
        base_x = x0 + a * dx / center_distance
        base_y = y0 + a * dy / center_distance

        if h_squared >= 0:
            h = math.sqrt(h_squared)
            rx = -dy / center_distance
            ry = dx / center_distance
            candidates = [
                (base_x + h * rx, base_y + h * ry),
                (base_x - h * rx, base_y - h * ry),
            ]
        else:
            candidates = [(base_x, base_y)]

        reference = (
            (self.last_estimate["x_cm"], self.last_estimate["y_cm"])
            if self.last_estimate is not None
            else (width_cm / 2.0, height_cm / 2.0)
        )
        point = min(candidates, key=lambda item: math.dist(item, reference))
        error = self.geometry_error(point, active)
        confidence = 1.0 / (1.0 + error / 250.0)
        return {
            "point": (clamp(point[0], 0.0, width_cm), clamp(point[1], 0.0, height_cm)),
            "confidence": confidence,
            "best_error": error,
            "method": "circle_intersection",
        }

    def estimate_from_multilateration(
        self,
        active: List[dict],
        width_cm: int,
        height_cm: int,
    ) -> Optional[dict]:
        reference = max(active, key=lambda sensor: sensor["weight"])
        ata00 = ata01 = ata11 = atb0 = atb1 = total_weight = 0.0
        x0 = reference["x_cm"]
        y0 = reference["y_cm"]
        r0 = reference["distance_cm"]

        for sensor in active:
            if sensor is reference:
                continue
            xi = sensor["x_cm"]
            yi = sensor["y_cm"]
            ri = sensor["distance_cm"]
            a0 = 2.0 * (xi - x0)
            a1 = 2.0 * (yi - y0)
            b = xi * xi + yi * yi - ri * ri - x0 * x0 - y0 * y0 + r0 * r0
            weight = max(0.05, min(reference["weight"], sensor["weight"]))
            ata00 += weight * a0 * a0
            ata01 += weight * a0 * a1
            ata11 += weight * a1 * a1
            atb0 += weight * a0 * b
            atb1 += weight * a1 * b
            total_weight += weight

        determinant = ata00 * ata11 - ata01 * ata01
        if abs(determinant) < 1e-6 or total_weight <= 0:
            return self.estimate_from_two_circles(active[:2], width_cm, height_cm)

        x_cm = (atb0 * ata11 - atb1 * ata01) / determinant
        y_cm = (ata00 * atb1 - ata01 * atb0) / determinant
        point = (clamp(x_cm, 0.0, width_cm), clamp(y_cm, 0.0, height_cm))
        error = self.geometry_error(point, active)
        confidence = 1.0 / (1.0 + error / 250.0)
        return {
            "point": point,
            "confidence": confidence,
            "best_error": error,
            "method": "weighted_multilateration",
        }

    def geometry_error(self, point: tuple[float, float], active: List[dict]) -> float:
        total_weight = 0.0
        total_error = 0.0
        for sensor in active:
            predicted = math.dist(point, (sensor["x_cm"], sensor["y_cm"]))
            total_error += sensor["weight"] * abs(predicted - sensor["distance_cm"])
            total_weight += sensor["weight"]
        return total_error / max(total_weight, 0.001)

    def estimate_from_calibration(
        self,
        active: List[dict],
        width_cm: int,
        height_cm: int,
    ) -> Optional[dict]:
        active_distances = {sensor["id"]: float(sensor["distance_cm"]) for sensor in active}
        weighted_targets = []
        closest_rms = None

        for sample in load_calibration_samples():
            target = sample.get("target", {})
            target_x = target.get("x_cm")
            target_y = target.get("y_cm")
            if target_x is None or target_y is None:
                continue

            sample_distances = calibration_distance_map(sample)
            common_ids = sorted(set(active_distances) & set(sample_distances))
            if len(common_ids) < 2:
                continue

            squared_error = 0.0
            for sensor_id in common_ids:
                delta = active_distances[sensor_id] - sample_distances[sensor_id]
                squared_error += delta * delta

            rms = math.sqrt(squared_error / len(common_ids))
            closest_rms = rms if closest_rms is None else min(closest_rms, rms)
            weight = 1.0 / (max(rms, 8.0) ** 2)
            weighted_targets.append(
                (
                    clamp(float(target_x), 0.0, width_cm),
                    clamp(float(target_y), 0.0, height_cm),
                    weight,
                )
            )

        if not weighted_targets:
            return None

        total_weight = sum(weight for _, _, weight in weighted_targets)
        if total_weight <= 0:
            return None

        x_cm = sum(x * weight for x, _, weight in weighted_targets) / total_weight
        y_cm = sum(y * weight for _, y, weight in weighted_targets) / total_weight
        confidence = 1.0 / (1.0 + (closest_rms or 0.0) / 120.0)

        return {
            "point": (clamp(x_cm, 0.0, width_cm), clamp(y_cm, 0.0, height_cm)),
            "confidence": confidence,
            "calibration_sample_count": len(weighted_targets),
            "calibration_closest_error_cm": round(closest_rms or 0.0, 2),
        }

    def recalculate_estimate(self) -> Optional[dict]:
        active, width_cm, height_cm = self.active_measurements()
        now_ms = int(time.time() * 1000)
        if len(active) < 2:
            with self.lock:
                self.last_estimate = None
            return None

        if len(active) == 2:
            estimate = self.estimate_from_two_circles(active, width_cm, height_cm)
        else:
            estimate = self.estimate_from_multilateration(active, width_cm, height_cm)

        if estimate is None:
            with self.lock:
                self.last_estimate = None
            return None

        best_point = estimate["point"]
        best_error = estimate["best_error"]
        confidence = estimate["confidence"]
        method = estimate["method"]

        calibration_estimate = self.estimate_from_calibration(active, width_cm, height_cm)
        calibration_sample_count = 0
        calibration_closest_error_cm = None
        uncalibrated_point = best_point
        if calibration_estimate is not None:
            best_point = calibration_estimate["point"]
            confidence = max(confidence, calibration_estimate["confidence"])
            calibration_sample_count = calibration_estimate["calibration_sample_count"]
            calibration_closest_error_cm = calibration_estimate["calibration_closest_error_cm"]
            method = f"{method}+calibration"

        with self.lock:
            previous = self.last_estimate
            smoothing = float(self.room.get("smoothing", 0.35))

        smoothed_x, smoothed_y = self.stabilize_position(
            best_point,
            previous,
            smoothing,
            now_ms,
            width_cm,
            height_cm,
        )

        next_estimate = {
            "x_cm": round(smoothed_x, 1),
            "y_cm": round(smoothed_y, 1),
            "raw_x_cm": round(best_point[0], 1),
            "raw_y_cm": round(best_point[1], 1),
            "uncalibrated_x_cm": round(uncalibrated_point[0], 1),
            "uncalibrated_y_cm": round(uncalibrated_point[1], 1),
            "confidence": round(confidence, 4),
            "active_sensor_count": len(active),
            "calibration_sample_count": calibration_sample_count,
            "calibration_closest_error_cm": calibration_closest_error_cm,
            "method": method,
            "best_error": round(best_error, 2),
            "updated_at_ms": now_ms,
        }

        with self.lock:
            self.last_estimate = next_estimate
        return next_estimate

    def stabilize_position(
        self,
        point: tuple[float, float],
        previous: Optional[dict],
        smoothing: float,
        now_ms: int,
        width_cm: int,
        height_cm: int,
    ) -> tuple[float, float]:
        if previous is None:
            return point

        dt_sec = max(0.001, (now_ms - int(previous.get("updated_at_ms", now_ms))) / 1000.0)
        target_x = previous["x_cm"] * (1.0 - smoothing) + point[0] * smoothing
        target_y = previous["y_cm"] * (1.0 - smoothing) + point[1] * smoothing
        dx = target_x - previous["x_cm"]
        dy = target_y - previous["y_cm"]
        distance = math.hypot(dx, dy)
        max_step = POSITION_MAX_SPEED_CM_SEC * dt_sec

        if distance > max_step > 0:
            scale = max_step / distance
            target_x = previous["x_cm"] + dx * scale
            target_y = previous["y_cm"] + dy * scale

        return (
            clamp(target_x, 0.0, width_cm),
            clamp(target_y, 0.0, height_cm),
        )

    def build_state(self) -> dict:
        with self.lock:
            estimate = self.last_estimate
            now_ms = int(time.time() * 1000)
            positions = {sensor_id: dict(cfg) for sensor_id, cfg in self.sensor_positions.items()}
            nodes = dict(self.sensor_nodes)
            sensors = []

            for sensor_id, cfg in positions.items():
                state = nodes[sensor_id].export_state(now_ms)
                sensors.append(
                    {
                        "id": sensor_id,
                        "label": cfg.get("label", sensor_id),
                        "x_cm": cfg["x_cm"],
                        "y_cm": cfg["y_cm"],
                        **state,
                    }
                )

            return {
                "room": self.room,
                "estimate": estimate,
                "sensors": sensors,
                "calibration": {
                    "sample_count": len(load_calibration_samples()),
                },
            }


TRACKER = Tracker(CONFIG)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "LD2420Tracker/0.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/state":
            self.send_json(TRACKER.build_state())
            return

        if parsed.path == "/api/ports":
            self.send_json(
                {
                    "ports": list_serial_ports(),
                    "server_host": get_lan_ip(),
                    "arduino_cli": str(find_arduino_cli() or ""),
                }
            )
            return

        if parsed.path.startswith("/api/firmware/"):
            job_id = parsed.path.replace("/api/firmware/", "", 1)
            with FIRMWARE_JOBS_LOCK:
                job = dict(FIRMWARE_JOBS.get(job_id, {}))
            if not job:
                self.send_json({"ok": False, "error": "Unknown job"}, status=HTTPStatus.NOT_FOUND)
                return
            self.send_json({"ok": True, "job": job})
            return

        if parsed.path == "/":
            self.serve_static("index.html")
            return

        if parsed.path.startswith("/static/"):
            local_name = parsed.path.replace("/static/", "", 1)
            self.serve_static(local_name)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            self.handle_config_update()
            return

        if parsed.path == "/api/calibration":
            self.handle_calibration_capture()
            return

        if parsed.path == "/api/auto-label":
            self.handle_auto_label()
            return

        if parsed.path == "/api/firmware":
            self.handle_firmware_upload()
            return

        if parsed.path != "/api/sensor":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length)

        try:
            payload = json.loads(raw.decode("utf-8"))
            payload["_remote_ip"] = self.client_address[0]
            result = TRACKER.update_sensor(payload)
            if result.get("created"):
                CONFIG["sensors"] = TRACKER.export_sensor_config()
                save_config(CONFIG)
            self.send_json(result, status=HTTPStatus.CREATED)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except json.JSONDecodeError:
            self.send_json(
                {"ok": False, "error": "Invalid JSON payload"},
                status=HTTPStatus.BAD_REQUEST,
            )

    def handle_calibration_capture(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length)

        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
            label = str(payload.get("label", "")).strip()
            target = payload.get("target", {})
            if not label:
                raise ValueError("Missing calibration label")

            sample = {
                "created_at_ms": int(time.time() * 1000),
                "label": label,
                "target": {
                    "x_cm": target.get("x_cm"),
                    "y_cm": target.get("y_cm"),
                },
                "state": TRACKER.build_state(),
            }
            append_calibration_sample(sample)
            self.send_json({"ok": True, "sample": sample})
        except (TypeError, ValueError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except json.JSONDecodeError:
            self.send_json({"ok": False, "error": "Invalid JSON payload"}, status=HTTPStatus.BAD_REQUEST)

    def handle_auto_label(self) -> None:
        global CONFIG

        with TRACKER.lock:
            width_cm = int(TRACKER.room.get("width_cm", 500))
            height_cm = int(TRACKER.room.get("height_cm", 400))
            positions = [
                ("top-left", 30, 30),
                ("top-right", width_cm - 30, 30),
                ("bottom-right", width_cm - 30, height_cm - 30),
                ("bottom-left", 30, height_cm - 30),
                ("center", width_cm // 2, height_cm // 2),
            ]
            sensors = []
            for index, sensor_id in enumerate(TRACKER.sensor_positions.keys()):
                label, x_cm, y_cm = positions[index % len(positions)]
                sensors.append(
                    {
                        "id": sensor_id,
                        "name": sensor_id,
                        "label": label,
                        "x_cm": x_cm,
                        "y_cm": y_cm,
                    }
                )

        CONFIG = {
            "room": CONFIG["room"],
            "server": CONFIG["server"],
            "sensors": sensors,
        }
        save_config(CONFIG)
        TRACKER.update_config(CONFIG)
        self.send_json({"ok": True, "config": CONFIG})

    def handle_firmware_upload(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length)

        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self.send_json({"ok": False, "error": "Invalid JSON payload"}, status=HTTPStatus.BAD_REQUEST)
            return

        job_id = uuid.uuid4().hex
        with FIRMWARE_JOBS_LOCK:
            FIRMWARE_JOBS[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at_ms": int(time.time() * 1000),
                "updated_at_ms": int(time.time() * 1000),
                "port": payload.get("port"),
                "sensor_id": payload.get("sensor_id"),
            }

        thread = threading.Thread(target=run_firmware_job, args=(job_id, payload), daemon=True)
        thread.start()
        self.send_json({"ok": True, "job_id": job_id, "job": FIRMWARE_JOBS[job_id]}, status=HTTPStatus.ACCEPTED)

    def handle_config_update(self) -> None:
        global CONFIG

        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length)

        try:
            payload = json.loads(raw.decode("utf-8"))
            CONFIG = normalize_config(
                {
                    "room": payload["room"],
                    "server": CONFIG["server"],
                    "sensors": payload["sensors"],
                }
            )
            save_config(CONFIG)
            TRACKER.update_config(CONFIG)
            self.send_json({"ok": True, "config": CONFIG})
        except (KeyError, TypeError, ValueError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except json.JSONDecodeError:
            self.send_json({"ok": False, "error": "Invalid JSON payload"}, status=HTTPStatus.BAD_REQUEST)

    def serve_static(self, relative_path: str) -> None:
        file_path = (PUBLIC_DIR / relative_path).resolve()
        if not str(file_path).startswith(str(PUBLIC_DIR.resolve())):
            self.send_error(HTTPStatus.FORBIDDEN, "Forbidden")
            return

        if not file_path.exists() or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return

        content_type = "text/plain; charset=utf-8"
        if file_path.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        elif file_path.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif file_path.suffix == ".css":
            content_type = "text/css; charset=utf-8"

        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    host = CONFIG["server"].get("host", "0.0.0.0")
    port = int(CONFIG["server"].get("port", 8080))
    httpd = ReusableThreadingHTTPServer((host, port), RequestHandler)
    print(f"Server started on http://{host}:{port}")
    print("Open a browser and check the dashboard.")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
