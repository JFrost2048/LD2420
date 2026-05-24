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


def clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def weighted_median(values: List[tuple[float, float]]) -> float:
    if not values:
        raise ValueError("weighted_median needs at least one value")

    ordered = sorted(values, key=lambda item: item[0])
    total_weight = sum(weight for _, weight in ordered)
    midpoint = total_weight / 2.0
    running = 0.0

    for value, weight in ordered:
        running += weight
        if running >= midpoint:
            return value

    return ordered[-1][0]


def robust_axis_value(values: List[tuple[float, float]], robust_radius_cm: float) -> tuple[float, float]:
    center = weighted_median(values)
    adjusted = []

    for value, weight in values:
        distance_from_center = abs(value - center)
        robust_weight = weight / (1.0 + distance_from_center / robust_radius_cm)
        adjusted.append((value, robust_weight))

    total_weight = sum(weight for _, weight in adjusted)
    if total_weight <= 0:
        return center, 0.0

    value = sum(value * weight for value, weight in adjusted) / total_weight
    disagreement = sum(abs(value - center) * weight for value, weight in adjusted) / total_weight
    confidence = 1.0 / (1.0 + disagreement / robust_radius_cm)
    return value, confidence


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

    def build_axis_measurements(self, active: List[dict], width_cm: int, height_cm: int) -> dict:
        measurements = {"x": [], "y": []}
        edge_margin = max(80.0, min(width_cm, height_cm) * 0.08)

        for sensor in active:
            x_cm = sensor["x_cm"]
            y_cm = sensor["y_cm"]
            distance_cm = sensor["distance_cm"]
            edge_distances = {
                "left": x_cm,
                "right": width_cm - x_cm,
                "top": y_cm,
                "bottom": height_cm - y_cm,
            }
            wall = min(edge_distances, key=edge_distances.get)
            wall_distance = edge_distances[wall]

            if wall_distance > edge_margin:
                continue

            reliability = sensor["weight"] * clamp(250.0 / max(distance_cm, 50.0), 0.25, 4.0)
            if wall == "left":
                measurements["x"].append((clamp(x_cm + distance_cm, 0.0, width_cm), reliability))
            elif wall == "right":
                measurements["x"].append((clamp(x_cm - distance_cm, 0.0, width_cm), reliability))
            elif wall == "top":
                measurements["y"].append((clamp(y_cm + distance_cm, 0.0, height_cm), reliability))
            elif wall == "bottom":
                measurements["y"].append((clamp(y_cm - distance_cm, 0.0, height_cm), reliability))

        return measurements

    def estimate_from_axis_measurements(
        self,
        active: List[dict],
        width_cm: int,
        height_cm: int,
    ) -> Optional[dict]:
        measurements = self.build_axis_measurements(active, width_cm, height_cm)
        robust_radius_cm = max(150.0, min(width_cm, height_cm) * 0.04)

        x_confidence = 0.0
        y_confidence = 0.0

        if measurements["x"]:
            x_cm, x_confidence = robust_axis_value(measurements["x"], robust_radius_cm)
        elif self.last_estimate is not None:
            x_cm = float(self.last_estimate["x_cm"])
        else:
            x_cm = width_cm / 2.0

        if measurements["y"]:
            y_cm, y_confidence = robust_axis_value(measurements["y"], robust_radius_cm)
        elif self.last_estimate is not None:
            y_cm = float(self.last_estimate["y_cm"])
        else:
            y_cm = height_cm / 2.0

        axis_count = int(bool(measurements["x"])) + int(bool(measurements["y"]))
        if axis_count == 0:
            return None

        coverage = 1.0 if axis_count == 2 else 0.45
        confidence = coverage * max(0.05, (x_confidence + y_confidence) / max(axis_count, 1))

        return {
            "point": (clamp(x_cm, 0.0, width_cm), clamp(y_cm, 0.0, height_cm)),
            "confidence": confidence,
            "best_error": 0.0,
            "method": "axis_projection",
            "axis_measurement_count": len(measurements["x"]) + len(measurements["y"]),
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

        axis_estimate = self.estimate_from_axis_measurements(active, width_cm, height_cm)
        if axis_estimate is not None:
            best_point = axis_estimate["point"]
            best_error = axis_estimate["best_error"]
            confidence = axis_estimate["confidence"]
            method = axis_estimate["method"]
            axis_measurement_count = axis_estimate["axis_measurement_count"]
        else:
            best_point = None
            best_error = float("inf")
            confidence = None
            method = "radial_search"
            axis_measurement_count = 0

        if best_point is None:
            best_point, best_error = self.estimate_from_radial_search(
                active,
                width_cm,
                height_cm,
                step_cm,
            )

        if best_point is None:
            self.last_estimate = None
            return None

        if confidence is None:
            confidence = 1.0 / (1.0 + (best_error / max(len(active), 1)))

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

        self.last_estimate = {
            "x_cm": round(smoothed_x, 1),
            "y_cm": round(smoothed_y, 1),
            "raw_x_cm": round(best_point[0], 1),
            "raw_y_cm": round(best_point[1], 1),
            "confidence": round(confidence, 4),
            "active_sensor_count": len(active),
            "axis_measurement_count": axis_measurement_count,
            "method": method,
            "best_error": round(best_error, 2),
            "updated_at_ms": int(time.time() * 1000),
        }
        return self.last_estimate

    def estimate_from_radial_search(
        self,
        active: List[dict],
        width_cm: int,
        height_cm: int,
        step_cm: int,
    ) -> tuple[Optional[tuple[float, float]], float]:
        best_point = None
        best_error = float("inf")

        def score(x_cm: float, y_cm: float) -> float:
            total_error = 0.0
            for sensor in active:
                predicted = math.dist((x_cm, y_cm), (sensor["x_cm"], sensor["y_cm"]))
                delta = predicted - sensor["distance_cm"]
                total_error += sensor["weight"] * (delta * delta)
            return total_error

        seed_points = [
            (width_cm / 2.0, height_cm / 2.0),
            *(
                (sensor["x_cm"], sensor["y_cm"])
                for sensor in active
            ),
        ]

        if self.last_estimate is not None:
            seed_points.append((self.last_estimate["x_cm"], self.last_estimate["y_cm"]))

        search_span = max(width_cm, height_cm) / 2.0

        for seed_x, seed_y in seed_points:
            x_cm = min(width_cm, max(0.0, float(seed_x)))
            y_cm = min(height_cm, max(0.0, float(seed_y)))
            candidate_error = score(x_cm, y_cm)
            current_step = max(float(step_cm), search_span / 8.0)

            while current_step >= step_cm:
                improved = True
                while improved:
                    improved = False
                    for dx in (-current_step, 0.0, current_step):
                        for dy in (-current_step, 0.0, current_step):
                            if dx == 0.0 and dy == 0.0:
                                continue
                            nx = min(width_cm, max(0.0, x_cm + dx))
                            ny = min(height_cm, max(0.0, y_cm + dy))
                            next_error = score(nx, ny)
                            if next_error < candidate_error:
                                x_cm = nx
                                y_cm = ny
                                candidate_error = next_error
                                improved = True

                current_step /= 2.0

            if candidate_error < best_error:
                best_error = candidate_error
                best_point = (round(x_cm / step_cm) * step_cm, round(y_cm / step_cm) * step_cm)

        if best_point is None:
            return None, best_error

        return best_point, best_error

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

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)

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
    ThreadingHTTPServer.daemon_threads = True
    httpd = ThreadingHTTPServer((host, port), RequestHandler)
    print(f"Server started on http://{host}:{port}")
    print("Open a browser and check the dashboard.")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
