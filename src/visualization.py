import json
import logging
import os
import sys
import threading
import time
import requests

DEFAULT_OSRM_BASE_URL = os.getenv("OSRM_BASE_URL", "http://localhost:5000")
FALLBACK_OSRM_BASE_URL = "https://router.project-osrm.org"

ROUTE_COLORS = [
    "#2563EB",  # Azul real
    "#EA580C",  # Naranja entrega
    "#16A34A",  # Verde esmeralda
    "#9333EA",  # Violeta
    "#E11D48",  # Rojo carmesí
    "#0891B2",  # Cian oscuro
    "#D97706",  # Ámbar
    "#4F46E5",  # Índigo
    "#059669",  # Verde botella
    "#DB2777",  # Rosa intenso
    "#7C3AED",  # Púrpura
    "#0284C7",  # Azul cielo
]


class _TokenBucketRateLimiter:
    """Thread-safe token bucket rate limiter."""

    def __init__(self, rate_per_second=50.0, burst=50, max_concurrent=8):
        self.rate = max(0.0, float(rate_per_second))
        self.capacity = max(1.0, float(burst))
        self.tokens = self.capacity
        self.updated_at = time.monotonic()
        self._cond = threading.Condition()
        self._semaphore = threading.Semaphore(max(1, int(max_concurrent)))

    def acquire(self):
        self._semaphore.acquire()
        with self._cond:
            while True:
                now = time.monotonic()
                elapsed = now - self.updated_at
                if elapsed > 0 and self.rate > 0:
                    self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                    self.updated_at = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True
                wait_time = 0.05 if self.rate <= 0 else (1.0 - self.tokens) / self.rate
                self._cond.wait(timeout=max(0.0, wait_time))

    def release(self):
        self._semaphore.release()
        with self._cond:
            self._cond.notify_all()


class OSRMClient:
    """OSRM client for fetching street-level route polylines from local OSRM or public fallback."""

    def __init__(self, base_url=None, profile="driving", max_workers=8, timeout=10, max_retries=1, auto_start_docker=False):
        resolved_url = base_url or DEFAULT_OSRM_BASE_URL
        self.base_url = resolved_url.rstrip("/")
        self.profile = profile
        self.max_workers = max(1, int(max_workers))
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.auto_start_docker = auto_start_docker
        self._limiter = _TokenBucketRateLimiter(rate_per_second=50.0, burst=50, max_concurrent=max_workers)
        self._session = requests.Session()
        self._auto_start_attempted = False

    def _ensure_local_osrm_running(self):
        if not self.auto_start_docker:
            return False
        if self._auto_start_attempted or ("localhost" not in self.base_url and "127.0.0.1" not in self.base_url):
            return False
        self._auto_start_attempted = True
        if not os.path.exists("setup_osrm.ps1"):
            return False
        logging.warning(
            f"OSRM local no responde en '{self.base_url}'. Iniciando contenedor OSRM con 'setup_osrm.ps1'..."
        )
        import subprocess

        res = subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", "setup_osrm.ps1"],
            check=False,
        )
        return res.returncode == 0

    def fetch_full_route(self, stops_coords):
        if not stops_coords or len(stops_coords) < 2:
            return stops_coords or []

        max_waypoints_per_call = 90
        if len(stops_coords) > max_waypoints_per_call:
            combined = []
            for start in range(0, len(stops_coords) - 1, max_waypoints_per_call - 1):
                chunk = stops_coords[start : start + max_waypoints_per_call]
                chunk_poly = self.fetch_full_route(chunk)
                if not combined:
                    combined.extend(chunk_poly)
                else:
                    combined.extend(chunk_poly[1:])
            return combined

        coords_str = ";".join(f"{c[1]:.6f},{c[0]:.6f}" for c in stops_coords)
        params = {"overview": "full", "geometries": "geojson", "continue_straight": "true"}

        urls_to_try = [f"{self.base_url}/route/v1/{self.profile}/{coords_str}"]
        if "localhost" in self.base_url or "127.0.0.1" in self.base_url:
            urls_to_try.append(f"{FALLBACK_OSRM_BASE_URL}/route/v1/{self.profile}/{coords_str}")

        for url in urls_to_try:
            for attempt in range(self.max_retries + 1):
                self._limiter.acquire()
                try:
                    resp = self._session.get(url, params=params, timeout=self.timeout)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("code") == "Ok" and data.get("routes"):
                            coords = data["routes"][0].get("geometry", {}).get("coordinates", [])
                            if coords:
                                return [[c[1], c[0]] for c in coords]
                    break
                except requests.RequestException:
                    if not self._auto_start_attempted and self.auto_start_docker:
                        self._limiter.release()
                        if self._ensure_local_osrm_running():
                            continue
                        self._limiter.acquire()
                finally:
                    try:
                        self._limiter.release()
                    except ValueError:
                        pass
                time.sleep(0.15 * (2 ** attempt))

        pairs = [(stops_coords[i], stops_coords[i + 1]) for i in range(len(stops_coords) - 1)]
        segments = self.fetch_many(pairs)
        route_coords = []
        for seg in segments:
            if not seg:
                continue
            if not route_coords:
                route_coords.extend(seg)
            else:
                route_coords.extend(seg[1:])
        return route_coords

    def _fetch_one(self, start_coords, end_coords):
        params = {"overview": "full", "geometries": "geojson"}
        urls = [f"{self.base_url}/route/v1/{self.profile}/{start_coords[1]},{start_coords[0]};{end_coords[1]},{end_coords[0]}"]
        if "localhost" in self.base_url or "127.0.0.1" in self.base_url:
            urls.append(f"{FALLBACK_OSRM_BASE_URL}/route/v1/{self.profile}/{start_coords[1]},{start_coords[0]};{end_coords[1]},{end_coords[0]}")

        for url in urls:
            self._limiter.acquire()
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("routes"):
                        coords = data["routes"][0].get("geometry", {}).get("coordinates", [])
                        if coords:
                            return [[c[1], c[0]] for c in coords]
            except requests.RequestException:
                pass
            finally:
                try:
                    self._limiter.release()
                except ValueError:
                    pass
        return [start_coords, end_coords]

    def fetch_many(self, pairs, label=None):
        from concurrent.futures import ThreadPoolExecutor, as_completed

        total = len(pairs)
        results = [None] * total
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_idx = {executor.submit(self._fetch_one, a, b): i for i, (a, b) in enumerate(pairs)}
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                results[idx] = future.result()
        return results


def build_map_payload(routes, data, osrm_base_url=None):
    """Construye el diccionario JSON con centro, depósito y rutas 3D listas para MapLibre GL JS."""
    ubicaciones = data.get("ubicaciones", [])
    if not ubicaciones:
        ubicaciones = [
            {
                "ID": 0,
                "ID_Viaje": 0,
                "Tipo_Servicio": "Depósito",
                "Nombre": "Depósito",
                "Latitud": 20.967073,
                "Longitud": -89.623739,
                "Horario_Inicio": "07:00",
                "Horario_Fin": "23:00",
                "Demanda": 0,
            }
        ]

    all_coords = [[float(u["Latitud"]), float(u["Longitud"])] for u in ubicaciones]
    center_lat = sum(c[0] for c in all_coords) / len(all_coords)
    center_lon = sum(c[1] for c in all_coords) / len(all_coords)

    depot_index = data.get("depot", 0)
    if depot_index >= len(ubicaciones):
        depot_index = 0
    depot_ubicacion = ubicaciones[depot_index]
    depot_info = {
        "name": str(depot_ubicacion.get("Nombre", "Depósito")),
        "lat": float(depot_ubicacion["Latitud"]),
        "lon": float(depot_ubicacion["Longitud"]),
    }

    osrm_client = OSRMClient(base_url=osrm_base_url, max_workers=8, timeout=10, max_retries=1)
    distance_matrix = data.get("distance_matrix", [])
    vehicle_cap = data.get("vehicle_capacity", 10)
    if isinstance(vehicle_cap, list):
        vehicle_cap = vehicle_cap[0] if vehicle_cap else 10

    routes_payload = []
    depot_return_required = data.get("depot_return_required", True)

    for route in routes or []:
        vehicle_id = route["vehicle_id"]
        display_id = vehicle_id + 1
        color = ROUTE_COLORS[vehicle_id % len(ROUTE_COLORS)]

        stops_coords = [stop["coordinates"] for stop in route["stops"]]
        route_latlon = osrm_client.fetch_full_route(stops_coords) if len(stops_coords) >= 2 else []
        geojson_coords = [[float(c[1]), float(c[0])] for c in route_latlon]

        total_time_str = f"{route['total_time'] // 60:02d}:{route['total_time'] % 60:02d}"

        route_dist_km = 0.0
        if distance_matrix:
            for k in range(len(route["stops"]) - 1):
                fn = route["stops"][k].get("node_index", 0)
                tn = route["stops"][k + 1].get("node_index", 0)
                try:
                    route_dist_km += float(distance_matrix[fn][tn])
                except (IndexError, TypeError, ValueError):
                    pass

        stops_payload = []
        for i, stop in enumerate(route["stops"]):
            if i == 0:
                continue
            if depot_return_required and i == len(route["stops"]) - 1:
                continue

            arr = int(stop["arrival_time"])
            arr_str = f"{arr // 60:02d}:{arr % 60:02d}"
            srv = int(stop.get("service_time", 0))
            dep = int(stop.get("departure_time", arr))
            dep_str = f"{dep // 60:02d}:{dep % 60:02d}"

            stops_payload.append(
                {
                    "sequence": i,
                    "node_index": stop.get("node_index", i),
                    "name": str(stop["name"]),
                    "lat": float(stop["coordinates"][0]),
                    "lon": float(stop["coordinates"][1]),
                    "arrival": arr_str,
                    "service_time": srv,
                    "departure": dep_str,
                    "demand": stop.get("demand", 0),
                    "vehicle_display_id": display_id,
                    "color": color,
                }
            )

        routes_payload.append(
            {
                "vehicle_id": vehicle_id,
                "display_id": display_id,
                "color": color,
                "total_time_str": total_time_str,
                "total_time_min": route["total_time"],
                "total_distance_km": round(route_dist_km, 2),
                "total_load": route["total_load"],
                "vehicle_capacity": vehicle_cap,
                "coordinates": geojson_coords,
                "stops": stops_payload,
            }
        )

    return {
        "center": [center_lon, center_lat],
        "depot": depot_info,
        "routes": routes_payload,
    }


def _normalize_initial_locations(data, initial_locations=None):
    if initial_locations:
        return initial_locations
    ubicaciones = data.get("ubicaciones", [])
    normalized = []
    for idx, u in enumerate(ubicaciones):
        raw_id = u.get("ID", idx)
        try:
            loc_id = int(raw_id)
        except (ValueError, TypeError):
            loc_id = idx
        raw_viaje = u.get("ID_Viaje", loc_id)
        try:
            id_viaje = int(raw_viaje)
        except (ValueError, TypeError):
            id_viaje = loc_id
        raw_tipo = u.get("Tipo_Servicio", "")
        if raw_tipo is None or str(raw_tipo).strip().lower() in ("nan", "", "none") or loc_id == 0:
            tipo = "Depósito" if (loc_id == 0 or idx == 0) else "Entrega"
        else:
            tipo = str(raw_tipo).strip()
        try:
            dem = 0 if (loc_id == 0 or idx == 0) else int(float(u.get("Demanda", 0) or 0))
        except (ValueError, TypeError):
            dem = 0
        normalized.append(
            {
                "ID": 0 if idx == 0 else loc_id,
                "ID_Viaje": 0 if idx == 0 else id_viaje,
                "Tipo_Servicio": tipo,
                "Nombre": str(u.get("Nombre", f"Punto {idx}")),
                "Latitud": float(u["Latitud"]),
                "Longitud": float(u["Longitud"]),
                "Horario_Inicio": str(u.get("Horario_Inicio", "08:00") or "08:00"),
                "Horario_Fin": str(u.get("Horario_Fin", "23:00") or "23:00"),
                "Demanda": dem,
            }
        )
    return normalized


def create_map_visualization(
    routes,
    data,
    output_file="rutas_twcvrp_mapa.html",
    osrm_base_url=None,
    initial_locations=None,
    initial_config=None,
    initial_report=None,
):
    """
    Crea el mapa interactivo en 3D (WebGL con MapLibre GL JS + OpenFreeMap Vector Tiles)
    con un Side Panel limpio e intuitivo para colocar/editar puntos, importar CSV,
    ajustar opciones de flota y visualizar rutas 3D y sus itinerarios.
    """
    map_payload = build_map_payload(routes or [], data or {}, osrm_base_url=osrm_base_url)
    locations_list = _normalize_initial_locations(data or {}, initial_locations)

    default_config = {
        "num_vehicles": (data or {}).get("num_vehicles", 5),
        "vehicle_capacity": (data or {}).get("vehicle_capacity", 10),
        "service_time": (data or {}).get("service_time", 10),
        "max_wait_time": (data or {}).get("max_wait_time", 60),
        "max_time_per_vehicle": (data or {}).get("max_time_per_vehicle", 90000),
        "depot_return_required": (data or {}).get("depot_return_required", True),
        "problem_type": (data or {}).get("problem_type", "AUTO"),
        "service": "osrm",
        "cost_per_km": (data or {}).get("cost_per_km", 3.24),
        "driver_cost_per_hour": (data or {}).get("driver_cost_per_hour", 81.25),
        "fixed_cost_per_vehicle": (data or {}).get("fixed_cost_per_vehicle", 34.38),
        "currency": (data or {}).get("currency", "MXN"),
    }
    if initial_config:
        default_config.update(initial_config)

    map_data_json = json.dumps(map_payload, ensure_ascii=False)
    locations_json = json.dumps(locations_list, ensure_ascii=False)
    config_json = json.dumps(default_config, ensure_ascii=False)
    report_json = json.dumps(initial_report, ensure_ascii=False)

    html_content = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Optimizador de Rutas VRP - Mapa 3D</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" />
  <link rel="stylesheet" href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" />
  <script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" />
  <style>
    :root {{
      --panel-bg: rgba(255, 255, 255, 0.96);
      --panel-border: rgba(15, 23, 42, 0.09);
      --panel-shadow: 0 10px 30px rgba(15, 23, 42, 0.14);
      --primary: #2563eb;
      --primary-hover: #1d4ed8;
      --primary-soft: #eff6ff;
      --text-main: #0f172a;
      --text-muted: #64748b;
      --border-light: #e2e8f0;
      --surface-muted: #f8fafc;
      --danger: #dc2626;
      --danger-soft: #fef2f2;
      --success: #16a34a;
      --success-soft: #f0fdf4;
      --warning: #d97706;
      --warning-soft: #fffbeb;
    }}

    * {{
      box-sizing: border-box;
    }}

    html, body {{
      margin: 0;
      padding: 0;
      width: 100%;
      height: 100%;
      overflow: hidden;
      font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      color: var(--text-main);
      -webkit-font-smoothing: antialiased;
    }}

    #map {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      background: #e2e8f0;
    }}

    #map.crosshair-mode canvas {{
      cursor: crosshair !important;
    }}

    /* Scrollbars limpias */
    ::-webkit-scrollbar {{
      width: 5px;
      height: 5px;
    }}
    ::-webkit-scrollbar-track {{
      background: transparent;
    }}
    ::-webkit-scrollbar-thumb {{
      background: #cbd5e1;
      border-radius: 999px;
    }}
    ::-webkit-scrollbar-thumb:hover {{
      background: #94a3b8;
    }}

    /* ================= SIDE PANEL IZQUIERDO ================= */
    .vrp-side-panel {{
      position: fixed;
      top: 14px;
      left: 14px;
      width: 375px;
      max-width: calc(100vw - 28px);
      max-height: calc(100vh - 28px);
      background: var(--panel-bg);
      border-radius: 14px;
      box-shadow: var(--panel-shadow);
      border: 1px solid var(--panel-border);
      z-index: 10001;
      display: flex;
      flex-direction: column;
      backdrop-filter: blur(10px);
      transition: transform 240ms cubic-bezier(0.22, 1, 0.36, 1), opacity 200ms ease;
    }}

    .vrp-side-panel.collapsed {{
      transform: translateX(calc(-100% - 24px));
      opacity: 0;
      pointer-events: none;
    }}

    .vrp-side-panel.drag-over {{
      border: 2px dashed var(--primary);
      background: rgba(239, 246, 255, 0.98);
    }}

    .vrp-side-toggle {{
      position: fixed;
      top: 14px;
      left: 14px;
      z-index: 10000;
      cursor: pointer;
      background: #ffffff;
      border: 1px solid var(--panel-border);
      color: var(--text-main);
      padding: 10px 14px;
      border-radius: 11px;
      box-shadow: var(--panel-shadow);
      font-size: 13px;
      font-weight: 600;
      display: none;
      align-items: center;
      gap: 8px;
      transition: all 150ms ease;
    }}
    .vrp-side-toggle:hover {{
      background: #f8fafc;
      transform: translateY(-1px);
    }}

    .vrp-panel-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 13px 16px;
      border-bottom: 1px solid var(--border-light);
    }}

    .vrp-panel-title {{
      margin: 0;
      font-size: 15px;
      font-weight: 700;
      color: var(--text-main);
      display: flex;
      align-items: center;
      gap: 8px;
    }}

    .vrp-title-icon {{
      width: 28px;
      height: 28px;
      border-radius: 8px;
      background: var(--primary-soft);
      color: var(--primary);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 13px;
    }}

    .vrp-icon-btn {{
      appearance: none;
      border: none;
      background: transparent;
      cursor: pointer;
      font-size: 14px;
      color: var(--text-muted);
      padding: 5px 8px;
      border-radius: 7px;
      transition: background 150ms ease, color 150ms ease;
    }}
    .vrp-icon-btn:hover {{
      background: #f1f5f9;
      color: var(--text-main);
    }}

    /* Pestañas */
    .vrp-tabs {{
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      padding: 6px 12px 0;
      gap: 4px;
      border-bottom: 1px solid var(--border-light);
      background: rgba(248, 250, 252, 0.7);
    }}

    .vrp-tab-btn {{
      appearance: none;
      border: none;
      background: transparent;
      padding: 9px 6px;
      font-size: 12px;
      font-weight: 600;
      color: var(--text-muted);
      cursor: pointer;
      border-bottom: 2px solid transparent;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 5px;
      transition: color 150ms ease, border-color 150ms ease;
    }}
    .vrp-tab-btn:hover {{
      color: var(--text-main);
    }}
    .vrp-tab-btn.active {{
      color: var(--primary);
      border-bottom-color: var(--primary);
    }}

    .vrp-badge-count {{
      background: #e2e8f0;
      color: #334155;
      font-size: 11px;
      padding: 1px 7px;
      border-radius: 999px;
      font-weight: 700;
    }}
    .vrp-tab-btn.active .vrp-badge-count {{
      background: #dbeafe;
      color: var(--primary);
    }}

    .vrp-panel-body {{
      padding: 12px 14px;
      overflow-y: auto;
      flex: 1;
      font-size: 13px;
    }}

    .vrp-tab-pane {{
      display: none;
    }}
    .vrp-tab-pane.active {{
      display: block;
    }}

    /* Barra de herramientas CSV */
    .vrp-toolbar {{
      display: grid;
      grid-template-columns: 1fr 1fr auto;
      gap: 6px;
      margin-bottom: 10px;
    }}

    .vrp-btn {{
      appearance: none;
      border: 1px solid var(--border-light);
      background: #ffffff;
      color: var(--text-main);
      padding: 7px 10px;
      border-radius: 8px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      transition: all 150ms ease;
    }}
    .vrp-btn:hover {{
      background: #f8fafc;
      border-color: #cbd5e1;
    }}

    /* Botón modo clic en el mapa */
    .vrp-map-mode-box {{
      background: var(--surface-muted);
      border: 1px solid var(--border-light);
      border-radius: 10px;
      padding: 8px;
      margin-bottom: 10px;
      transition: all 150ms ease;
    }}
    .vrp-map-mode-box.active {{
      background: var(--success-soft);
      border-color: #86efac;
    }}

    .vrp-btn-map-mode {{
      width: 100%;
      padding: 8px 12px;
      background: #ffffff;
      border: 1px solid #cbd5e1;
      color: var(--text-main);
    }}
    .vrp-btn-map-mode:hover {{
      background: #f1f5f9;
    }}
    .vrp-btn-map-mode.active {{
      background: var(--success);
      border-color: #15803d;
      color: #ffffff;
      box-shadow: 0 4px 12px rgba(22, 163, 74, 0.22);
    }}

    .vrp-quick-defaults {{
      display: none;
      grid-template-columns: 1.1fr 0.8fr 1fr;
      gap: 6px;
      margin-top: 8px;
      padding-top: 8px;
      border-top: 1px dashed #bbf7d0;
    }}
    .vrp-map-mode-box.active .vrp-quick-defaults {{
      display: grid;
    }}

    /* Acordeón de formulario manual / edición */
    .vrp-form-toggle-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
    }}

    .vrp-form-card {{
      background: var(--surface-muted);
      border: 1px solid var(--border-light);
      border-radius: 10px;
      padding: 10px;
      margin-bottom: 10px;
      display: none;
    }}
    .vrp-form-card.open {{
      display: block;
    }}

    .vrp-form-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      font-size: 12px;
      font-weight: 700;
      color: #334155;
    }}

    .vrp-grid-2 {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-bottom: 8px;
    }}

    .vrp-grid-3 {{
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 6px;
      margin-bottom: 8px;
    }}

    .vrp-field {{
      display: flex;
      flex-direction: column;
      gap: 3px;
    }}

    .vrp-field label {{
      font-size: 11px;
      font-weight: 600;
      color: var(--text-muted);
    }}

    .vrp-input, .vrp-select {{
      width: 100%;
      padding: 6px 8px;
      border: 1px solid #cbd5e1;
      border-radius: 7px;
      font-size: 12px;
      font-family: inherit;
      color: var(--text-main);
      background: #ffffff;
      outline: none;
      transition: border-color 150ms ease, box-shadow 150ms ease;
    }}
    .vrp-input:focus, .vrp-select:focus {{
      border-color: var(--primary);
      box-shadow: 0 0 0 2.5px rgba(37, 99, 235, 0.14);
    }}

    /* Aviso de cambios pendientes */
    .vrp-dirty-banner {{
      display: none;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      background: var(--warning-soft);
      border: 1px solid #fde68a;
      color: #92400e;
      padding: 6px 10px;
      border-radius: 8px;
      font-size: 11.5px;
      font-weight: 600;
      margin-bottom: 8px;
    }}
    .vrp-dirty-banner.visible {{
      display: flex;
    }}

    /* Lista de puntos */
    .vrp-points-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin: 4px 0 6px;
    }}

    .vrp-points-list {{
      display: flex;
      flex-direction: column;
      gap: 6px;
      max-height: calc(100vh - 360px);
      min-height: 140px;
      overflow-y: auto;
      padding-right: 2px;
    }}

    .vrp-point-item {{
      display: flex;
      align-items: center;
      gap: 9px;
      padding: 8px 10px;
      background: #ffffff;
      border: 1px solid var(--border-light);
      border-radius: 9px;
      cursor: pointer;
      transition: all 150ms ease;
    }}
    .vrp-point-item:hover {{
      border-color: #93c5fd;
      background: #f8fafc;
    }}
    .vrp-point-item.selected {{
      border-color: var(--primary);
      background: var(--primary-soft);
    }}

    .vrp-point-badge {{
      width: 24px;
      height: 24px;
      border-radius: 50%;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 11px;
      font-weight: 700;
      flex-shrink: 0;
    }}
    .vrp-point-badge.depot {{
      background: #0f172a;
      color: #ffffff;
    }}
    .vrp-point-badge.entrega {{
      background: #fee2e2;
      color: #b91c1c;
      border: 1px solid #fca5a5;
    }}
    .vrp-point-badge.recogida {{
      background: #dbeafe;
      color: #1d4ed8;
      border: 1px solid #93c5fd;
    }}

    .vrp-point-info {{
      flex: 1;
      min-width: 0;
    }}
    .vrp-point-name {{
      font-size: 12.5px;
      font-weight: 600;
      color: var(--text-main);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .vrp-point-meta {{
      font-size: 11px;
      color: var(--text-muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      margin-top: 1px;
    }}

    .vrp-point-actions {{
      display: flex;
      gap: 2px;
    }}

    /* Indicador capacidad vs demanda */
    .vrp-capacity-bar {{
      background: var(--surface-muted);
      border: 1px solid var(--border-light);
      border-radius: 9px;
      padding: 9px 11px;
      margin-top: 10px;
      font-size: 12px;
    }}
    .vrp-capacity-bar.ok {{
      border-color: #bbf7d0;
      background: var(--success-soft);
      color: #166534;
    }}
    .vrp-capacity-bar.warn {{
      border-color: #fecaca;
      background: var(--danger-soft);
      color: #991b1b;
    }}
    .vrp-cap-progress {{
      height: 5px;
      border-radius: 999px;
      background: rgba(0, 0, 0, 0.08);
      margin-top: 6px;
      overflow: hidden;
    }}
    .vrp-cap-fill {{
      height: 100%;
      background: var(--success);
      border-radius: 999px;
      transition: width 200ms ease;
    }}
    .vrp-capacity-bar.warn .vrp-cap-fill {{
      background: var(--danger);
    }}

    /* Footer fijo del side panel */
    .vrp-panel-footer {{
      padding: 10px 14px 12px;
      border-top: 1px solid var(--border-light);
      background: rgba(255, 255, 255, 0.96);
      border-radius: 0 0 14px 14px;
    }}

    .vrp-btn-solve {{
      width: 100%;
      padding: 10px 14px;
      background: var(--primary);
      color: #ffffff;
      border: none;
      border-radius: 9px;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      box-shadow: 0 4px 14px rgba(37, 99, 235, 0.28);
      transition: background 150ms ease, transform 100ms ease;
    }}
    .vrp-btn-solve:hover:not(:disabled) {{
      background: var(--primary-hover);
    }}
    .vrp-btn-solve:disabled {{
      opacity: 0.65;
      cursor: wait;
    }}

    .vrp-status-msg {{
      margin-top: 6px;
      font-size: 11px;
      text-align: center;
      min-height: 15px;
      color: var(--text-muted);
    }}
    .vrp-status-msg.error {{
      color: var(--danger);
      font-weight: 600;
    }}
    .vrp-status-msg.success {{
      color: var(--success);
      font-weight: 600;
    }}

    /* ================= BARRA DE BÚSQUEDA CENTRADA ================= */
    .twcvrp-search-bar {{
      position: fixed;
      top: 14px;
      left: 50%;
      transform: translateX(-50%);
      z-index: 10000;
      width: 340px;
      max-width: 75vw;
      background: var(--panel-bg);
      border-radius: 12px;
      box-shadow: var(--panel-shadow);
      border: 1px solid var(--panel-border);
      padding: 7px 13px;
      display: flex;
      align-items: center;
      gap: 8px;
      backdrop-filter: blur(8px);
    }}
    .twcvrp-search-bar i {{
      color: var(--text-muted);
      font-size: 13px;
    }}
    .twcvrp-search-input {{
      border: none;
      outline: none;
      background: transparent;
      width: 100%;
      font-size: 13px;
      font-family: inherit;
      color: var(--text-main);
      padding: 2px 0;
    }}
    .twcvrp-search-results {{
      position: absolute;
      top: calc(100% + 6px);
      left: 0;
      right: 0;
      background: #fff;
      border-radius: 10px;
      box-shadow: 0 10px 26px rgba(15, 23, 42, 0.16);
      border: 1px solid var(--panel-border);
      max-height: 240px;
      overflow-y: auto;
      display: none;
    }}
    .twcvrp-search-item {{
      padding: 8px 12px;
      font-size: 12px;
      color: var(--text-main);
      cursor: pointer;
      border-bottom: 1px solid #f1f5f9;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .twcvrp-search-item:last-child {{ border-bottom: none; }}
    .twcvrp-search-item:hover {{ background: #f8fafc; }}

    /* Banner inferior cuando está activo el modo de clic en el mapa */
    .vrp-map-banner {{
      position: fixed;
      bottom: 22px;
      left: 50%;
      transform: translateX(-50%) translateY(80px);
      z-index: 10000;
      background: rgba(15, 23, 42, 0.92);
      color: #ffffff;
      padding: 9px 16px;
      border-radius: 999px;
      font-size: 12.5px;
      font-weight: 500;
      display: flex;
      align-items: center;
      gap: 10px;
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.25);
      backdrop-filter: blur(6px);
      opacity: 0;
      pointer-events: none;
      transition: transform 220ms cubic-bezier(0.22, 1, 0.36, 1), opacity 180ms ease;
    }}
    .vrp-map-banner.visible {{
      transform: translateX(-50%) translateY(0);
      opacity: 1;
      pointer-events: auto;
    }}
    .vrp-map-banner button {{
      appearance: none;
      border: 1px solid rgba(255, 255, 255, 0.25);
      background: rgba(255, 255, 255, 0.12);
      color: #ffffff;
      border-radius: 999px;
      padding: 3px 10px;
      font-size: 11px;
      font-weight: 600;
      cursor: pointer;
    }}
    .vrp-map-banner button:hover {{
      background: rgba(255, 255, 255, 0.25);
    }}

    /* Toast flotante */
    .vrp-toast {{
      position: fixed;
      top: 64px;
      left: 50%;
      transform: translateX(-50%) translateY(-15px);
      z-index: 10005;
      background: #0f172a;
      color: #ffffff;
      padding: 8px 14px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      box-shadow: 0 8px 20px rgba(15, 23, 42, 0.22);
      opacity: 0;
      pointer-events: none;
      transition: all 200ms ease;
      display: flex;
      align-items: center;
      gap: 7px;
    }}
    .vrp-toast.show {{
      opacity: 1;
      transform: translateX(-50%) translateY(0);
    }}
    .vrp-toast.error {{
      background: #dc2626;
    }}
    .vrp-toast.success {{
      background: #15803d;
    }}

    /* ================= PANEL DERECHO DE LEYENDA Y RESULTADOS ================= */
    .twcvrp-legend-panel {{
      position: fixed;
      top: 14px;
      right: -345px;
      width: 315px;
      max-height: calc(100vh - 28px);
      overflow-y: auto;
      background: var(--panel-bg);
      border-radius: 14px;
      box-shadow: var(--panel-shadow);
      border: 1px solid var(--panel-border);
      z-index: 9999;
      transition: right 240ms cubic-bezier(0.22, 1, 0.36, 1);
      backdrop-filter: blur(10px);
    }}
    .twcvrp-legend-panel.open {{ right: 14px; }}
    .twcvrp-legend-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 13px 15px;
      border-bottom: 1px solid var(--border-light);
    }}
    .twcvrp-legend-title {{ margin: 0; font-size: 14px; font-weight: 700; color: var(--text-main); }}
    .twcvrp-legend-content {{ padding: 12px 14px; font-size: 13px; color: var(--text-main); }}
    .twcvrp-divider {{ margin: 10px 0; border: 0; border-top: 1px solid var(--border-light); }}
    .twcvrp-section-title {{
      margin: 0 0 8px 0;
      font-size: 11px;
      font-weight: 700;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}

    /* Controles de cámara y estilo en el panel derecho */
    .vrp-cam-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
      margin-bottom: 8px;
    }}

    /* Tarjetas de ruta por vehículo con acordeón de paradas */
    .vrp-route-card {{
      border: 1px solid var(--border-light);
      border-radius: 9px;
      background: #ffffff;
      margin-bottom: 7px;
      overflow: hidden;
      transition: border-color 150ms ease;
    }}
    .vrp-route-card:hover {{
      border-color: #cbd5e1;
    }}
    .vrp-route-header {{
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 10px;
      cursor: pointer;
      font-size: 12.5px;
      font-weight: 600;
    }}
    .twcvrp-dot {{
      width: 12px;
      height: 12px;
      border-radius: 50%;
      display: inline-block;
      box-shadow: inset 0 0 0 2px #fff, 0 0 0 1px rgba(0,0,0,0.18);
      flex-shrink: 0;
    }}
    .twcvrp-meta {{
      margin-left: auto;
      color: var(--text-muted);
      font-size: 11px;
      font-weight: 500;
    }}
    .vrp-route-stops-drawer {{
      display: none;
      border-top: 1px solid #f1f5f9;
      background: #f8fafc;
      padding: 7px 10px;
      font-size: 11.5px;
    }}
    .vrp-route-card.expanded .vrp-route-stops-drawer {{
      display: block;
    }}
    .vrp-stop-step {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 4px 0;
      border-bottom: 1px dashed #e2e8f0;
      cursor: pointer;
    }}
    .vrp-stop-step:last-child {{
      border-bottom: none;
    }}
    .vrp-stop-step:hover {{
      color: var(--primary);
    }}

    .twcvrp-legend-toggle {{
      position: fixed;
      top: 14px;
      right: 14px;
      z-index: 10000;
      cursor: pointer;
      background: #ffffff;
      border: 1px solid var(--panel-border);
      color: var(--text-main);
      padding: 10px 14px;
      border-radius: 11px;
      box-shadow: var(--panel-shadow);
      font-size: 13px;
      font-weight: 600;
      display: none;
      align-items: center;
      gap: 7px;
    }}

    /* Resumen de métricas en leyenda */
    .vrp-kpi-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
      margin-bottom: 10px;
    }}
    .vrp-kpi-box {{
      background: var(--surface-muted);
      border: 1px solid var(--border-light);
      border-radius: 9px;
      padding: 7px 9px;
    }}
    .vrp-kpi-label {{
      font-size: 10.5px;
      color: var(--text-muted);
      font-weight: 600;
    }}
    .vrp-kpi-val {{
      font-size: 13.5px;
      font-weight: 700;
      color: var(--text-main);
      margin-top: 1px;
    }}

    /* Marcadores unificados y arrastrables en el mapa */
    .vrp-map-marker {{
      width: 26px;
      height: 26px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 11.5px;
      font-weight: 700;
      cursor: grab;
      box-shadow: 0 3px 10px rgba(15, 23, 42, 0.32);
      border: 2.5px solid #ffffff;
      user-select: none;
      transition: transform 120ms ease, box-shadow 120ms ease;
    }}
    .vrp-map-marker:hover {{
      transform: scale(1.16);
      z-index: 50 !important;
    }}
    .vrp-map-marker:active {{
      cursor: grabbing;
    }}
    .vrp-map-marker.depot {{
      width: 32px;
      height: 32px;
      background: #0f172a;
      color: #ffffff;
      font-size: 13px;
      border-width: 3px;
    }}
    .vrp-map-marker.entrega {{
      background: #ef4444;
      color: #ffffff;
    }}
    .vrp-map-marker.recogida {{
      background: #2563eb;
      color: #ffffff;
    }}

    .maplibregl-popup-content {{
      border-radius: 11px;
      padding: 12px 14px;
      box-shadow: 0 10px 25px rgba(15, 23, 42, 0.18);
      border: 1px solid rgba(15, 23, 42, 0.08);
      font-family: "Inter", sans-serif;
      font-size: 12px;
      line-height: 1.45;
      min-width: 195px;
    }}
    .vrp-popup-actions {{
      display: flex;
      gap: 6px;
      margin-top: 8px;
      padding-top: 8px;
      border-top: 1px solid #f1f5f9;
    }}
    .vrp-popup-btn {{
      flex: 1;
      padding: 4px 8px;
      border-radius: 6px;
      border: 1px solid var(--border-light);
      background: #f8fafc;
      font-size: 11px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 4px;
    }}
    .vrp-popup-btn:hover {{
      background: #e2e8f0;
    }}
    .vrp-popup-btn.danger {{
      color: var(--danger);
      border-color: #fecaca;
      background: #fef2f2;
    }}

    @media (max-width: 960px) {{
      .twcvrp-search-bar {{
        display: none;
      }}
    }}
  </style>
</head>
<body>
  <div id="map"></div>

  <!-- Toast de notificación -->
  <div id="vrp-toast" class="vrp-toast"></div>

  <!-- Banner inferior de modo clic en el mapa -->
  <div id="vrp-map-banner" class="vrp-map-banner">
    <i class="fa fa-crosshairs" style="color:#4ade80;"></i>
    <span>Haz clic en cualquier punto del mapa para agregar una ubicación</span>
    <button id="btn-banner-exit">Salir (Esc)</button>
  </div>

  <!-- Botón para abrir Side Panel cuando está colapsado -->
  <button id="vrp-side-toggle" class="vrp-side-toggle" title="Abrir panel de planificación">
    <i class="fa fa-sliders" style="color:var(--primary)"></i> Planificador VRP
  </button>

  <!-- ================= SIDE PANEL IZQUIERDO ================= -->
  <aside id="vrp-side-panel" class="vrp-side-panel">
    <div class="vrp-panel-header">
      <h3 class="vrp-panel-title">
        <span class="vrp-title-icon"><i class="fa fa-route"></i></span>
        Planificador VRP
      </h3>
      <button id="vrp-side-close" class="vrp-icon-btn" title="Ocultar panel lateral">
        <i class="fa fa-chevron-left"></i>
      </button>
    </div>

    <div class="vrp-tabs">
      <button class="vrp-tab-btn active" data-tab="tab-points">
        <i class="fa fa-location-dot"></i> Puntos
        <span id="vrp-points-count" class="vrp-badge-count">0</span>
      </button>
      <button class="vrp-tab-btn" data-tab="tab-config">
        <i class="fa fa-sliders"></i> Opciones
      </button>
      <button class="vrp-tab-btn" data-tab="tab-costs">
        <i class="fa fa-coins"></i> Costos
      </button>
    </div>

    <div class="vrp-panel-body">
      <!-- PESTAÑA 1: PUNTOS Y CSV -->
      <div id="tab-points" class="vrp-tab-pane active">
        <div class="vrp-toolbar">
          <button id="btn-import-csv" class="vrp-btn" title="Importar archivo CSV (o arrástralo aquí)">
            <i class="fa fa-file-arrow-up" style="color:var(--primary)"></i> Importar CSV
          </button>
          <button id="btn-export-csv" class="vrp-btn" title="Descargar puntos actuales en CSV">
            <i class="fa fa-file-arrow-down"></i> Exportar CSV
          </button>
          <button id="btn-load-demo" class="vrp-btn" title="Cargar ejemplo de varias paradas en Mérida">
            <i class="fa fa-wand-magic-sparkles" style="color:var(--warning)"></i> Demo
          </button>
          <input id="input-csv-file" type="file" accept=".csv,text/csv" style="display:none" />
        </div>

        <!-- Caja de modo clic en el mapa -->
        <div id="map-mode-box" class="vrp-map-mode-box">
          <button id="btn-map-click-mode" class="vrp-btn vrp-btn-map-mode">
            <i class="fa fa-crosshairs"></i>
            <span id="map-click-mode-label">Poner puntos haciendo clic en el mapa</span>
          </button>
          <div class="vrp-quick-defaults">
            <div class="vrp-field">
              <label for="quick-type">Rol al hacer clic</label>
              <select id="quick-type" class="vrp-select">
                <option value="Entrega">Entrega</option>
                <option value="Recogida">Recogida</option>
                <option value="Depósito">Mover Depósito</option>
              </select>
            </div>
            <div class="vrp-field">
              <label for="quick-demand">Demanda</label>
              <input id="quick-demand" type="number" min="0" class="vrp-input" value="3" />
            </div>
            <div class="vrp-field">
              <label for="quick-window">Horario</label>
              <select id="quick-window" class="vrp-select">
                <option value="08:00-23:00">08:00 - 23:00</option>
                <option value="08:00-14:00">08:00 - 14:00</option>
                <option value="14:00-20:00">14:00 - 20:00</option>
                <option value="07:00-23:00">07:00 - 23:00</option>
              </select>
            </div>
          </div>
        </div>

        <!-- Botón para abrir formulario manual -->
        <div class="vrp-form-toggle-row">
          <span class="twcvrp-section-title" style="margin:0;">Ubicaciones (<span id="lbl-stops-summary">0 paradas</span>)</span>
          <div style="display:flex;gap:4px;">
            <button id="btn-toggle-manual-form" class="vrp-btn" style="padding:4px 8px;font-size:11px;">
              <i class="fa fa-plus"></i> Manual
            </button>
            <button id="btn-clear-points" class="vrp-btn" style="padding:4px 8px;font-size:11px;color:var(--danger);" title="Eliminar todas las paradas excepto el depósito">
              <i class="fa fa-trash-can"></i>
            </button>
          </div>
        </div>

        <!-- Formulario colapsable para agregar o editar un punto -->
        <div id="vrp-point-form-card" class="vrp-form-card">
          <div class="vrp-form-header">
            <span id="form-point-title">Agregar Punto Manual</span>
            <button id="btn-cancel-edit" class="vrp-icon-btn" style="font-size:11px;padding:2px 6px;" title="Cerrar formulario">
              ✕ Cerrar
            </button>
          </div>
          <input type="hidden" id="point-edit-index" value="-1" />

          <div class="vrp-grid-2">
            <div class="vrp-field">
              <label for="point-type">Tipo</label>
              <select id="point-type" class="vrp-select">
                <option value="Entrega">Entrega</option>
                <option value="Recogida">Recogida</option>
                <option value="Depósito">Depósito (Origen)</option>
              </select>
            </div>
            <div class="vrp-field">
              <label for="point-name">Nombre</label>
              <input id="point-name" type="text" class="vrp-input" placeholder="Ej. Cliente Centro" />
            </div>
          </div>

          <div class="vrp-grid-2">
            <div class="vrp-field">
              <label for="point-lat">Latitud</label>
              <input id="point-lat" type="number" step="any" class="vrp-input" placeholder="20.967073" />
            </div>
            <div class="vrp-field">
              <label for="point-lon">Longitud</label>
              <input id="point-lon" type="number" step="any" class="vrp-input" placeholder="-89.623739" />
            </div>
          </div>

          <div class="vrp-grid-3">
            <div class="vrp-field">
              <label for="point-start">Inicio</label>
              <input id="point-start" type="time" class="vrp-input" value="08:00" />
            </div>
            <div class="vrp-field">
              <label for="point-end">Fin</label>
              <input id="point-end" type="time" class="vrp-input" value="23:00" />
            </div>
            <div class="vrp-field">
              <label for="point-demand">Demanda</label>
              <input id="point-demand" type="number" min="0" class="vrp-input" value="3" />
            </div>
          </div>

          <div class="vrp-grid-2" style="margin-bottom:0;align-items:end;">
            <div class="vrp-field">
              <label for="point-trip-id" title="Para problemas Pickup-Delivery (PDP), dos puntos con el mismo ID_Viaje forman un par">ID Viaje</label>
              <input id="point-trip-id" type="number" min="0" class="vrp-input" value="1" />
            </div>
            <button id="btn-save-point" class="vrp-btn" style="background:var(--primary);color:#fff;border-color:var(--primary);height:30px;">
              <i class="fa fa-check"></i> <span id="btn-save-point-label">Guardar punto</span>
            </button>
          </div>
        </div>

        <!-- Banner de cambios sin recalcular -->
        <div id="vrp-dirty-banner" class="vrp-dirty-banner">
          <span><i class="fa fa-circle-exclamation"></i> Puntos modificados</span>
          <span style="text-decoration:underline;cursor:pointer;" id="btn-recalc-inline">Recalcular ahora</span>
        </div>

        <div id="vrp-points-list" class="vrp-points-list"></div>
      </div>

      <!-- PESTAÑA 2: OPCIONES Y PARÁMETROS IMPORTANTES -->
      <div id="tab-config" class="vrp-tab-pane">
        <div class="vrp-form-card open" style="display:block;">
          <div class="vrp-form-header">Flota y Capacidad</div>
          <div class="vrp-grid-2">
            <div class="vrp-field">
              <label for="cfg-num-vehicles">Vehículos disponibles</label>
              <input id="cfg-num-vehicles" type="number" min="1" max="200" class="vrp-input" value="5" />
            </div>
            <div class="vrp-field">
              <label for="cfg-vehicle-capacity">Capacidad por vehículo</label>
              <input id="cfg-vehicle-capacity" type="number" min="1" class="vrp-input" value="10" />
            </div>
          </div>

          <div class="vrp-grid-2" style="margin-bottom:0;">
            <div class="vrp-field">
              <label for="cfg-service-time">Tiempo servicio (min)</label>
              <input id="cfg-service-time" type="number" min="0" class="vrp-input" value="10" />
            </div>
            <div class="vrp-field">
              <label for="cfg-max-wait">Espera máx. (min)</label>
              <input id="cfg-max-wait" type="number" min="0" class="vrp-input" value="60" />
            </div>
          </div>
        </div>

        <div class="vrp-form-card open" style="display:block;margin-bottom:0;">
          <div class="vrp-form-header">Modelo de Ruteo y Red Vial</div>
          <div class="vrp-field" style="margin-bottom:8px;">
            <label for="cfg-problem-type">Tipo de problema VRP</label>
            <select id="cfg-problem-type" class="vrp-select">
              <option value="AUTO">Automático (detectar según CSV)</option>
              <option value="CVRPTW">CVRPTW (Capacidad + Ventanas de Horario)</option>
              <option value="CVRP">CVRP (Solo Capacidad, sin horarios)</option>
              <option value="PDP">PDP (Recogida y Entrega por ID_Viaje)</option>
            </select>
          </div>

          <div class="vrp-field" style="margin-bottom:8px;">
            <label for="cfg-service">Motor de Matriz de Calles</label>
            <select id="cfg-service" class="vrp-select">
              <option value="osrm">OSRM (Docker Local / OpenStreetMap)</option>
              <option value="here">HERE Maps Matrix API</option>
              <option value="ors">OpenRouteService API</option>
            </select>
          </div>

          <label style="display:flex;align-items:center;gap:8px;font-size:12px;font-weight:600;cursor:pointer;margin-top:8px;">
            <input id="cfg-depot-return" type="checkbox" checked />
            Retorno obligatorio al depósito al finalizar
          </label>
        </div>

        <div id="vrp-capacity-check" class="vrp-capacity-bar ok">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <span>Demanda total: <b id="lbl-total-demand">0</b></span>
            <span>Capacidad flota: <b id="lbl-total-cap">50</b></span>
          </div>
          <div class="vrp-cap-progress">
            <div id="vrp-cap-fill" class="vrp-cap-fill" style="width:10%;"></div>
          </div>
        </div>
      </div>

      <!-- PESTAÑA 3: GESTIÓN DE COSTOS OPERATIVOS -->
      <div id="tab-costs" class="vrp-tab-pane">
        <div class="vrp-form-card open" style="display:block;">
          <div class="vrp-form-header">
            <span>Tarifas Operativas</span>
            <button id="btn-reset-costs" class="vrp-icon-btn" style="font-size:11px;padding:2px 6px;color:var(--primary);" title="Restaurar costos por defecto del proyecto">
              <i class="fa fa-rotate-left"></i> Por defecto
            </button>
          </div>

          <div class="vrp-grid-2">
            <div class="vrp-field">
              <label for="cfg-cost-km">Costo por km (Combustible/Mant.)</label>
              <input id="cfg-cost-km" type="number" min="0" step="0.01" class="vrp-input" value="3.24" />
            </div>
            <div class="vrp-field">
              <label for="cfg-cost-hour">Costo por hora (Conductor)</label>
              <input id="cfg-cost-hour" type="number" min="0" step="0.01" class="vrp-input" value="81.25" />
            </div>
          </div>

          <div class="vrp-grid-2" style="margin-bottom:6px;">
            <div class="vrp-field">
              <label for="cfg-cost-fixed">Costo fijo por vehículo</label>
              <input id="cfg-cost-fixed" type="number" min="0" step="0.01" class="vrp-input" value="34.38" />
            </div>
            <div class="vrp-field">
              <label for="cfg-currency">Moneda</label>
              <select id="cfg-currency" class="vrp-select">
                <option value="MXN">MXN ($)</option>
                <option value="USD">USD ($)</option>
                <option value="EUR">EUR (€)</option>
              </select>
            </div>
          </div>

          <!-- Calculadora rápida de combustible por rendimiento -->
          <div style="border-top:1px dashed var(--border-light);padding-top:7px;margin-top:4px;">
            <div style="display:flex;justify-content:space-between;align-items:center;">
              <span style="font-size:11px;font-weight:600;color:var(--text-muted);">
                <i class="fa fa-gas-pump"></i> Calcular costo/km por combustible
              </span>
              <button id="btn-toggle-fuel-calc" class="vrp-icon-btn" style="font-size:11px;padding:2px 6px;color:var(--primary);">
                Abrir
              </button>
            </div>
            <div id="fuel-calc-box" style="display:none;margin-top:7px;">
              <div class="vrp-grid-3">
                <div class="vrp-field">
                  <label for="fuel-price-liter">Precio / L</label>
                  <input id="fuel-price-liter" type="number" min="0" step="0.1" class="vrp-input" value="24.50" />
                </div>
                <div class="vrp-field">
                  <label for="fuel-km-liter">Rend. (km/L)</label>
                  <input id="fuel-km-liter" type="number" min="0.1" step="0.1" class="vrp-input" value="8.5" />
                </div>
                <div class="vrp-field">
                  <label for="fuel-maint-km">Mant. / km</label>
                  <input id="fuel-maint-km" type="number" min="0" step="0.05" class="vrp-input" value="0.36" />
                </div>
              </div>
              <button id="btn-apply-fuel-calc" class="vrp-btn" style="width:100%;padding:5px 8px;font-size:11px;background:var(--primary-soft);color:var(--primary);border-color:#bfdbfe;">
                <i class="fa fa-calculator"></i> Aplicar al Costo por km
              </button>
            </div>
          </div>
        </div>

        <!-- Desglose financiero en tiempo real -->
        <div class="vrp-form-card open" style="display:block;margin-bottom:0;">
          <div class="vrp-form-header">
            <span>Resumen Financiero de Rutas</span>
            <span id="cost-live-total" style="font-size:13.5px;font-weight:700;color:var(--primary);">$0.00</span>
          </div>

          <div style="display:flex;flex-direction:column;gap:8px;margin-bottom:10px;">
            <div>
              <div style="display:flex;justify-content:space-between;font-size:11.5px;margin-bottom:2px;">
                <span style="color:var(--text-muted);"><i class="fa fa-road"></i> Combustible y Mant. (<span id="cost-lbl-km">0 km</span>)</span>
                <b id="cost-val-fuel">$0.00</b>
              </div>
              <div class="vrp-cap-progress" style="margin-top:2px;">
                <div id="cost-bar-fuel" class="vrp-cap-fill" style="width:0%;background:#2563eb;"></div>
              </div>
            </div>

            <div>
              <div style="display:flex;justify-content:space-between;font-size:11.5px;margin-bottom:2px;">
                <span style="color:var(--text-muted);"><i class="fa fa-id-card"></i> Conductores (<span id="cost-lbl-hours">0 h</span>)</span>
                <b id="cost-val-driver">$0.00</b>
              </div>
              <div class="vrp-cap-progress" style="margin-top:2px;">
                <div id="cost-bar-driver" class="vrp-cap-fill" style="width:0%;background:#ea580c;"></div>
              </div>
            </div>

            <div>
              <div style="display:flex;justify-content:space-between;font-size:11.5px;margin-bottom:2px;">
                <span style="color:var(--text-muted);"><i class="fa fa-truck"></i> Costo Fijo (<span id="cost-lbl-vehs">0 veh</span>)</span>
                <b id="cost-val-fixed">$0.00</b>
              </div>
              <div class="vrp-cap-progress" style="margin-top:2px;">
                <div id="cost-bar-fixed" class="vrp-cap-fill" style="width:0%;background:#16a34a;"></div>
              </div>
            </div>
          </div>

          <div class="vrp-grid-3" style="margin-bottom:8px;">
            <div class="vrp-kpi-box" style="padding:5px 7px;">
              <div class="vrp-kpi-label">Costo / Parada</div>
              <div id="cost-unit-stop" class="vrp-kpi-val" style="font-size:12px;">$0.00</div>
            </div>
            <div class="vrp-kpi-box" style="padding:5px 7px;">
              <div class="vrp-kpi-label">Costo / Unidad</div>
              <div id="cost-unit-load" class="vrp-kpi-val" style="font-size:12px;">$0.00</div>
            </div>
            <div class="vrp-kpi-box" style="padding:5px 7px;">
              <div class="vrp-kpi-label">Costo / km</div>
              <div id="cost-unit-km" class="vrp-kpi-val" style="font-size:12px;">$0.00</div>
            </div>
          </div>

          <div class="twcvrp-section-title" style="margin:6px 0 4px;">Costo por Vehículo</div>
          <div id="cost-per-vehicle-list" style="display:flex;flex-direction:column;gap:4px;max-height:115px;overflow-y:auto;"></div>
        </div>
      </div>
    </div>

    <div class="vrp-panel-footer">
      <button id="btn-run-solver" class="vrp-btn-solve">
        <i class="fa fa-bolt"></i>
        <span id="btn-run-solver-label">Calcular Rutas en el Mapa</span>
      </button>
      <div id="vrp-status-msg" class="vrp-status-msg">
        Haz clic en un punto o en una ruta para ver su información
      </div>
    </div>
  </aside>

  <!-- ================= BARRA DE BÚSQUEDA ================= -->
  <div class="twcvrp-search-bar">
    <i class="fa fa-search"></i>
    <input id="twcvrp-search-input" class="twcvrp-search-input" type="text" placeholder="Buscar parada o depósito..." autocomplete="off" />
    <div id="twcvrp-search-results" class="twcvrp-search-results"></div>
  </div>

  <!-- ================= PANEL DERECHO: LEYENDA Y RESULTADOS 3D ================= -->
  <div id="twcvrp-legend" class="twcvrp-legend-panel open">
    <div class="twcvrp-legend-header">
      <h4 class="twcvrp-legend-title">Rutas y Vista 3D</h4>
      <button id="twcvrp-legend-close" class="vrp-icon-btn" title="Ocultar panel derecho">
        <i class="fa fa-chevron-right"></i>
      </button>
    </div>
    <div class="twcvrp-legend-content">
      <div id="vrp-summary-kpis" style="display:none;">
        <div class="vrp-kpi-grid">
          <div class="vrp-kpi-box">
            <div class="vrp-kpi-label">Distancia Total</div>
            <div id="kpi-distance" class="vrp-kpi-val">0 km</div>
          </div>
          <div class="vrp-kpi-box">
            <div class="vrp-kpi-label">Tiempo Total</div>
            <div id="kpi-time" class="vrp-kpi-val">0 min</div>
          </div>
          <div class="vrp-kpi-box">
            <div class="vrp-kpi-label">Vehículos Usados</div>
            <div id="kpi-vehicles" class="vrp-kpi-val">0</div>
          </div>
          <div class="vrp-kpi-box">
            <div class="vrp-kpi-label">Costo Estimado</div>
            <div id="kpi-cost" class="vrp-kpi-val">$0</div>
          </div>
        </div>
        <hr class="twcvrp-divider" />
      </div>

      <div class="twcvrp-section-title">Cámara y Mapa</div>
      <div class="vrp-cam-grid">
        <button id="twcvrp-toggle-pitch" class="vrp-btn" title="Alternar inclinación 3D y vista 2D superior">
          <i class="fa fa-cube" style="color:var(--primary)"></i> <span id="lbl-toggle-pitch">Vista 3D</span>
        </button>
        <button id="btn-fit-bounds" class="vrp-btn" title="Encuadrar todos los puntos en pantalla">
          <i class="fa fa-expand"></i> Centrar todo
        </button>
      </div>
      <div class="vrp-field" style="margin-bottom:8px;">
        <select id="select-map-style" class="vrp-select" title="Cambiar estilo visual del mapa 3D">
          <option value="https://tiles.openfreemap.org/styles/positron">Mapa Claro (Positron 3D)</option>
          <option value="https://tiles.openfreemap.org/styles/liberty">Mapa Calles (Liberty 3D)</option>
          <option value="https://tiles.openfreemap.org/styles/bright">Mapa Detallado (Bright 3D)</option>
        </select>
      </div>

      <hr class="twcvrp-divider" />
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
        <span class="twcvrp-section-title" style="margin:0;">Rutas por vehículo</span>
        <button id="twcvrp-eye-all" class="vrp-icon-btn" style="font-size:11px;padding:2px 6px;" title="Mostrar u ocultar todas las rutas">
          <i class="fa fa-eye"></i> <span id="twcvrp-eye-all-label">Ocultar todas</span>
        </button>
      </div>
      <div id="twcvrp-routes-container"></div>
      <hr class="twcvrp-divider" />
      <p style="margin:0;color:var(--text-muted);font-size:11px;line-height:1.45;">
        <b>Tip:</b> Haz clic en un punto para ver sus detalles, haz clic en una ruta para ver la información del vehículo o gira la cámara 3D con <b>clic derecho</b>.
      </p>
    </div>
  </div>
  <button id="twcvrp-legend-toggle" class="twcvrp-legend-toggle" title="Mostrar rutas y vista 3D">
    <i class="fa fa-layer-group" style="color:var(--primary)"></i> Rutas 3D
  </button>

  <script>
    let MAP_DATA = {map_data_json};
    let LOCATIONS = {locations_json};
    let CONFIG = {config_json};
    let REPORT = {report_json};

    const API_BASE = (window.location.protocol.startsWith('http') && window.location.port === '8000')
      ? ''
      : 'http://127.0.0.1:8000';

    const map = new maplibregl.Map({{
      container: 'map',
      style: 'https://tiles.openfreemap.org/styles/positron',
      center: MAP_DATA.center,
      zoom: 13,
      pitch: 55,
      bearing: -15,
      maxPitch: 80,
      maxZoom: 22,
      antialias: true,
      fadeDuration: 0
    }});

    map.addControl(new maplibregl.NavigationControl({{
      visualizePitch: true,
      showZoom: true,
      showCompass: true
    }}), 'bottom-right');

    let mapLoaded = false;
    let mapClickAddMode = false;
    let is3D = true;
    let activeRouteLayerIds = [];
    let activeRouteSourceIds = [];
    let mapMarkers = [];
    let activePopup = null;
    const vehicleVisibility = {{}};
    let stopAssignmentMap = {{}}; // locIndex -> {{ vehicle_display_id, sequence, arrival, departure, color }}

    // ================= NOTIFICACIONES Y ESTADO =================
    let toastTimer = null;
    function showToast(text, type = '') {{
      const t = document.getElementById('vrp-toast');
      t.innerHTML = type === 'error'
        ? `<i class="fa fa-circle-xmark"></i> <span>${{text}}</span>`
        : `<i class="fa fa-circle-check"></i> <span>${{text}}</span>`;
      t.className = 'vrp-toast show' + (type ? ' ' + type : '');
      if (toastTimer) clearTimeout(toastTimer);
      toastTimer = setTimeout(() => {{
        t.classList.remove('show');
      }}, 3000);
    }}

    function setStatus(text, type = '') {{
      const el = document.getElementById('vrp-status-msg');
      el.textContent = text;
      el.className = 'vrp-status-msg' + (type ? ' ' + type : '');
      if (type === 'error' || type === 'success') {{
        showToast(text, type);
      }}
    }}

    function markRoutesDirty(isDirty) {{
      const banner = document.getElementById('vrp-dirty-banner');
      banner.classList.toggle('visible', Boolean(isDirty));
    }}

    // ================= PADDING DINÁMICO DE CÁMARA SEGÚN PANELES ABIERTOS =================
    function getMapPadding() {{
      const leftOpen = !document.getElementById('vrp-side-panel').classList.contains('collapsed');
      const rightOpen = document.getElementById('twcvrp-legend').classList.contains('open');
      const isNarrow = window.innerWidth < 768;
      return {{
        top: 80,
        bottom: 70,
        left: (!isNarrow && leftOpen) ? 410 : 60,
        right: (!isNarrow && rightOpen) ? 350 : 60
      }};
    }}

    function fitMapToLocations() {{
      if (!LOCATIONS.length) return;
      const b = new maplibregl.LngLatBounds();
      LOCATIONS.forEach(l => b.extend([Number(l.Longitud), Number(l.Latitud)]));
      if (MAP_DATA && MAP_DATA.routes) {{
        MAP_DATA.routes.forEach(r => {{
          (r.coordinates || []).forEach(c => b.extend(c));
        }});
      }}
      if (!b.isEmpty()) {{
        map.fitBounds(b, {{
          padding: getMapPadding(),
          maxZoom: 15.5,
          pitch: is3D ? 55 : 0,
          bearing: is3D ? -15 : 0,
          duration: 1000
        }});
      }}
    }}

    document.getElementById('btn-fit-bounds').addEventListener('click', fitMapToLocations);

    // ================= CONFIGURACIÓN, CAPACIDAD Y COSTOS =================
    function syncConfigToInputs() {{
      document.getElementById('cfg-num-vehicles').value = CONFIG.num_vehicles ?? 5;
      document.getElementById('cfg-vehicle-capacity').value = CONFIG.vehicle_capacity ?? 10;
      document.getElementById('cfg-service-time').value = CONFIG.service_time ?? 10;
      document.getElementById('cfg-max-wait').value = CONFIG.max_wait_time ?? 60;
      document.getElementById('cfg-problem-type').value = CONFIG.problem_type || 'AUTO';
      document.getElementById('cfg-service').value = CONFIG.service || 'osrm';
      document.getElementById('cfg-depot-return').checked = CONFIG.depot_return_required !== false;
      document.getElementById('cfg-cost-km').value = CONFIG.cost_per_km ?? 3.24;
      document.getElementById('cfg-cost-hour').value = CONFIG.driver_cost_per_hour ?? 81.25;
      document.getElementById('cfg-cost-fixed').value = CONFIG.fixed_cost_per_vehicle ?? 34.38;
      document.getElementById('cfg-currency').value = CONFIG.currency || 'MXN';
      updateCapacityIndicator();
      recalculateAndRenderCosts();
    }}

    function readConfigFromInputs() {{
      return {{
        num_vehicles: Math.max(1, parseInt(document.getElementById('cfg-num-vehicles').value, 10) || 5),
        vehicle_capacity: Math.max(1, parseInt(document.getElementById('cfg-vehicle-capacity').value, 10) || 10),
        service_time: Math.max(0, parseInt(document.getElementById('cfg-service-time').value, 10) || 0),
        max_wait_time: Math.max(0, parseInt(document.getElementById('cfg-max-wait').value, 10) || 60),
        max_time_per_vehicle: CONFIG.max_time_per_vehicle || 90000,
        depot_return_required: document.getElementById('cfg-depot-return').checked,
        problem_type: document.getElementById('cfg-problem-type').value,
        service: document.getElementById('cfg-service').value,
        cost_per_km: Math.max(0, parseFloat(document.getElementById('cfg-cost-km').value) || 0),
        driver_cost_per_hour: Math.max(0, parseFloat(document.getElementById('cfg-cost-hour').value) || 0),
        fixed_cost_per_vehicle: Math.max(0, parseFloat(document.getElementById('cfg-cost-fixed').value) || 0),
        currency: document.getElementById('cfg-currency').value || 'MXN'
      }};
    }}

    function formatMoney(amount, currencyCode) {{
      const code = currencyCode || document.getElementById('cfg-currency').value || 'MXN';
      const symbol = code === 'EUR' ? '€' : '$';
      return `${{symbol}}${{Number(amount || 0).toLocaleString('es-MX', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }})}} ${{code}}`;
    }}

    function recalculateAndRenderCosts() {{
      const cfg = readConfigFromInputs();
      CONFIG.cost_per_km = cfg.cost_per_km;
      CONFIG.driver_cost_per_hour = cfg.driver_cost_per_hour;
      CONFIG.fixed_cost_per_vehicle = cfg.fixed_cost_per_vehicle;
      CONFIG.currency = cfg.currency;

      const routes = (MAP_DATA && MAP_DATA.routes) ? MAP_DATA.routes : [];
      const sumReport = (REPORT && REPORT.summary) ? REPORT.summary : null;

      let totalDistKm = 0;
      let totalTimeMin = 0;
      let totalLoad = 0;
      let totalStops = 0;

      if (routes.length > 0) {{
        routes.forEach(r => {{
          const rDist = (r.total_distance_km !== undefined && r.total_distance_km !== '')
            ? Number(r.total_distance_km)
            : 0;
          let rMin = r.total_time_min;
          if (rMin === undefined && r.total_time_str) {{
            const p = String(r.total_time_str).split(':').map(Number);
            rMin = (p[0] || 0) * 60 + (p[1] || 0);
          }}
          totalDistKm += rDist;
          totalTimeMin += (rMin || 0);
          totalLoad += (Number(r.total_load) || 0);
          totalStops += (r.stops ? r.stops.length : 0);
        }});
        if (totalDistKm === 0 && sumReport && sumReport.total_distance_km) {{
          totalDistKm = Number(sumReport.total_distance_km);
        }}
      }} else if (sumReport) {{
        totalDistKm = Number(sumReport.total_distance_km || 0);
        totalTimeMin = Number(sumReport.total_time_minutes || 0);
        totalStops = Number(sumReport.total_stops_served || 0);
      }}

      const numVehUsed = routes.length || (sumReport ? Number(sumReport.total_vehicles_used || 0) : 0);
      const fuelCost = totalDistKm * cfg.cost_per_km;
      const driverCost = (totalTimeMin / 60) * cfg.driver_cost_per_hour;
      const fixedCost = numVehUsed * cfg.fixed_cost_per_vehicle;
      const totalCost = fuelCost + driverCost + fixedCost;

      const sym = cfg.currency === 'EUR' ? '€' : '$';
      const fmtShort = (v) => `${{sym}}${{Number(v || 0).toLocaleString('es-MX', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }})}}`;

      document.getElementById('cost-live-total').textContent = formatMoney(totalCost, cfg.currency);
      document.getElementById('cost-lbl-km').textContent = `${{totalDistKm.toFixed(2)}} km`;
      document.getElementById('cost-lbl-hours').textContent = `${{(totalTimeMin / 60).toFixed(2)}} h`;
      document.getElementById('cost-lbl-vehs').textContent = `${{numVehUsed}} veh`;

      document.getElementById('cost-val-fuel').textContent = fmtShort(fuelCost);
      document.getElementById('cost-val-driver').textContent = fmtShort(driverCost);
      document.getElementById('cost-val-fixed').textContent = fmtShort(fixedCost);

      const pFuel = totalCost > 0 ? Math.round((fuelCost / totalCost) * 100) : 0;
      const pDriver = totalCost > 0 ? Math.round((driverCost / totalCost) * 100) : 0;
      const pFixed = totalCost > 0 ? Math.round((fixedCost / totalCost) * 100) : 0;

      document.getElementById('cost-bar-fuel').style.width = `${{pFuel}}%`;
      document.getElementById('cost-bar-driver').style.width = `${{pDriver}}%`;
      document.getElementById('cost-bar-fixed').style.width = `${{pFixed}}%`;

      document.getElementById('cost-unit-stop').textContent = totalStops > 0 ? fmtShort(totalCost / totalStops) : `${{sym}}0.00`;
      document.getElementById('cost-unit-load').textContent = totalLoad > 0 ? fmtShort(totalCost / totalLoad) : `${{sym}}0.00`;
      document.getElementById('cost-unit-km').textContent = totalDistKm > 0 ? fmtShort(totalCost / totalDistKm) : `${{sym}}0.00`;

      // Actualizar KPI en panel derecho
      const kpiCostEl = document.getElementById('kpi-cost');
      if (kpiCostEl) kpiCostEl.textContent = fmtShort(totalCost);

      // Desglose por vehículo
      const vehListEl = document.getElementById('cost-per-vehicle-list');
      vehListEl.innerHTML = '';
      if (!routes.length) {{
        vehListEl.innerHTML = '<span style="font-size:11px;color:var(--text-muted);">Calcula las rutas para ver el costo por vehículo.</span>';
      }} else {{
        routes.forEach(r => {{
          const rDist = Number(r.total_distance_km || 0);
          let rMin = r.total_time_min;
          if (rMin === undefined && r.total_time_str) {{
            const p = String(r.total_time_str).split(':').map(Number);
            rMin = (p[0] || 0) * 60 + (p[1] || 0);
          }}
          const rCost = (rDist * cfg.cost_per_km) + (((rMin || 0) / 60) * cfg.driver_cost_per_hour) + cfg.fixed_cost_per_vehicle;
          r.calculated_cost = rCost;
          const row = document.createElement('div');
          row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;padding:5px 8px;background:#fff;border:1px solid var(--border-light);border-radius:7px;font-size:11.5px;';
          row.innerHTML = `
            <span style="display:flex;align-items:center;gap:6px;font-weight:600;">
              <span class="twcvrp-dot" style="background:${{r.color}};width:10px;height:10px;"></span>
              Vehículo ${{r.display_id}}
              <span style="color:var(--text-muted);font-weight:400;">(${{rDist.toFixed(1)}} km • ${{r.total_time_str}})</span>
            </span>
            <b>${{fmtShort(rCost)}}</b>
          `;
          vehListEl.appendChild(row);
        }});
      }}
    }}

    ['cfg-cost-km', 'cfg-cost-hour', 'cfg-cost-fixed', 'cfg-currency'].forEach(id => {{
      document.getElementById(id).addEventListener('input', recalculateAndRenderCosts);
      document.getElementById(id).addEventListener('change', recalculateAndRenderCosts);
    }});

    document.getElementById('btn-reset-costs').addEventListener('click', () => {{
      document.getElementById('cfg-cost-km').value = '3.24';
      document.getElementById('cfg-cost-hour').value = '81.25';
      document.getElementById('cfg-cost-fixed').value = '34.38';
      document.getElementById('cfg-currency').value = 'MXN';
      recalculateAndRenderCosts();
      setStatus('Tarifas restauradas a los valores base del proyecto.', 'success');
    }});

    document.getElementById('btn-toggle-fuel-calc').addEventListener('click', function() {{
      const box = document.getElementById('fuel-calc-box');
      const isOpen = box.style.display === 'block';
      box.style.display = isOpen ? 'none' : 'block';
      this.textContent = isOpen ? 'Abrir' : 'Cerrar';
    }});

    document.getElementById('btn-apply-fuel-calc').addEventListener('click', () => {{
      const priceL = parseFloat(document.getElementById('fuel-price-liter').value) || 0;
      const kmL = Math.max(0.1, parseFloat(document.getElementById('fuel-km-liter').value) || 1);
      const maintKm = parseFloat(document.getElementById('fuel-maint-km').value) || 0;
      const computedKmCost = Number(((priceL / kmL) + maintKm).toFixed(2));
      document.getElementById('cfg-cost-km').value = computedKmCost;
      recalculateAndRenderCosts();
      setStatus(`Costo por km actualizado a ${{computedKmCost}} según rendimiento.`, 'success');
    }});

    function updateCapacityIndicator() {{
      const cfg = readConfigFromInputs();
      const totalCap = cfg.num_vehicles * cfg.vehicle_capacity;
      const totalDemand = LOCATIONS.reduce((acc, loc, idx) => {{
        return acc + (idx === 0 ? 0 : (parseInt(loc.Demanda, 10) || 0));
      }}, 0);

      document.getElementById('lbl-total-demand').textContent = totalDemand;
      document.getElementById('lbl-total-cap').textContent = totalCap;
      const pct = totalCap > 0 ? Math.min(100, Math.round((totalDemand / totalCap) * 100)) : 100;
      document.getElementById('vrp-cap-fill').style.width = `${{pct}}%`;
      const bar = document.getElementById('vrp-capacity-check');
      bar.className = totalCap >= totalDemand ? 'vrp-capacity-bar ok' : 'vrp-capacity-bar warn';
    }}

    ['cfg-num-vehicles', 'cfg-vehicle-capacity', 'cfg-service-time', 'cfg-max-wait', 'cfg-problem-type', 'cfg-depot-return'].forEach(id => {{
      document.getElementById(id).addEventListener('change', () => {{
        updateCapacityIndicator();
        if (MAP_DATA.routes && MAP_DATA.routes.length > 0) markRoutesDirty(true);
      }});
      document.getElementById(id).addEventListener('input', updateCapacityIndicator);
    }});

    // ================= GESTIÓN DE PUNTOS Y MARCADORES UNIFICADOS =================
    function normalizeLocationsOrder() {{
      if (!LOCATIONS.length) return;
      const depotIdx = LOCATIONS.findIndex(l => l.ID === 0 || (l.Tipo_Servicio || '').toLowerCase().includes('dep'));
      if (depotIdx > 0) {{
        const [depot] = LOCATIONS.splice(depotIdx, 1);
        LOCATIONS.unshift(depot);
      }}
      LOCATIONS.forEach((loc, idx) => {{
        if (idx === 0) {{
          loc.ID = 0;
          loc.ID_Viaje = 0;
          loc.Tipo_Servicio = 'Depósito';
          loc.Demanda = 0;
        }} else {{
          loc.ID = idx;
          if (!loc.ID_Viaje || loc.ID_Viaje <= 0) loc.ID_Viaje = idx;
          if (!loc.Tipo_Servicio || loc.Tipo_Servicio === 'Depósito') loc.Tipo_Servicio = 'Entrega';
        }}
      }});
    }}

    function buildStopAssignmentLookup() {{
      stopAssignmentMap = {{}};
      if (!MAP_DATA || !MAP_DATA.routes) return;
      MAP_DATA.routes.forEach(route => {{
        (route.stops || []).forEach(st => {{
          let matchIdx = st.node_index;
          if (matchIdx === undefined || !LOCATIONS[matchIdx]) {{
            matchIdx = LOCATIONS.findIndex((l, idx) =>
              idx > 0 &&
              Math.abs(Number(l.Latitud) - Number(st.lat)) < 0.0001 &&
              Math.abs(Number(l.Longitud) - Number(st.lon)) < 0.0001
            );
          }}
          if (matchIdx > 0) {{
            stopAssignmentMap[matchIdx] = {{
              vehicle_display_id: route.display_id,
              sequence: st.sequence,
              arrival: st.arrival,
              departure: st.departure,
              service_time: st.service_time,
              color: route.color
            }};
          }}
        }});
      }});
    }}

    function buildPopupHtml(loc, idx) {{
      const isDepot = idx === 0;
      const assigned = stopAssignmentMap[idx];
      const routeBadge = assigned
        ? `<div style="margin:4px 0;padding:3px 7px;border-radius:6px;background:${{assigned.color}}18;color:${{assigned.color}};font-weight:700;font-size:11px;display:inline-block;">
             Vehículo ${{assigned.vehicle_display_id}} • Parada #${{assigned.sequence}} (${{assigned.arrival}} - ${{assigned.departure}})
           </div><br>`
        : '';

      return `
        <div>
          <div style="font-weight:700;font-size:13px;color:#0f172a;margin-bottom:2px;">${{loc.Nombre}}</div>
          ${{routeBadge}}
          <span style="color:#64748b;">Tipo:</span> <b>${{isDepot ? 'Depósito (Origen)' : loc.Tipo_Servicio}}</b><br>
          <span style="color:#64748b;">Horario:</span> ${{loc.Horario_Inicio}} - ${{loc.Horario_Fin}}<br>
          ${{!isDepot ? `<span style="color:#64748b;">Demanda:</span> <b>${{loc.Demanda}}</b> &nbsp;|&nbsp; <span style="color:#64748b;">Viaje:</span> #${{loc.ID_Viaje}}<br>` : ''}}
          <span style="color:#94a3b8;font-size:11px;">${{Number(loc.Latitud).toFixed(6)}}, ${{Number(loc.Longitud).toFixed(6)}}</span>
          <div class="vrp-popup-actions">
            <button class="vrp-popup-btn" onclick="window._vrpEditPoint(${{idx}})">
              <i class="fa fa-pen"></i> Editar
            </button>
            ${{!isDepot ? `<button class="vrp-popup-btn danger" onclick="window._vrpDeletePoint(${{idx}})">
              <i class="fa fa-trash-can"></i> Eliminar
            </button>` : ''}}
          </div>
        </div>
      `;
    }}

    window._vrpEditPoint = function(idx) {{
      if (activePopup) activePopup.remove();
      const sidePanel = document.getElementById('vrp-side-panel');
      sidePanel.classList.remove('collapsed');
      document.getElementById('vrp-side-toggle').style.display = 'none';
      document.querySelector('[data-tab="tab-points"]').click();
      loadPointIntoForm(idx);
    }};

    window._vrpDeletePoint = function(idx) {{
      if (idx <= 0 || idx >= LOCATIONS.length) return;
      if (activePopup) activePopup.remove();
      const removed = LOCATIONS.splice(idx, 1)[0];
      invalidateSolvedRoutes();
      renderPointsList();
      renderMapMarkers();
      setStatus(`Punto "${{removed.Nombre}}" eliminado.`, 'success');
    }};

    function buildPointsGeoJSON() {{
      buildStopAssignmentLookup();
      const features = [];

      LOCATIONS.forEach((loc, idx) => {{
        const isDepot = idx === 0;
        const assigned = stopAssignmentMap[idx];

        if (!isDepot && assigned && vehicleVisibility[assigned.vehicle_display_id] === false) {{
          return;
        }}

        const isRecogida = (loc.Tipo_Servicio || '').toLowerCase() === 'recogida';
        let fillColor = '#ef4444';
        let strokeColor = '#ffffff';
        let textColor = '#ffffff';
        let label = String(idx);
        let radius = 11.5;

        if (isDepot) {{
          fillColor = '#0f172a';
          strokeColor = '#ffffff';
          textColor = '#ffffff';
          label = 'D';
          radius = 14;
        }} else if (assigned) {{
          fillColor = '#ffffff';
          strokeColor = assigned.color;
          textColor = assigned.color;
          label = String(assigned.sequence);
          radius = 11.5;
        }} else if (isRecogida) {{
          fillColor = '#2563eb';
        }}

        features.push({{
          type: 'Feature',
          properties: {{
            idx: idx,
            label: label,
            radius: radius,
            fillColor: fillColor,
            strokeColor: strokeColor,
            textColor: textColor
          }},
          geometry: {{
            type: 'Point',
            coordinates: [Number(loc.Longitud), Number(loc.Latitud)]
          }}
        }});
      }});

      return {{
        type: 'FeatureCollection',
        features: features
      }};
    }}

    function renderMapMarkers() {{
      if (!mapLoaded) return;
      const geojsonData = buildPointsGeoJSON();

      if (map.getSource('vrp-points-source')) {{
        map.getSource('vrp-points-source').setData(geojsonData);
      }} else {{
        map.addSource('vrp-points-source', {{
          type: 'geojson',
          data: geojsonData
        }});
      }}

      if (!map.getLayer('vrp-points-circle')) {{
        map.addLayer({{
          id: 'vrp-points-circle',
          type: 'circle',
          source: 'vrp-points-source',
          paint: {{
            'circle-radius': ['get', 'radius'],
            'circle-color': ['get', 'fillColor'],
            'circle-stroke-color': ['get', 'strokeColor'],
            'circle-stroke-width': 2.8,
            'circle-pitch-alignment': 'viewport'
          }}
        }});
      }} else {{
        map.moveLayer('vrp-points-circle');
      }}

      if (!map.getLayer('vrp-points-label')) {{
        map.addLayer({{
          id: 'vrp-points-label',
          type: 'symbol',
          source: 'vrp-points-source',
          layout: {{
            'text-field': ['get', 'label'],
            'text-font': ['Noto Sans Bold'],
            'text-size': 12,
            'text-allow-overlap': true,
            'text-ignore-placement': true
          }},
          paint: {{
            'text-color': ['get', 'textColor']
          }}
        }});
      }} else {{
        map.moveLayer('vrp-points-label');
      }}
    }}

    function buildRoutePopupHtml(props) {{
      const vid = props.vehicle_id;
      const color = props.color || '#2563EB';
      const cfg = readConfigFromInputs();
      const sym = cfg.currency === 'EUR' ? '€' : '$';
      const distNum = Number(props.total_distance_km || 0);
      const distStr = distNum > 0
        ? `<span style="color:#64748b;">Distancia:</span> <b>${{distNum.toFixed(2)}} km</b><br>`
        : '';
      const timeParts = String(props.total_time || '00:00').split(':').map(Number);
      const timeMin = (timeParts[0] || 0) * 60 + (timeParts[1] || 0);
      const routeCost = (distNum * cfg.cost_per_km) + ((timeMin / 60) * cfg.driver_cost_per_hour) + cfg.fixed_cost_per_vehicle;
      const costStr = `<span style="color:#64748b;">Costo ruta:</span> <b>${{sym}}${{routeCost.toLocaleString('es-MX', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }})}} ${{cfg.currency}}</b><br>`;
      const capStr = props.vehicle_capacity
        ? `${{props.total_load}} / ${{props.vehicle_capacity}}`
        : `${{props.total_load}}`;
      const stopsListStr = props.stops_summary
        ? `<div style="margin-top:6px;padding-top:6px;border-top:1px solid #f1f5f9;font-size:11px;color:#475569;">
             <b>Recorrido:</b> ${{props.stops_summary}}
           </div>`
        : '';

      return `
        <div>
          <div style="display:flex;align-items:center;gap:7px;font-weight:700;font-size:13px;color:#0f172a;margin-bottom:4px;">
            <span class="twcvrp-dot" style="background:${{color}}"></span>
            <span>Ruta del Vehículo ${{vid}}</span>
          </div>
          <span style="color:#64748b;">Paradas asignadas:</span> <b>${{props.num_stops}}</b><br>
          <span style="color:#64748b;">Tiempo total:</span> <b>${{props.total_time}} h</b><br>
          ${{distStr}}
          <span style="color:#64748b;">Carga transportada:</span> <b>${{capStr}}</b><br>
          ${{costStr}}
          ${{stopsListStr}}
        </div>
      `;
    }}

    function renderPointsList() {{
      normalizeLocationsOrder();
      buildStopAssignmentLookup();
      const listEl = document.getElementById('vrp-points-list');
      const countEl = document.getElementById('vrp-points-count');
      const stopsSummaryEl = document.getElementById('lbl-stops-summary');
      countEl.textContent = LOCATIONS.length;
      const numStops = Math.max(0, LOCATIONS.length - 1);
      stopsSummaryEl.textContent = `${{numStops}} ${{numStops === 1 ? 'parada' : 'paradas'}}`;
      listEl.innerHTML = '';

      LOCATIONS.forEach((loc, idx) => {{
        const isDepot = idx === 0;
        const assigned = stopAssignmentMap[idx];
        const tipoClass = isDepot ? 'depot' : ((loc.Tipo_Servicio || '').toLowerCase() === 'recogida' ? 'recogida' : 'entrega');
        const badgeText = isDepot ? 'D' : (assigned ? assigned.sequence : idx);
        const badgeStyle = (!isDepot && assigned)
          ? `background:${{assigned.color}};color:#fff;border:none;`
          : '';

        const item = document.createElement('div');
        item.className = 'vrp-point-item';
        item.innerHTML = `
          <span class="vrp-point-badge ${{tipoClass}}" style="${{badgeStyle}}">${{badgeText}}</span>
          <div class="vrp-point-info">
            <div class="vrp-point-name">
              <span>${{loc.Nombre}}</span>
              ${{assigned ? `<span style="font-size:10px;padding:1px 5px;border-radius:4px;background:${{assigned.color}}18;color:${{assigned.color}};font-weight:700;">V${{assigned.vehicle_display_id}} • ${{assigned.arrival}}</span>` : ''}}
            </div>
            <div class="vrp-point-meta">
              ${{isDepot ? 'Depósito de salida' : loc.Tipo_Servicio + ' • Dem: ' + loc.Demanda + ' • Viaje #' + loc.ID_Viaje}}
              (${{loc.Horario_Inicio}}-${{loc.Horario_Fin}})
            </div>
          </div>
          <div class="vrp-point-actions">
            <button class="vrp-icon-btn btn-edit-pt" data-idx="${{idx}}" title="Editar punto" style="font-size:12px;padding:4px 6px;">
              <i class="fa fa-pen"></i>
            </button>
            ${{!isDepot ? `<button class="vrp-icon-btn btn-del-pt" data-idx="${{idx}}" title="Eliminar punto" style="font-size:12px;padding:4px 6px;color:var(--danger);">
              <i class="fa fa-xmark"></i>
            </button>` : ''}}
          </div>
        `;

        item.addEventListener('click', (e) => {{
          if (e.target.closest('.btn-edit-pt') || e.target.closest('.btn-del-pt')) return;
          focusLocationOnMap(idx);
        }});

        listEl.appendChild(item);
      }});

      listEl.querySelectorAll('.btn-edit-pt').forEach(btn => {{
        btn.addEventListener('click', () => {{
          const idx = parseInt(btn.getAttribute('data-idx'), 10);
          loadPointIntoForm(idx);
        }});
      }});

      listEl.querySelectorAll('.btn-del-pt').forEach(btn => {{
        btn.addEventListener('click', () => {{
          const idx = parseInt(btn.getAttribute('data-idx'), 10);
          window._vrpDeletePoint(idx);
        }});
      }});

      if (document.getElementById('point-edit-index').value === '-1') {{
        document.getElementById('point-trip-id').value = Math.max(1, LOCATIONS.length);
      }}
      updateCapacityIndicator();
    }}

    function focusLocationOnMap(idx) {{
      const loc = LOCATIONS[idx];
      if (!loc) return;
      if (activePopup) activePopup.remove();
      map.flyTo({{
        center: [Number(loc.Longitud), Number(loc.Latitud)],
        zoom: 16.5,
        pitch: is3D ? 58 : 0,
        padding: getMapPadding(),
        duration: 950
      }});
      const popup = new maplibregl.Popup({{ offset: 16 }})
        .setLngLat([Number(loc.Longitud), Number(loc.Latitud)])
        .setHTML(buildPopupHtml(loc, idx))
        .addTo(map);
      activePopup = popup;
    }}

    // ================= FORMULARIO MANUAL / EDICIÓN =================
    const formCard = document.getElementById('vrp-point-form-card');
    document.getElementById('btn-toggle-manual-form').addEventListener('click', () => {{
      if (formCard.classList.contains('open') && document.getElementById('point-edit-index').value === '-1') {{
        formCard.classList.remove('open');
      }} else {{
        resetPointForm();
        const center = map.getCenter();
        document.getElementById('point-lat').value = center.lat.toFixed(6);
        document.getElementById('point-lon').value = center.lng.toFixed(6);
        formCard.classList.add('open');
        document.getElementById('point-name').focus();
      }}
    }});

    function loadPointIntoForm(idx) {{
      const loc = LOCATIONS[idx];
      if (!loc) return;
      formCard.classList.add('open');
      document.getElementById('point-edit-index').value = String(idx);
      document.getElementById('form-point-title').textContent = `Editando: ${{loc.Nombre}}`;
      document.getElementById('btn-save-point-label').textContent = 'Actualizar';
      document.getElementById('point-type').value = idx === 0 ? 'Depósito' : (loc.Tipo_Servicio || 'Entrega');
      document.getElementById('point-name').value = loc.Nombre;
      document.getElementById('point-lat').value = Number(loc.Latitud).toFixed(6);
      document.getElementById('point-lon').value = Number(loc.Longitud).toFixed(6);
      document.getElementById('point-start').value = loc.Horario_Inicio || '08:00';
      document.getElementById('point-end').value = loc.Horario_Fin || '23:00';
      document.getElementById('point-demand').value = idx === 0 ? 0 : (loc.Demanda ?? 1);
      document.getElementById('point-trip-id').value = idx === 0 ? 0 : (loc.ID_Viaje ?? idx);
    }}

    function resetPointForm() {{
      document.getElementById('point-edit-index').value = '-1';
      document.getElementById('form-point-title').textContent = 'Agregar Punto Manual';
      document.getElementById('btn-save-point-label').textContent = 'Guardar punto';
      document.getElementById('point-type').value = LOCATIONS.length === 0 ? 'Depósito' : 'Entrega';
      document.getElementById('point-name').value = '';
      document.getElementById('point-lat').value = '';
      document.getElementById('point-lon').value = '';
      document.getElementById('point-demand').value = '3';
      document.getElementById('point-trip-id').value = String(Math.max(1, LOCATIONS.length));
    }}

    document.getElementById('btn-cancel-edit').addEventListener('click', () => {{
      resetPointForm();
      formCard.classList.remove('open');
    }});

    document.getElementById('btn-save-point').addEventListener('click', () => {{
      const editIdx = parseInt(document.getElementById('point-edit-index').value, 10);
      const tipo = document.getElementById('point-type').value;
      const lat = parseFloat(document.getElementById('point-lat').value);
      const lon = parseFloat(document.getElementById('point-lon').value);

      if (Number.isNaN(lat) || Number.isNaN(lon) || lat < -90 || lat > 90 || lon < -180 || lon > 180) {{
        setStatus('Ingresa una Latitud y Longitud válidas.', 'error');
        return;
      }}

      const isDepot = tipo === 'Depósito' || editIdx === 0;
      const defaultName = isDepot ? 'Depósito' : `Parada ${{editIdx >= 0 ? editIdx : LOCATIONS.length}}`;
      const nombre = document.getElementById('point-name').value.trim() || defaultName;
      const inicio = document.getElementById('point-start').value || '08:00';
      const fin = document.getElementById('point-end').value || '23:00';
      const demanda = isDepot ? 0 : (parseInt(document.getElementById('point-demand').value, 10) || 0);
      const idViaje = isDepot ? 0 : (parseInt(document.getElementById('point-trip-id').value, 10) || Math.max(1, LOCATIONS.length));

      const newObj = {{
        ID: isDepot ? 0 : (editIdx > 0 ? editIdx : LOCATIONS.length),
        ID_Viaje: idViaje,
        Tipo_Servicio: isDepot ? 'Depósito' : tipo,
        Nombre: nombre,
        Latitud: Number(lat.toFixed(6)),
        Longitud: Number(lon.toFixed(6)),
        Horario_Inicio: inicio,
        Horario_Fin: fin,
        Demanda: demanda
      }};

      if (editIdx >= 0 && editIdx < LOCATIONS.length) {{
        LOCATIONS[editIdx] = newObj;
      }} else if (isDepot && LOCATIONS.length > 0) {{
        LOCATIONS[0] = newObj;
      }} else {{
        LOCATIONS.push(newObj);
      }}

      resetPointForm();
      formCard.classList.remove('open');
      invalidateSolvedRoutes();
      renderPointsList();
      renderMapMarkers();
      setStatus(`Punto "${{nombre}}" guardado.`, 'success');
    }});

    document.getElementById('btn-clear-points').addEventListener('click', () => {{
      if (LOCATIONS.length <= 1) return;
      LOCATIONS = [LOCATIONS[0]];
      MAP_DATA.routes = [];
      clearRoutesFromMap();
      markRoutesDirty(false);
      renderPointsList();
      renderMapMarkers();
      setStatus('Paradas eliminadas. Se conservó el depósito.', 'success');
    }});

    document.getElementById('btn-recalc-inline').addEventListener('click', () => {{
      document.getElementById('btn-run-solver').click();
    }});

    // ================= MODO CLIC EN EL MAPA PARA AGREGAR PUNTOS =================
    const btnMapMode = document.getElementById('btn-map-click-mode');
    const boxMapMode = document.getElementById('map-mode-box');
    const lblMapMode = document.getElementById('map-click-mode-label');
    const mapBanner = document.getElementById('vrp-map-banner');

    function setMapClickMode(active) {{
      mapClickAddMode = active;
      btnMapMode.classList.toggle('active', active);
      boxMapMode.classList.toggle('active', active);
      mapBanner.classList.toggle('visible', active);
      document.getElementById('map').classList.toggle('crosshair-mode', active);
      lblMapMode.textContent = active
        ? 'Modo activo: Haz clic en el mapa para colocar'
        : 'Poner puntos haciendo clic en el mapa';
    }}

    btnMapMode.addEventListener('click', () => setMapClickMode(!mapClickAddMode));
    document.getElementById('btn-banner-exit').addEventListener('click', () => setMapClickMode(false));

    document.addEventListener('keydown', (e) => {{
      if (e.key === 'Escape' && mapClickAddMode) {{
        setMapClickMode(false);
      }}
    }});

    map.on('click', (e) => {{
      if (!mapClickAddMode) return;
      const lat = Number(e.lngLat.lat.toFixed(6));
      const lon = Number(e.lngLat.lng.toFixed(6));

      // Si el formulario de edición está abierto, capturar coordenadas ahí
      const editIdx = parseInt(document.getElementById('point-edit-index').value, 10);
      if (formCard.classList.contains('open') && editIdx >= 0) {{
        document.getElementById('point-lat').value = lat;
        document.getElementById('point-lon').value = lon;
        setStatus('Coordenadas capturadas desde el mapa.', 'success');
        return;
      }}

      const quickType = document.getElementById('quick-type').value;
      const isDepot = LOCATIONS.length === 0 || quickType === 'Depósito';
      const nextIdx = isDepot ? 0 : LOCATIONS.length;
      const nombre = isDepot ? 'Depósito' : `Parada ${{nextIdx}}`;
      const winParts = (document.getElementById('quick-window').value || '08:00-23:00').split('-');
      const inicio = isDepot ? '07:00' : (winParts[0] || '08:00');
      const fin = winParts[1] || '23:00';
      const demanda = isDepot ? 0 : (parseInt(document.getElementById('quick-demand').value, 10) || 3);

      const pt = {{
        ID: nextIdx,
        ID_Viaje: isDepot ? 0 : nextIdx,
        Tipo_Servicio: isDepot ? 'Depósito' : quickType,
        Nombre: nombre,
        Latitud: lat,
        Longitud: lon,
        Horario_Inicio: inicio,
        Horario_Fin: fin,
        Demanda: demanda
      }};

      if (isDepot && LOCATIONS.length > 0) {{
        LOCATIONS[0] = pt;
        document.getElementById('quick-type').value = 'Entrega';
      }} else {{
        LOCATIONS.push(pt);
      }}

      invalidateSolvedRoutes();
      renderPointsList();
      renderMapMarkers();
      setStatus(`Agregado "${{nombre}}" en el mapa.`, 'success');
    }});

    // ================= IMPORTAR / EXPORTAR CSV Y DEMO =================
    const fileInput = document.getElementById('input-csv-file');
    document.getElementById('btn-import-csv').addEventListener('click', () => fileInput.click());

    // Soporte Drag & Drop sobre el Side Panel
    const sidePanelEl = document.getElementById('vrp-side-panel');
    sidePanelEl.addEventListener('dragover', (e) => {{
      e.preventDefault();
      sidePanelEl.classList.add('drag-over');
    }});
    sidePanelEl.addEventListener('dragleave', () => {{
      sidePanelEl.classList.remove('drag-over');
    }});
    sidePanelEl.addEventListener('drop', async (e) => {{
      e.preventDefault();
      sidePanelEl.classList.remove('drag-over');
      const file = e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) await handleCsvFileUpload(file);
    }});

    fileInput.addEventListener('change', async (e) => {{
      const file = e.target.files[0];
      if (!file) return;
      await handleCsvFileUpload(file);
      fileInput.value = '';
    }});

    async function handleCsvFileUpload(file) {{
      const formData = new FormData();
      formData.append('file', file);

      try {{
        const resp = await fetch(`${{API_BASE}}/api/import-csv`, {{
          method: 'POST',
          body: formData
        }});
        if (resp.ok) {{
          const data = await resp.json();
          LOCATIONS = data.locations;
          MAP_DATA.routes = [];
          clearRoutesFromMap();
          markRoutesDirty(false);
          renderPointsList();
          renderMapMarkers();
          fitMapToLocations();
          setStatus(data.message, 'success');
          return;
        }} else {{
          const err = await resp.json();
          setStatus(err.detail || 'Error al importar CSV', 'error');
          return;
        }}
      }} catch (_) {{
        const text = await file.text();
        parseCsvInBrowser(text);
      }}
    }}

    function parseCsvInBrowser(csvText) {{
      const lines = csvText
        .split(String.fromCharCode(10))
        .map(l => l.replace(String.fromCharCode(13), '').trim())
        .filter(l => l.length > 0);
      if (lines.length < 2) {{
        setStatus('El archivo CSV está vacío.', 'error');
        return;
      }}
      const headers = lines[0].split(',').map(h => h.trim());
      const idxMap = {{}};
      headers.forEach((h, i) => idxMap[h] = i);

      if (idxMap['Latitud'] === undefined || idxMap['Longitud'] === undefined) {{
        setStatus('El CSV debe incluir las columnas Latitud y Longitud.', 'error');
        return;
      }}

      const parsed = [];
      for (let i = 1; i < lines.length; i++) {{
        const cols = lines[i].split(',').map(c => c.trim());
        const lat = parseFloat(cols[idxMap['Latitud']]);
        const lon = parseFloat(cols[idxMap['Longitud']]);
        if (Number.isNaN(lat) || Number.isNaN(lon)) continue;
        const id = idxMap['ID'] !== undefined ? (parseInt(cols[idxMap['ID']], 10) || 0) : (i - 1);
        const idViaje = idxMap['ID_Viaje'] !== undefined ? (parseInt(cols[idxMap['ID_Viaje']], 10) || 0) : id;
        let tipo = idxMap['Tipo_Servicio'] !== undefined ? cols[idxMap['Tipo_Servicio']] : 'Entrega';
        if (!tipo || tipo.toLowerCase() === 'nan' || id === 0) tipo = id === 0 ? 'Depósito' : 'Entrega';
        const nombre = idxMap['Nombre'] !== undefined ? cols[idxMap['Nombre']] : `Punto ${{i - 1}}`;
        const inicio = idxMap['Horario_Inicio'] !== undefined ? cols[idxMap['Horario_Inicio']] : '08:00';
        const fin = idxMap['Horario_Fin'] !== undefined ? cols[idxMap['Horario_Fin']] : '23:00';
        const demanda = idxMap['Demanda'] !== undefined ? (parseInt(cols[idxMap['Demanda']], 10) || 0) : 0;

        parsed.push({{
          ID: id,
          ID_Viaje: idViaje,
          Tipo_Servicio: tipo,
          Nombre: nombre,
          Latitud: lat,
          Longitud: lon,
          Horario_Inicio: inicio,
          Horario_Fin: fin,
          Demanda: demanda
        }});
      }}

      if (!parsed.length) {{
        setStatus('No se encontraron coordenadas válidas en el CSV.', 'error');
        return;
      }}

      LOCATIONS = parsed;
      MAP_DATA.routes = [];
      clearRoutesFromMap();
      markRoutesDirty(false);
      renderPointsList();
      renderMapMarkers();
      fitMapToLocations();
      setStatus(`Importadas ${{LOCATIONS.length}} ubicaciones del CSV.`, 'success');
    }}

    document.getElementById('btn-export-csv').addEventListener('click', () => {{
      normalizeLocationsOrder();
      const header = 'ID,ID_Viaje,Tipo_Servicio,Nombre,Latitud,Longitud,Horario_Inicio,Horario_Fin,Demanda';
      const rows = LOCATIONS.map((loc, idx) => {{
        const isDepot = idx === 0;
        const tipo = isDepot ? 'NaN' : (loc.Tipo_Servicio || 'Entrega');
        const safeName = String(loc.Nombre || '').replace(/,/g, ' ');
        return `${{isDepot ? 0 : idx}},${{isDepot ? 0 : loc.ID_Viaje}},${{tipo}},${{safeName}},${{loc.Latitud}},${{loc.Longitud}},${{loc.Horario_Inicio}},${{loc.Horario_Fin}},${{isDepot ? 0 : loc.Demanda}}`;
      }});
      const csvContent = [header, ...rows].join(String.fromCharCode(10));
      const blob = new Blob([csvContent], {{ type: 'text/csv;charset=utf-8;' }});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'data.csv';
      a.click();
      URL.revokeObjectURL(url);
      setStatus('Archivo data.csv descargado.', 'success');
    }});

    document.getElementById('btn-load-demo').addEventListener('click', () => {{
      LOCATIONS = [
        {{ ID: 0, ID_Viaje: 0, Tipo_Servicio: 'Depósito', Nombre: 'CEDIS Mérida Centro', Latitud: 20.967073, Longitud: -89.623739, Horario_Inicio: '07:00', Horario_Fin: '23:00', Demanda: 0 }},
        {{ ID: 1, ID_Viaje: 1, Tipo_Servicio: 'Entrega', Nombre: 'Sucursal Caucel', Latitud: 20.998512, Longitud: -89.701420, Horario_Inicio: '08:00', Horario_Fin: '18:00', Demanda: 4 }},
        {{ ID: 2, ID_Viaje: 2, Tipo_Servicio: 'Entrega', Nombre: 'Plaza Altabrisa', Latitud: 21.017840, Longitud: -89.584310, Horario_Inicio: '09:00', Horario_Fin: '21:00', Demanda: 5 }},
        {{ ID: 3, ID_Viaje: 3, Tipo_Servicio: 'Entrega', Nombre: 'Parque Industrial Umán', Latitud: 20.912135, Longitud: -89.702568, Horario_Inicio: '08:00', Horario_Fin: '20:00', Demanda: 4 }},
        {{ ID: 4, ID_Viaje: 4, Tipo_Servicio: 'Entrega', Nombre: 'Montejo Norte', Latitud: 21.028450, Longitud: -89.624890, Horario_Inicio: '08:30', Horario_Fin: '19:00', Demanda: 3 }},
        {{ ID: 5, ID_Viaje: 5, Tipo_Servicio: 'Entrega', Nombre: 'Kanasín Centro', Latitud: 20.936120, Longitud: -89.563210, Horario_Inicio: '10:00', Horario_Fin: '22:00', Demanda: 4 }},
        {{ ID: 6, ID_Viaje: 6, Tipo_Servicio: 'Entrega', Nombre: 'Aeropuerto Sur', Latitud: 20.937200, Longitud: -89.643800, Horario_Inicio: '08:00', Horario_Fin: '21:00', Demanda: 3 }}
      ];
      document.getElementById('cfg-num-vehicles').value = 3;
      document.getElementById('cfg-vehicle-capacity').value = 10;
      MAP_DATA.routes = [];
      clearRoutesFromMap();
      markRoutesDirty(true);
      renderPointsList();
      renderMapMarkers();
      fitMapToLocations();
      setStatus('Demo de 6 entregas en Mérida cargada. Presiona Calcular Rutas.', 'success');
    }});

    // ================= RENDERIZADO DE RUTAS 3D EN MAPLIBRE =================
    function invalidateSolvedRoutes() {{
      if (MAP_DATA && MAP_DATA.routes && MAP_DATA.routes.length > 0) {{
        MAP_DATA.routes = [];
        clearRoutesFromMap();
        markRoutesDirty(true);
      }}
    }}

    function clearRoutesFromMap() {{
      activeRouteLayerIds.forEach(id => {{
        if (map.getLayer(id)) map.removeLayer(id);
      }});
      activeRouteSourceIds.forEach(id => {{
        if (map.getSource(id)) map.removeSource(id);
      }});
      activeRouteLayerIds = [];
      activeRouteSourceIds = [];
      stopAssignmentMap = {{}};
      document.getElementById('twcvrp-routes-container').innerHTML =
        '<p style="margin:6px 0;color:var(--text-muted);font-size:11.5px;">Aún no hay rutas trazadas. Presiona <b>Calcular Rutas en el Mapa</b>.</p>';
    }}

    function add3DBuildingsLayer() {{
      const layers = map.getStyle().layers || [];
      let labelLayerId = undefined;
      for (let i = 0; i < layers.length; i++) {{
        if (layers[i].type === 'symbol' && layers[i].layout && layers[i].layout['text-field']) {{
          labelLayerId = layers[i].id;
          break;
        }}
      }}
      if (map.getSource('openmaptiles') && !map.getLayer('3d-buildings')) {{
        map.addLayer({{
          'id': '3d-buildings',
          'source': 'openmaptiles',
          'source-layer': 'building',
          'type': 'fill-extrusion',
          'minzoom': 13,
          'paint': {{
            'fill-extrusion-color': '#d9dee6',
            'fill-extrusion-height': [
              'interpolate', ['linear'], ['zoom'],
              13, 0,
              14.5, ['coalesce', ['get', 'render_height'], 12]
            ],
            'fill-extrusion-base': ['coalesce', ['get', 'render_min_height'], 0],
            'fill-extrusion-opacity': 0.82
          }}
        }}, labelLayerId);
      }}
    }}

    function renderSolvedRoutesOnMap(mapData, reportData, shouldFitBounds = true) {{
      if (!mapLoaded) return;
      clearRoutesFromMap();

      const routesContainer = document.getElementById('twcvrp-routes-container');
      routesContainer.innerHTML = '';

      if (!mapData || !mapData.routes || mapData.routes.length === 0) {{
        routesContainer.innerHTML =
          '<p style="margin:6px 0;color:var(--text-muted);font-size:11.5px;">Presiona <b>Calcular Rutas en el Mapa</b> para trazar las rutas óptimas.</p>';
        renderMapMarkers();
        return;
      }}

      mapData.routes.forEach((route) => {{
        const vid = route.display_id;
        if (vehicleVisibility[vid] === undefined) vehicleVisibility[vid] = true;

        const sourceId = `route-source-${{vid}}`;
        const casingLayerId = `route-casing-${{vid}}`;
        const lineLayerId = `route-line-${{vid}}`;

        const stopsSummary = [
          mapData.depot.name,
          ...(route.stops || []).map(s => s.name),
          ...(CONFIG.depot_return_required !== false ? [mapData.depot.name] : [])
        ].join(' → ');

        map.addSource(sourceId, {{
          type: 'geojson',
          data: {{
            type: 'Feature',
            properties: {{
              vehicle_id: vid,
              color: route.color,
              total_time: route.total_time_str,
              total_distance_km: route.total_distance_km ?? '',
              total_load: route.total_load,
              vehicle_capacity: route.vehicle_capacity ?? CONFIG.vehicle_capacity ?? '',
              num_stops: (route.stops || []).length,
              stops_summary: stopsSummary
            }},
            geometry: {{
              type: 'LineString',
              coordinates: route.coordinates
            }}
          }}
        }});
        activeRouteSourceIds.push(sourceId);

        map.addLayer({{
          id: casingLayerId,
          type: 'line',
          source: sourceId,
          layout: {{
            'line-join': 'round',
            'line-cap': 'round',
            'visibility': vehicleVisibility[vid] ? 'visible' : 'none'
          }},
          paint: {{
            'line-color': '#ffffff',
            'line-width': 8,
            'line-opacity': 0.92
          }}
        }});
        activeRouteLayerIds.push(casingLayerId);

        map.addLayer({{
          id: lineLayerId,
          type: 'line',
          source: sourceId,
          layout: {{
            'line-join': 'round',
            'line-cap': 'round',
            'visibility': vehicleVisibility[vid] ? 'visible' : 'none'
          }},
          paint: {{
            'line-color': route.color,
            'line-width': 5,
            'line-opacity': 0.95
          }}
        }});
        activeRouteLayerIds.push(lineLayerId);

        // Tarjeta expandible de ruta en el panel derecho
        const cardEl = document.createElement('div');
        cardEl.className = 'vrp-route-card';
        const stopsHtml = (route.stops || []).map(st => `
          <div class="vrp-stop-step" data-lat="${{st.lat}}" data-lon="${{st.lon}}" data-idx="${{st.node_index || 0}}">
            <span><b>#${{st.sequence}}</b> ${{st.name}}</span>
            <span style="color:var(--text-muted);">${{st.arrival}} • Dem: ${{st.demand}}</span>
          </div>
        `).join('');

        cardEl.innerHTML = `
          <div class="vrp-route-header">
            <span class="twcvrp-dot" style="background:${{route.color}}"></span>
            <span>Vehículo ${{vid}}</span>
            <span class="twcvrp-meta">${{route.stops.length}} paradas • ${{route.total_time_str}} • Carga: ${{route.total_load}}</span>
            <button class="vrp-icon-btn twcvrp-eye" data-vehicle-id="${{vid}}" title="Mostrar / Ocultar ruta" style="padding:2px 6px;">
              <i class="fa ${{vehicleVisibility[vid] ? 'fa-eye' : 'fa-eye-slash'}}"></i>
            </button>
          </div>
          <div class="vrp-route-stops-drawer">
            <div class="vrp-stop-step" data-lat="${{mapData.depot.lat}}" data-lon="${{mapData.depot.lon}}" data-idx="0">
              <span><b>Salida:</b> ${{mapData.depot.name}}</span>
              <span style="color:var(--text-muted);">Depósito</span>
            </div>
            ${{stopsHtml}}
          </div>
        `;

        cardEl.querySelector('.vrp-route-header').addEventListener('click', (e) => {{
          if (e.target.closest('.twcvrp-eye')) return;
          cardEl.classList.toggle('expanded');
          // Encuadrar ruta de este vehículo
          const rb = new maplibregl.LngLatBounds();
          (route.coordinates || []).forEach(c => rb.extend(c));
          if (!rb.isEmpty()) {{
            map.fitBounds(rb, {{ padding: getMapPadding(), maxZoom: 16, pitch: is3D ? 55 : 0, duration: 900 }});
          }}
        }});

        cardEl.querySelectorAll('.vrp-stop-step').forEach(stepEl => {{
          stepEl.addEventListener('click', () => {{
            const idx = parseInt(stepEl.getAttribute('data-idx'), 10);
            if (!Number.isNaN(idx) && LOCATIONS[idx]) {{
              focusLocationOnMap(idx);
            }}
          }});
        }});

        routesContainer.appendChild(cardEl);
      }});

      routesContainer.querySelectorAll('.twcvrp-eye[data-vehicle-id]').forEach(btn => {{
        btn.addEventListener('click', function(e) {{
          e.stopPropagation();
          const vid = parseInt(this.getAttribute('data-vehicle-id'), 10);
          setVehicleVisible(vid, !vehicleVisibility[vid]);
        }});
      }});

      if (reportData && reportData.summary) {{
        const s = reportData.summary;
        document.getElementById('vrp-summary-kpis').style.display = 'block';
        document.getElementById('kpi-distance').textContent = `${{s.total_distance_km}} km`;
        const hrs = Math.floor((s.total_time_minutes || 0) / 60);
        const mins = (s.total_time_minutes || 0) % 60;
        document.getElementById('kpi-time').textContent = hrs > 0 ? `${{hrs}}h ${{mins}}m` : `${{mins}} min`;
        document.getElementById('kpi-vehicles').textContent = `${{s.total_vehicles_used}} / ${{s.total_vehicles_available}}`;
        const cost = s.cost_analysis ? s.cost_analysis.total_cost : 0;
        document.getElementById('kpi-cost').textContent = `$${{Number(cost).toLocaleString('es-MX', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }})}}`;
      }}

      renderPointsList();
      renderMapMarkers();
      recalculateAndRenderCosts();

      if (shouldFitBounds) {{
        fitMapToLocations();
      }}
    }}

    // Listener global único para clics sobre puntos o líneas de ruta
    map.on('click', (e) => {{
      if (mapClickAddMode) return;

      // 1. Prioridad: si se hace clic en un punto (Depósito o Parada), mostrar información del punto
      const pointLayers = ['vrp-points-circle', 'vrp-points-label'].filter(id => map.getLayer(id));
      if (pointLayers.length > 0) {{
        const hitPoints = map.queryRenderedFeatures(e.point, {{ layers: pointLayers }});
        if (hitPoints.length > 0) {{
          const idx = hitPoints[0].properties.idx;
          const loc = LOCATIONS[idx];
          if (loc) {{
            if (activePopup) activePopup.remove();
            activePopup = new maplibregl.Popup({{ offset: 14 }})
              .setLngLat([Number(loc.Longitud), Number(loc.Latitud)])
              .setHTML(buildPopupHtml(loc, idx))
              .addTo(map);
          }}
          return;
        }}
      }}

      // 2. Si se hace clic en una ruta, mostrar información de la ruta
      const existingRouteLayers = activeRouteLayerIds.filter(id => map.getLayer(id));
      if (!existingRouteLayers.length) return;
      const hitRoutes = map.queryRenderedFeatures(e.point, {{ layers: existingRouteLayers }});
      if (!hitRoutes.length) return;
      const props = hitRoutes[0].properties;
      if (activePopup) activePopup.remove();
      activePopup = new maplibregl.Popup({{ offset: 10 }})
        .setLngLat(e.lngLat)
        .setHTML(buildRoutePopupHtml(props))
        .addTo(map);
    }});

    // Cursor pointer al pasar sobre un punto o sobre una ruta
    map.on('mousemove', (e) => {{
      if (mapClickAddMode) return;
      const interactiveLayers = [
        ...['vrp-points-circle', 'vrp-points-label'].filter(id => map.getLayer(id)),
        ...activeRouteLayerIds.filter(id => map.getLayer(id))
      ];
      if (!interactiveLayers.length) {{
        map.getCanvas().style.cursor = '';
        return;
      }}
      const hits = map.queryRenderedFeatures(e.point, {{ layers: interactiveLayers }});
      map.getCanvas().style.cursor = hits.length > 0 ? 'pointer' : '';
    }});

    // ================= EJECUTAR OPTIMIZACIÓN (/api/solve) =================
    const btnSolve = document.getElementById('btn-run-solver');
    const lblSolve = document.getElementById('btn-run-solver-label');

    btnSolve.addEventListener('click', async () => {{
      if (LOCATIONS.length < 2) {{
        setStatus('Agrega al menos 1 Depósito y 1 Parada antes de calcular.', 'error');
        return;
      }}

      CONFIG = readConfigFromInputs();
      btnSolve.disabled = true;
      lblSolve.textContent = 'Optimizando rutas...';
      setStatus('Calculando matriz vial y resolviendo VRP con OR-Tools...', '');

      try {{
        const resp = await fetch(`${{API_BASE}}/api/solve`, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            locations: LOCATIONS,
            config: CONFIG,
            save_to_csv: true
          }})
        }});

        const result = await resp.json();
        if (!resp.ok) {{
          setStatus(result.detail || 'No se pudo resolver el problema VRP.', 'error');
          return;
        }}

        MAP_DATA = result.map_data;
        REPORT = result.report;
        if (result.locations) {{
          LOCATIONS = result.locations;
        }}
        setMapClickMode(false);
        markRoutesDirty(false);
        renderSolvedRoutesOnMap(MAP_DATA, REPORT, true);
        setStatus(result.message, 'success');
      }} catch (err) {{
        try {{
          await solveInBrowserFallback();
        }} catch (fallbackErr) {{
          setStatus('Error al calcular rutas: ' + (fallbackErr.message || fallbackErr), 'error');
        }}
      }} finally {{
        btnSolve.disabled = false;
        lblSolve.textContent = 'Calcular Rutas en el Mapa';
      }}
    }});

    async function solveInBrowserFallback() {{
      normalizeLocationsOrder();
      const cfg = readConfigFromInputs();
      const depot = LOCATIONS[0];
      const customers = LOCATIONS.slice(1);
      const totalDemand = customers.reduce((s, c) => s + (parseInt(c.Demanda, 10) || 0), 0);
      const totalCap = cfg.num_vehicles * cfg.vehicle_capacity;
      if (totalCap < totalDemand) {{
        setStatus(`Capacidad total (${{totalCap}}) menor que demanda (${{totalDemand}}). Aumenta vehículos o capacidad.`, 'error');
        return;
      }}

      const COLORS = ['#2563EB', '#EA580C', '#16A34A', '#9333EA', '#E11D48', '#0891B2', '#D97706', '#4F46E5'];
      const toMin = (str) => {{
        const parts = String(str || '08:00').split(':').map(Number);
        return (parts[0] || 0) * 60 + (parts[1] || 0);
      }};
      const fmtMin = (m) => `${{String(Math.floor(m / 60)).padStart(2, '0')}}:${{String(Math.round(m % 60)).padStart(2, '0')}}`;

      const coordsQuery = LOCATIONS.map(l => `${{Number(l.Longitud).toFixed(6)}},${{Number(l.Latitud).toFixed(6)}}`).join(';');
      let osrmBase = 'http://localhost:5000';
      let tableData = null;
      for (const base of ['http://localhost:5000', 'https://router.project-osrm.org']) {{
        try {{
          const r = await fetch(`${{base}}/table/v1/driving/${{coordsQuery}}?annotations=duration,distance`);
          if (r.ok) {{
            const j = await r.json();
            if (j.code === 'Ok') {{
              tableData = j;
              osrmBase = base;
              break;
            }}
          }}
        }} catch (_) {{}}
      }}

      if (!tableData) {{
        throw new Error('No se pudo conectar a OSRM para obtener tiempos de calle.');
      }}

      const timeMat = tableData.durations.map(row => row.map(s => Math.max(0, Math.round((s || 0) / 60))));
      const distMat = tableData.distances.map(row => row.map(d => Number(((d || 0) / 1000).toFixed(2))));

      const unvisited = new Set(customers.map((_, i) => i + 1));
      const routesOut = [];
      let totalDistKm = 0;
      let totalTimeMin = 0;

      for (let v = 0; v < cfg.num_vehicles && unvisited.size > 0; v++) {{
        let curr = 0;
        let load = 0;
        let currTime = toMin(depot.Horario_Inicio || '07:00');
        const seqNodes = [0];
        const stopsPayload = [];
        let routeTime = 0;

        while (unvisited.size > 0) {{
          let bestNode = -1;
          let bestCost = Infinity;
          for (const cand of unvisited) {{
            const dem = parseInt(LOCATIONS[cand].Demanda, 10) || 0;
            if (load + dem > cfg.vehicle_capacity) continue;
            const travel = timeMat[curr][cand] || 1;
            const arr = Math.max(currTime + travel, toMin(LOCATIONS[cand].Horario_Inicio));
            if (cfg.problem_type !== 'CVRP' && arr > toMin(LOCATIONS[cand].Horario_Fin)) continue;
            if (travel < bestCost) {{
              bestCost = travel;
              bestNode = cand;
            }}
          }}
          if (bestNode === -1) break;
          unvisited.delete(bestNode);
          const travel = timeMat[curr][bestNode] || 1;
          totalDistKm += distMat[curr][bestNode] || 0;
          routeTime += travel + cfg.service_time;
          const arr = Math.max(currTime + travel, toMin(LOCATIONS[bestNode].Horario_Inicio));
          const dep = arr + cfg.service_time;
          load += parseInt(LOCATIONS[bestNode].Demanda, 10) || 0;
          currTime = dep;
          curr = bestNode;
          seqNodes.push(bestNode);

          stopsPayload.push({{
            sequence: stopsPayload.length + 1,
            node_index: bestNode,
            name: LOCATIONS[bestNode].Nombre,
            lat: Number(LOCATIONS[bestNode].Latitud),
            lon: Number(LOCATIONS[bestNode].Longitud),
            arrival: fmtMin(arr),
            service_time: cfg.service_time,
            departure: fmtMin(dep),
            demand: parseInt(LOCATIONS[bestNode].Demanda, 10) || 0,
            vehicle_display_id: v + 1,
            color: COLORS[v % COLORS.length]
          }});
        }}

        if (stopsPayload.length > 0) {{
          if (cfg.depot_return_required) {{
            routeTime += timeMat[curr][0] || 0;
            totalDistKm += distMat[curr][0] || 0;
            seqNodes.push(0);
          }}
          totalTimeMin += routeTime;

          const wpQuery = seqNodes.map(n => `${{Number(LOCATIONS[n].Longitud).toFixed(6)}},${{Number(LOCATIONS[n].Latitud).toFixed(6)}}`).join(';');
          let polyCoords = seqNodes.map(n => [Number(LOCATIONS[n].Longitud), Number(LOCATIONS[n].Latitud)]);
          try {{
            const rr = await fetch(`${{osrmBase}}/route/v1/driving/${{wpQuery}}?overview=full&geometries=geojson`);
            if (rr.ok) {{
              const rj = await rr.json();
              if (rj.routes && rj.routes[0] && rj.routes[0].geometry) {{
                polyCoords = rj.routes[0].geometry.coordinates;
              }}
            }}
          }} catch (_) {{}}

          routesOut.push({{
            vehicle_id: v,
            display_id: v + 1,
            color: COLORS[v % COLORS.length],
            total_time_str: fmtMin(routeTime),
            total_load: load,
            coordinates: polyCoords,
            stops: stopsPayload
          }});
        }}
      }}

      const totalCost = (totalDistKm * cfg.cost_per_km) + ((totalTimeMin / 60) * cfg.driver_cost_per_hour) + (routesOut.length * cfg.fixed_cost_per_vehicle);
      MAP_DATA = {{
        center: [Number(depot.Longitud), Number(depot.Latitud)],
        depot: {{ name: depot.Nombre, lat: Number(depot.Latitud), lon: Number(depot.Longitud) }},
        routes: routesOut
      }};
      REPORT = {{
        summary: {{
          total_vehicles_used: routesOut.length,
          total_vehicles_available: cfg.num_vehicles,
          total_stops_served: customers.length - unvisited.size,
          total_time_minutes: totalTimeMin,
          total_distance_km: Number(totalDistKm.toFixed(2)),
          cost_analysis: {{ total_cost: Number(totalCost.toFixed(2)) }}
        }}
      }};
      setMapClickMode(false);
      markRoutesDirty(false);
      renderSolvedRoutesOnMap(MAP_DATA, REPORT, true);
      setStatus(`¡Rutas calculadas! ${{routesOut.length}} vehículo(s) utilizado(s).`, 'success');
    }}

    // ================= CARGA Y CAMBIO DE ESTILO DEL MAPA 3D =================
    map.on('load', () => {{
      mapLoaded = true;
      add3DBuildingsLayer();
      if (MAP_DATA.routes && MAP_DATA.routes.length > 0) {{
        renderSolvedRoutesOnMap(MAP_DATA, REPORT, true);
      }} else {{
        renderMapMarkers();
        fitMapToLocations();
      }}
    }});

    document.getElementById('select-map-style').addEventListener('change', (e) => {{
      const newStyle = e.target.value;
      map.setStyle(newStyle);
      map.once('styledata', () => {{
        setTimeout(() => {{
          add3DBuildingsLayer();
          if (MAP_DATA.routes && MAP_DATA.routes.length > 0) {{
            renderSolvedRoutesOnMap(MAP_DATA, REPORT, false);
          }} else {{
            renderMapMarkers();
          }}
        }}, 250);
      }});
    }});

    // ================= VISIBILIDAD DE VEHÍCULOS Y CONTROLES 3D =================
    function setVehicleVisible(vid, visible) {{
      vehicleVisibility[vid] = visible;
      const visValue = visible ? 'visible' : 'none';
      ['route-casing-' + vid, 'route-line-' + vid].forEach(layerId => {{
        if (map.getLayer(layerId)) {{
          map.setLayoutProperty(layerId, 'visibility', visValue);
        }}
      }});
      const btn = document.querySelector(`.twcvrp-eye[data-vehicle-id="${{vid}}"]`);
      if (btn) {{
        btn.innerHTML = visible ? '<i class="fa fa-eye"></i>' : '<i class="fa fa-eye-slash"></i>';
        btn.title = visible ? 'Ocultar' : 'Mostrar';
      }}
      renderMapMarkers();
    }}

    document.getElementById('twcvrp-eye-all').addEventListener('click', function() {{
      const anyVisible = Object.values(vehicleVisibility).some(v => v);
      const target = !anyVisible;
      Object.keys(vehicleVisibility).forEach(vid => setVehicleVisible(parseInt(vid, 10), target));
      const label = document.getElementById('twcvrp-eye-all-label');
      if (label) label.textContent = target ? 'Ocultar todas' : 'Ver todas';
    }});

    document.getElementById('twcvrp-toggle-pitch').addEventListener('click', function() {{
      is3D = !is3D;
      document.getElementById('lbl-toggle-pitch').textContent = is3D ? 'Vista 3D' : 'Vista 2D';
      map.easeTo({{
        pitch: is3D ? 58 : 0,
        bearing: is3D ? -15 : 0,
        duration: 750
      }});
    }});

    // ================= COLAPSAR / EXPANDIR PANELES =================
    const sidePanel = document.getElementById('vrp-side-panel');
    const sideToggle = document.getElementById('vrp-side-toggle');
    document.getElementById('vrp-side-close').addEventListener('click', () => {{
      sidePanel.classList.add('collapsed');
      sideToggle.style.display = 'inline-flex';
    }});
    sideToggle.addEventListener('click', () => {{
      sidePanel.classList.remove('collapsed');
      sideToggle.style.display = 'none';
    }});

    const legendPanel = document.getElementById('twcvrp-legend');
    const legendToggle = document.getElementById('twcvrp-legend-toggle');
    document.getElementById('twcvrp-legend-close').addEventListener('click', () => {{
      legendPanel.classList.remove('open');
      legendToggle.style.display = 'inline-flex';
    }});
    legendToggle.addEventListener('click', () => {{
      legendPanel.classList.add('open');
      legendToggle.style.display = 'none';
    }});

    // Pestañas del Side Panel
    document.querySelectorAll('.vrp-tab-btn').forEach(btn => {{
      btn.addEventListener('click', () => {{
        document.querySelectorAll('.vrp-tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.vrp-tab-pane').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById(btn.getAttribute('data-tab')).classList.add('active');
      }});
    }});

    // ================= BUSCADOR EN TIEMPO REAL CON FLYTO =================
    const searchInput = document.getElementById('twcvrp-search-input');
    const searchResults = document.getElementById('twcvrp-search-results');

    searchInput.addEventListener('input', function() {{
      const q = this.value.trim().toLowerCase();
      searchResults.innerHTML = '';
      if (!q) {{
        searchResults.style.display = 'none';
        return;
      }}

      const matches = LOCATIONS
        .map((l, idx) => ({{ loc: l, idx }}))
        .filter(item => {{
          const assigned = stopAssignmentMap[item.idx];
          const text = `${{item.loc.Nombre}} ${{item.idx === 0 ? 'deposito' : item.loc.Tipo_Servicio}} ${{assigned ? 'vehiculo ' + assigned.vehicle_display_id : ''}}`.toLowerCase();
          return text.includes(q);
        }});

      if (!matches.length) {{
        searchResults.style.display = 'none';
        return;
      }}

      matches.forEach(({{ loc, idx }}) => {{
        const assigned = stopAssignmentMap[idx];
        const item = document.createElement('div');
        item.className = 'twcvrp-search-item';
        item.innerHTML = `
          <span><b>${{loc.Nombre}}</b></span>
          <span style="color:var(--text-muted);font-size:11px;">
            ${{idx === 0 ? 'Depósito' : (assigned ? `Vehículo ${{assigned.vehicle_display_id}} (#${{assigned.sequence}})` : loc.Tipo_Servicio)}}
          </span>
        `;
        item.addEventListener('click', () => {{
          searchResults.style.display = 'none';
          searchInput.value = loc.Nombre;
          focusLocationOnMap(idx);
        }});
        searchResults.appendChild(item);
      }});
      searchResults.style.display = 'block';
    }});

    document.addEventListener('click', (e) => {{
      if (!e.target.closest('.twcvrp-search-bar')) {{
        searchResults.style.display = 'none';
      }}
    }});

    // Inicializar estado
    syncConfigToInputs();
    renderPointsList();
  </script>
</body>
</html>
"""

    try:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"\nMapa 3D guardado como '{output_file}'")
        return output_file
    except Exception as e:
        print(f"Error al guardar el mapa 3D: {e}")
        return None
