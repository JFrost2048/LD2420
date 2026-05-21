import json
import math
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
CONFIG_PATH = BASE_DIR / "config.json"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Missing config file: {CONFIG_PATH}\n"
            "Copy server/config.example.json to server/config.json first."
        )

    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


CONFIG = load_config()


def save_config(config: dict) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


@dataclass
class SensorState:
    sensor_id: str
    last_update_ms: int = 0
    device_timestamp_ms: int = 0
    present: bool = False
    moving: bool = False
    stationary: bool = False
    moving_distance_cm: int = 0
    stationary_distance_cm: int = 0
    moving_energy: int = 0
    stationary_energy: int = 0
    selected_distance_cm: Optional[float] = None
    selected_weight: float = 0.0

    def update_from_payload(self, payload: dict) -> None:
        self.last_update_ms = int(time.time() * 1000)
        self.device_timestamp_ms = int(payload.get("timestamp_ms", 0) or 0)
        self.present = bool(payload.get("present", False))
        self.moving = bool(payload.get("moving", False))
        self.stationary = bool(payload.get("stationary", False))
        self.moving_distance_cm = int(payload.get("moving_distance_cm", 0) or 0)
        self.stationary_distance_cm = int(payload.get("stationary_distance_cm", 0) or 0)
        self.moving_energy = int(payload.get("moving_energy", 0) or 0)
        self.stationary_energy = int(payload.get("stationary_energy", 0) or 0)

        self.selected_distance_cm, self.selected_weight = choose_measurement(self)


def choose_measurement(sensor: SensorState) -> tuple[Optional[float], float]:
    candidates = []

    if sensor.stationary and sensor.stationary_distance_cm > 0:
        candidates.append(
            (
                float(sensor.stationary_distance_cm),
                max(0.20, sensor.stationary_energy / 100.0),
            )
        )

    if sensor.moving and sensor.moving_distance_cm > 0:
        candidates.append(
            (
                float(sensor.moving_distance_cm),
                max(0.20, sensor.moving_energy / 100.0),
            )
        )

    if not candidates and sensor.present:
        fallback_distance = max(sensor.moving_distance_cm, sensor.stationary_distance_cm)
        if fallback_distance > 0:
            fallback_energy = max(sensor.moving_energy, sensor.stationary_energy, 20)
            candidates.append((float(fallback_distance), fallback_energy / 100.0))

    if not candidates:
        return None, 0.0

    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates[0]


class Tracker:
    def __init__(self, config: dict) -> None:
        self.lock = threading.Lock()
        self.room = {}
        self.sensor_positions = {}
        self.sensor_states: Dict[str, SensorState] = {}
        self.last_estimate = None
        self.apply_config(config)

    def apply_config(self, config: dict) -> None:
        self.room = dict(config["room"])
        self.sensor_positions = {item["id"]: dict(item) for item in config["sensors"]}
        existing_states = self.sensor_states
        self.sensor_states = {}

        for sensor_id in self.sensor_positions.keys():
            self.sensor_states[sensor_id] = existing_states.get(
                sensor_id, SensorState(sensor_id=sensor_id)
            )

    def update_config(self, config: dict) -> None:
        with self.lock:
            self.apply_config(config)
            self.last_estimate = None

    def update_sensor(self, payload: dict) -> dict:
        sensor_id = payload.get("sensor_id")
        if sensor_id not in self.sensor_states:
            raise ValueError(f"Unknown sensor_id: {sensor_id}")

        with self.lock:
            self.sensor_states[sensor_id].update_from_payload(payload)
            estimate = self.recalculate_estimate()
            return {
                "ok": True,
                "sensor_id": sensor_id,
                "estimate": estimate,
            }

    def recalculate_estimate(self) -> Optional[dict]:
        active = []
        now_ms = int(time.time() * 1000)

        for sensor_id, state in self.sensor_states.items():
            age_ms = now_ms - state.last_update_ms
            if age_ms > 3000:
                continue
            if not state.present or state.selected_distance_cm is None:
                continue

            sensor_cfg = self.sensor_positions[sensor_id]
            active.append(
                {
                    "id": sensor_id,
                    "x_cm": float(sensor_cfg["x_cm"]),
                    "y_cm": float(sensor_cfg["y_cm"]),
                    "distance_cm": state.selected_distance_cm,
                    "weight": state.selected_weight,
                }
            )

        if len(active) < 2:
            self.last_estimate = None
            return None

        width_cm = int(self.room["width_cm"])
        height_cm = int(self.room["height_cm"])
        step_cm = max(5, int(self.room.get("grid_step_cm", 10)))

        best_point = None
        best_error = float("inf")

        for x_cm in range(0, width_cm + 1, step_cm):
            for y_cm in range(0, height_cm + 1, step_cm):
                total_error = 0.0

                for sensor in active:
                    predicted = math.dist((x_cm, y_cm), (sensor["x_cm"], sensor["y_cm"]))
                    delta = predicted - sensor["distance_cm"]
                    total_error += sensor["weight"] * (delta * delta)

                if total_error < best_error:
                    best_error = total_error
                    best_point = (float(x_cm), float(y_cm))

        if best_point is None:
            self.last_estimate = None
            return None

        smoothing = float(self.room.get("smoothing", 0.35))
        if self.last_estimate is None:
            smoothed_x, smoothed_y = best_point
        else:
            smoothed_x = (
                self.last_estimate["x_cm"] * (1.0 - smoothing) + best_point[0] * smoothing
            )
            smoothed_y = (
                self.last_estimate["y_cm"] * (1.0 - smoothing) + best_point[1] * smoothing
            )

        confidence = 1.0 / (1.0 + (best_error / max(len(active), 1)))
        self.last_estimate = {
            "x_cm": round(smoothed_x, 1),
            "y_cm": round(smoothed_y, 1),
            "raw_x_cm": round(best_point[0], 1),
            "raw_y_cm": round(best_point[1], 1),
            "confidence": round(confidence, 4),
            "active_sensor_count": len(active),
            "best_error": round(best_error, 2),
            "updated_at_ms": int(time.time() * 1000),
        }
        return self.last_estimate

    def build_state(self) -> dict:
        with self.lock:
            estimate = self.recalculate_estimate()
            now_ms = int(time.time() * 1000)
            sensors = []

            for sensor_id, cfg in self.sensor_positions.items():
                state = self.sensor_states[sensor_id]
                sensors.append(
                    {
                        "id": sensor_id,
                        "x_cm": cfg["x_cm"],
                        "y_cm": cfg["y_cm"],
                        "present": state.present,
                        "moving": state.moving,
                        "stationary": state.stationary,
                        "moving_distance_cm": state.moving_distance_cm,
                        "stationary_distance_cm": state.stationary_distance_cm,
                        "moving_energy": state.moving_energy,
                        "stationary_energy": state.stationary_energy,
                        "selected_distance_cm": state.selected_distance_cm,
                        "selected_weight": state.selected_weight,
                        "last_update_ms": state.last_update_ms,
                        "device_timestamp_ms": state.device_timestamp_ms,
                        "age_ms": now_ms - state.last_update_ms if state.last_update_ms else None,
                    }
                )

            return {
                "room": self.room,
                "estimate": estimate,
                "sensors": sensors,
            }


TRACKER = Tracker(CONFIG)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "LD2420Tracker/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/state":
            self.send_json(TRACKER.build_state())
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

        if parsed.path != "/api/sensor":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length)

        try:
            payload = json.loads(raw.decode("utf-8"))
            result = TRACKER.update_sensor(payload)
            self.send_json(result, status=HTTPStatus.CREATED)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except json.JSONDecodeError:
            self.send_json(
                {"ok": False, "error": "Invalid JSON payload"},
                status=HTTPStatus.BAD_REQUEST,
            )

    def handle_config_update(self) -> None:
        global CONFIG

        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length)

        try:
            payload = json.loads(raw.decode("utf-8"))
            room = payload["room"]
            sensors = payload["sensors"]

            normalized_room = {
                "width_cm": max(50, int(room["width_cm"])),
                "height_cm": max(50, int(room["height_cm"])),
                "grid_step_cm": max(5, int(room.get("grid_step_cm", 10))),
                "smoothing": min(1.0, max(0.0, float(room.get("smoothing", 0.35)))),
            }

            normalized_sensors = []
            known_ids = set(TRACKER.sensor_states.keys())
            for item in sensors:
                sensor_id = str(item["id"])
                if sensor_id not in known_ids:
                    raise ValueError(f"Unknown sensor_id: {sensor_id}")

                normalized_sensors.append(
                    {
                        "id": sensor_id,
                        "x_cm": int(item["x_cm"]),
                        "y_cm": int(item["y_cm"]),
                    }
                )

            if len(normalized_sensors) != len(known_ids):
                raise ValueError("Every sensor must be included in the config update")

            CONFIG = {
                "room": normalized_room,
                "server": CONFIG["server"],
                "sensors": normalized_sensors,
            }
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


def main() -> None:
    host = CONFIG["server"].get("host", "0.0.0.0")
    port = int(CONFIG["server"].get("port", 8080))
    httpd = ThreadingHTTPServer((host, port), RequestHandler)
    print(f"Server started on http://{host}:{port}")
    print("Open a browser and check the dashboard.")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
