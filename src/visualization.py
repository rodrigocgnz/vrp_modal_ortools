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
    "#FF4757",  # Rojo vibrante
    "#3742FA",  # Azul intenso
    "#2ED573",  # Verde esmeralda
    "#FFA502",  # Naranja cálido
    "#FF6348",  # Coral
    "#5F27CD",  # Violeta eléctrico
    "#00D2D3",  # Turquesa brillante
    "#FFD700",  # Amarillo dorado
    "#FF69B4",  # Rosa neón
    "#1E90FF",  # Azul dodger
    "#32CD32",  # Verde lima
    "#8A2BE2",  # Azul violeta
    "#FF8C00",  # Naranja oscuro
    "#00FA9A",  # Verde primavera
    "#8B4513",  # Marrón cuero
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
        """Si apunta a localhost y OSRM no responde, ejecuta setup_osrm.ps1 si auto_start_docker=True."""
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
        """
        Obtiene la geometría completa siguiendo las calles para todas las paradas de un vehículo
        en una sola llamada a /route/v1/{profile}/ en OSRM.
        stops_coords: lista de [lat, lon] en orden de visita.
        Retorna lista de [lat, lon].
        """
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
                "total_load": route["total_load"],
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
    con un Side Panel simple e integrado para gestionar puntos (clic en el mapa o importar CSV),
    configurar parámetros del VRP y calcular/visualizar rutas sobre las calles reales.
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
  <link rel="stylesheet" href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" />
  <script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" />
  <style>
    :root {{
      --panel-bg: rgba(255, 255, 255, 0.96);
      --panel-border: rgba(0, 0, 0, 0.08);
      --panel-shadow: 0 8px 24px rgba(0, 0, 0, 0.14);
      --primary: #2563eb;
      --primary-hover: #1d4ed8;
      --text-main: #1e293b;
      --text-muted: #64748b;
      --border-light: #e2e8f0;
      --surface-muted: #f8fafc;
      --danger: #ef4444;
      --success: #16a34a;
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
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, Arial, sans-serif;
      color: var(--text-main);
    }}

    #map {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
    }}

    #map.crosshair-mode canvas {{
      cursor: crosshair !important;
    }}

    /* ================= SIDE PANEL IZQUIERDO ================= */
    .vrp-side-panel {{
      position: fixed;
      top: 14px;
      left: 14px;
      width: 365px;
      max-width: calc(100vw - 28px);
      max-height: calc(100vh - 28px);
      background: var(--panel-bg);
      border-radius: 12px;
      box-shadow: var(--panel-shadow);
      border: 1px solid var(--panel-border);
      z-index: 10001;
      display: flex;
      flex-direction: column;
      backdrop-filter: blur(8px);
      transition: transform 240ms cubic-bezier(0.22, 1, 0.36, 1), opacity 200ms ease;
    }}

    .vrp-side-panel.collapsed {{
      transform: translateX(calc(-100% - 24px));
      opacity: 0;
      pointer-events: none;
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
      border-radius: 10px;
      box-shadow: var(--panel-shadow);
      font-size: 13px;
      font-weight: 600;
      display: none;
      align-items: center;
      gap: 8px;
      transition: background 150ms ease;
    }}
    .vrp-side-toggle:hover {{
      background: #f1f5f9;
    }}

    .vrp-panel-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 14px;
      border-bottom: 1px solid var(--border-light);
    }}

    .vrp-panel-title {{
      margin: 0;
      font-size: 15px;
      font-weight: 700;
      color: #111827;
      display: flex;
      align-items: center;
      gap: 8px;
    }}

    .vrp-icon-btn {{
      appearance: none;
      border: none;
      background: transparent;
      cursor: pointer;
      font-size: 16px;
      color: var(--text-muted);
      padding: 4px 8px;
      border-radius: 6px;
      transition: background 150ms ease, color 150ms ease;
    }}
    .vrp-icon-btn:hover {{
      background: #f1f5f9;
      color: #0f172a;
    }}

    /* Pestañas simples */
    .vrp-tabs {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      padding: 6px 14px 0;
      gap: 6px;
      border-bottom: 1px solid var(--border-light);
      background: rgba(248, 250, 252, 0.6);
    }}

    .vrp-tab-btn {{
      appearance: none;
      border: none;
      background: transparent;
      padding: 8px 10px;
      font-size: 13px;
      font-weight: 600;
      color: var(--text-muted);
      cursor: pointer;
      border-bottom: 2px solid transparent;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
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
      padding: 1px 6px;
      border-radius: 10px;
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

    /* Acciones CSV y Modo Mapa */
    .vrp-toolbar {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
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

    .vrp-btn-map-mode {{
      width: 100%;
      margin-bottom: 10px;
      padding: 8px 12px;
      background: #f0fdf4;
      border: 1px dashed #86efac;
      color: #166534;
    }}
    .vrp-btn-map-mode:hover {{
      background: #dcfce7;
      border-color: #4ade80;
    }}
    .vrp-btn-map-mode.active {{
      background: #16a34a;
      border: 1px solid #15803d;
      color: #ffffff;
      box-shadow: 0 4px 12px rgba(22, 163, 74, 0.25);
    }}

    /* Formulario compacto para agregar/editar punto */
    .vrp-form-card {{
      background: var(--surface-muted);
      border: 1px solid var(--border-light);
      border-radius: 10px;
      padding: 10px;
      margin-bottom: 10px;
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
      border-radius: 6px;
      font-size: 12px;
      color: var(--text-main);
      background: #ffffff;
      outline: none;
      transition: border-color 150ms ease;
    }}
    .vrp-input:focus, .vrp-select:focus {{
      border-color: var(--primary);
      box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.12);
    }}

    /* Lista de puntos */
    .vrp-points-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin: 8px 0 6px;
    }}

    .vrp-points-list {{
      display: flex;
      flex-direction: column;
      gap: 6px;
      max-height: 250px;
      overflow-y: auto;
      padding-right: 2px;
    }}

    .vrp-point-item {{
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 7px 9px;
      background: #ffffff;
      border: 1px solid var(--border-light);
      border-radius: 8px;
      cursor: pointer;
      transition: border-color 150ms ease, background 150ms ease;
    }}
    .vrp-point-item:hover {{
      border-color: #93c5fd;
      background: #f8fafc;
    }}

    .vrp-point-badge {{
      width: 22px;
      height: 22px;
      border-radius: 50%;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 11px;
      font-weight: 700;
      flex-shrink: 0;
    }}
    .vrp-point-badge.depot {{
      background: #111827;
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
      font-size: 12px;
      font-weight: 600;
      color: #0f172a;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .vrp-point-meta {{
      font-size: 11px;
      color: var(--text-muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}

    .vrp-point-actions {{
      display: flex;
      gap: 2px;
    }}

    /* Indicador capacidad vs demanda */
    .vrp-capacity-bar {{
      background: var(--surface-muted);
      border: 1px solid var(--border-light);
      border-radius: 8px;
      padding: 8px 10px;
      margin-top: 8px;
      font-size: 12px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .vrp-capacity-bar.ok {{
      border-color: #bbf7d0;
      background: #f0fdf4;
      color: #166534;
    }}
    .vrp-capacity-bar.warn {{
      border-color: #fecaca;
      background: #fef2f2;
      color: #991b1b;
    }}

    /* Footer fijo del side panel */
    .vrp-panel-footer {{
      padding: 10px 14px 12px;
      border-top: 1px solid var(--border-light);
      background: rgba(255, 255, 255, 0.95);
      border-radius: 0 0 12px 12px;
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
      box-shadow: 0 4px 12px rgba(37, 99, 235, 0.25);
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
      padding: 6px 12px;
      display: flex;
      align-items: center;
      gap: 8px;
      backdrop-filter: blur(6px);
    }}
    .twcvrp-search-bar i {{
      color: #666;
      font-size: 14px;
    }}
    .twcvrp-search-input {{
      border: none;
      outline: none;
      background: transparent;
      width: 100%;
      font-size: 13px;
      color: #222;
      padding: 4px 0;
    }}
    .twcvrp-search-results {{
      position: absolute;
      top: calc(100% + 6px);
      left: 0;
      right: 0;
      background: #fff;
      border-radius: 10px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.15);
      border: 1px solid rgba(0, 0, 0, 0.08);
      max-height: 240px;
      overflow-y: auto;
      display: none;
    }}
    .twcvrp-search-item {{
      padding: 8px 12px;
      font-size: 12px;
      color: #333;
      cursor: pointer;
      border-bottom: 1px solid #f2f2f2;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .twcvrp-search-item:last-child {{ border-bottom: none; }}
    .twcvrp-search-item:hover {{ background: #f5f7fa; }}

    /* ================= PANEL DERECHO DE LEYENDA Y RESULTADOS ================= */
    .twcvrp-legend-panel {{
      position: fixed;
      top: 14px;
      right: -330px;
      width: 295px;
      max-height: calc(100vh - 28px);
      overflow-y: auto;
      background: var(--panel-bg);
      border-radius: 12px;
      box-shadow: var(--panel-shadow);
      border: 1px solid var(--panel-border);
      z-index: 9999;
      transition: right 260ms ease;
      backdrop-filter: blur(6px);
    }}
    .twcvrp-legend-panel.open {{ right: 14px; }}
    .twcvrp-legend-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 14px;
      border-bottom: 1px solid #eee;
    }}
    .twcvrp-legend-title {{ margin: 0; font-size: 14px; font-weight: 700; color: #222; }}
    .twcvrp-legend-close {{
      appearance: none;
      border: none;
      background: transparent;
      cursor: pointer;
      font-size: 18px;
      line-height: 1;
      color: #666;
      padding: 4px 8px;
      border-radius: 6px;
    }}
    .twcvrp-legend-close:hover {{ background: #f2f2f2; color: #111; }}
    .twcvrp-legend-content {{ padding: 12px 14px; font-size: 13px; color: #333; }}
    .twcvrp-item {{ margin: 5px 0; display: flex; align-items: center; gap: 8px; font-size: 12px; }}
    .twcvrp-divider {{ margin: 10px 0; border: 0; border-top: 1px solid #eee; }}
    .twcvrp-section-title {{ margin: 0 0 6px 0; font-size: 12px; font-weight: 700; color: #333; text-transform: uppercase; letter-spacing: 0.02em; }}
    .twcvrp-route-item {{ display: flex; align-items: center; gap: 8px; margin: 6px 0; font-size: 12px; }}
    .twcvrp-dot {{
      width: 12px;
      height: 12px;
      border-radius: 50%;
      display: inline-block;
      box-shadow: inset 0 0 0 2px #fff, 0 0 0 1px rgba(0,0,0,0.15);
      flex-shrink: 0;
    }}
    .twcvrp-meta {{ margin-left: auto; color: #666; font-size: 11px; }}
    .twcvrp-route-actions {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
    .twcvrp-eye {{
      appearance: none;
      border: none;
      background: transparent;
      cursor: pointer;
      color: #444;
      padding: 4px 6px;
      border-radius: 6px;
    }}
    .twcvrp-eye:hover {{ background: #f2f2f2; color: #111; }}
    .twcvrp-btn-3d {{
      appearance: none;
      border: 1px solid #ddd;
      background: #f8f9fa;
      cursor: pointer;
      color: #222;
      padding: 5px 10px;
      border-radius: 8px;
      font-size: 12px;
      font-weight: 600;
    }}
    .twcvrp-btn-3d:hover {{ background: #eef1f5; }}
    .twcvrp-hint {{ margin: 0; color: #666; font-size: 11px; line-height: 1.4; }}
    .twcvrp-legend-toggle {{
      position: fixed;
      top: 14px;
      right: 14px;
      z-index: 10000;
      cursor: pointer;
      background: #ffffff;
      border: 1px solid rgba(0,0,0,0.08);
      color: #222;
      padding: 10px 12px;
      border-radius: 10px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.15);
      font-size: 13px;
      font-weight: 600;
      display: none;
    }}

    /* Resumen de métricas en leyenda */
    .vrp-kpi-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
      margin-bottom: 10px;
    }}
    .vrp-kpi-box {{
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 8px;
      padding: 6px 8px;
    }}
    .vrp-kpi-label {{
      font-size: 10px;
      color: var(--text-muted);
      font-weight: 600;
    }}
    .vrp-kpi-val {{
      font-size: 13px;
      font-weight: 700;
      color: #0f172a;
    }}

    /* Marcadores arrastrables de edición en el mapa */
    .draft-marker {{
      width: 24px;
      height: 24px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 11px;
      font-weight: 700;
      cursor: grab;
      box-shadow: 0 3px 8px rgba(0,0,0,0.28);
      border: 2px solid #ffffff;
      user-select: none;
    }}
    .draft-marker.depot {{
      width: 30px;
      height: 30px;
      background: #111827;
      color: #ffffff;
      font-size: 13px;
    }}
    .draft-marker.entrega {{
      background: #ef4444;
      color: #ffffff;
    }}
    .draft-marker.recogida {{
      background: #2563eb;
      color: #ffffff;
    }}

    .maplibregl-popup-content {{
      border-radius: 10px;
      padding: 12px 14px;
      box-shadow: 0 6px 20px rgba(0,0,0,0.18);
      font-size: 12.5px;
      line-height: 1.45;
    }}

    @media (max-width: 900px) {{
      .twcvrp-search-bar {{
        display: none;
      }}
    }}
  </style>
</head>
<body>
  <div id="map"></div>

  <!-- Botón para abrir Side Panel cuando está colapsado -->
  <button id="vrp-side-toggle" class="vrp-side-toggle" title="Abrir panel de puntos y opciones">
    <i class="fa fa-sliders"></i> Panel VRP
  </button>

  <!-- ================= SIDE PANEL IZQUIERDO ================= -->
  <aside id="vrp-side-panel" class="vrp-side-panel">
    <div class="vrp-panel-header">
      <h3 class="vrp-panel-title">
        <i class="fa fa-route" style="color:var(--primary)"></i>
        Planificador VRP
      </h3>
      <button id="vrp-side-close" class="vrp-icon-btn" title="Ocultar panel">
        <i class="fa fa-chevron-left"></i>
      </button>
    </div>

    <div class="vrp-tabs">
      <button class="vrp-tab-btn active" data-tab="tab-points">
        <i class="fa fa-location-dot"></i> Puntos
        <span id="vrp-points-count" class="vrp-badge-count">0</span>
      </button>
      <button class="vrp-tab-btn" data-tab="tab-config">
        <i class="fa fa-gear"></i> Opciones
      </button>
    </div>

    <div class="vrp-panel-body">
      <!-- PESTAÑA 1: PUNTOS Y CSV -->
      <div id="tab-points" class="vrp-tab-pane active">
        <div class="vrp-toolbar">
          <button id="btn-import-csv" class="vrp-btn" title="Importar archivo CSV como data/data.csv">
            <i class="fa fa-file-arrow-up" style="color:var(--primary)"></i> Importar CSV
          </button>
          <button id="btn-export-csv" class="vrp-btn" title="Descargar puntos actuales en CSV">
            <i class="fa fa-file-arrow-down"></i> Exportar CSV
          </button>
          <input id="input-csv-file" type="file" accept=".csv,text/csv" style="display:none" />
        </div>

        <button id="btn-map-click-mode" class="vrp-btn vrp-btn-map-mode">
          <i class="fa fa-crosshairs"></i>
          <span id="map-click-mode-label">Agregar puntos haciendo clic en el mapa</span>
        </button>

        <!-- Formulario para agregar o editar un punto -->
        <div class="vrp-form-card">
          <div class="vrp-form-header">
            <span id="form-point-title">Nuevo Punto / Parada</span>
            <button id="btn-cancel-edit" class="vrp-icon-btn" style="display:none;font-size:11px;padding:2px 6px;" title="Cancelar edición">
              Cancelar
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
              <input id="point-demand" type="number" min="0" class="vrp-input" value="1" />
            </div>
          </div>

          <div class="vrp-grid-2" style="margin-bottom:0;align-items:end;">
            <div class="vrp-field">
              <label for="point-trip-id" title="Para problemas Pickup-Delivery (PDP), dos puntos con el mismo ID_Viaje forman un par">ID Viaje</label>
              <input id="point-trip-id" type="number" min="0" class="vrp-input" value="1" />
            </div>
            <button id="btn-save-point" class="vrp-btn" style="background:var(--text-main);color:#fff;border-color:var(--text-main);height:30px;">
              <i class="fa fa-plus"></i> <span id="btn-save-point-label">Guardar punto</span>
            </button>
          </div>
        </div>

        <div class="vrp-points-header">
          <span class="twcvrp-section-title" style="margin:0;">Ubicaciones cargadas</span>
          <button id="btn-clear-points" class="vrp-icon-btn" style="font-size:11px;color:var(--danger);" title="Dejar solo el depósito">
            <i class="fa fa-trash-can"></i> Limpiar paradas
          </button>
        </div>

        <div id="vrp-points-list" class="vrp-points-list"></div>
      </div>

      <!-- PESTAÑA 2: OPCIONES Y PARÁMETROS IMPORTANTES -->
      <div id="tab-config" class="vrp-tab-pane">
        <div class="vrp-form-card">
          <div class="vrp-form-header">Flota y Capacidad</div>
          <div class="vrp-grid-2">
            <div class="vrp-field">
              <label for="cfg-num-vehicles">Número de vehículos</label>
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

        <div class="vrp-form-card" style="margin-bottom:0;">
          <div class="vrp-form-header">Modelo de Ruteo y Motor</div>
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

          <label style="display:flex;align-items:center;gap:8px;font-size:12px;font-weight:600;cursor:pointer;margin-top:6px;">
            <input id="cfg-depot-return" type="checkbox" checked />
            Retorno obligatorio al depósito al finalizar
          </label>
        </div>

        <div id="vrp-capacity-check" class="vrp-capacity-bar ok">
          <span>Demanda total: <b id="lbl-total-demand">0</b></span>
          <span>Capacidad flota: <b id="lbl-total-cap">50</b></span>
        </div>
      </div>
    </div>

    <div class="vrp-panel-footer">
      <button id="btn-run-solver" class="vrp-btn-solve">
        <i class="fa fa-bolt"></i>
        <span id="btn-run-solver-label">Calcular Rutas en el Mapa</span>
      </button>
      <div id="vrp-status-msg" class="vrp-status-msg">
        Haz clic en el mapa o importa tu CSV para comenzar
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
      <button id="twcvrp-legend-close" class="twcvrp-legend-close" title="Ocultar">×</button>
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

      <div class="twcvrp-item"><span class="vrp-point-badge depot" style="width:16px;height:16px;font-size:9px;">D</span> Depósito</div>
      <div class="twcvrp-item"><i class="fa fa-circle" style="color:#ef4444;"></i> Parada de Entrega / Recogida</div>
      <hr class="twcvrp-divider" />
      <div class="twcvrp-route-actions">
        <button id="twcvrp-toggle-pitch" class="twcvrp-btn-3d" title="Alternar vista 3D / 2D">
          <i class="fa fa-cube"></i> Vista 3D / 2D
        </button>
        <button id="twcvrp-eye-all" class="twcvrp-eye" title="Ocultar todas">
          <i class="fa fa-eye"></i> <span id="twcvrp-eye-all-label">Ocultar todas</span>
        </button>
      </div>
      <div class="twcvrp-section-title">Rutas por vehículo</div>
      <div id="twcvrp-routes-container"></div>
      <hr class="twcvrp-divider" />
      <p class="twcvrp-hint">
        <b>Tip:</b> Puedes arrastrar los puntos en el mapa para ajustar su ubicación o rotar la cámara 3D con <b>clic derecho</b>.
      </p>
    </div>
  </div>
  <button id="twcvrp-legend-toggle" class="twcvrp-legend-toggle" title="Mostrar rutas y leyenda">☰ Rutas</button>

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
    let activeRouteLayerIds = [];
    let activeRouteSourceIds = [];
    let draftMarkers = [];
    const vehicleVisibility = {{}};
    let searchableLocations = [];

    // ================= INICIALIZAR CONTROLES DE CONFIGURACIÓN =================
    function syncConfigToInputs() {{
      document.getElementById('cfg-num-vehicles').value = CONFIG.num_vehicles ?? 5;
      document.getElementById('cfg-vehicle-capacity').value = CONFIG.vehicle_capacity ?? 10;
      document.getElementById('cfg-service-time').value = CONFIG.service_time ?? 10;
      document.getElementById('cfg-max-wait').value = CONFIG.max_wait_time ?? 60;
      document.getElementById('cfg-problem-type').value = CONFIG.problem_type || 'AUTO';
      document.getElementById('cfg-service').value = CONFIG.service || 'osrm';
      document.getElementById('cfg-depot-return').checked = CONFIG.depot_return_required !== false;
      updateCapacityIndicator();
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
        service: document.getElementById('cfg-service').value
      }};
    }}

    function updateCapacityIndicator() {{
      const cfg = readConfigFromInputs();
      const totalCap = cfg.num_vehicles * cfg.vehicle_capacity;
      const totalDemand = LOCATIONS.reduce((acc, loc, idx) => {{
        const isDepot = idx === 0 || loc.ID === 0 || (loc.Tipo_Servicio || '').toLowerCase().includes('dep');
        return acc + (isDepot ? 0 : (parseInt(loc.Demanda, 10) || 0));
      }}, 0);

      document.getElementById('lbl-total-demand').textContent = totalDemand;
      document.getElementById('lbl-total-cap').textContent = totalCap;
      const bar = document.getElementById('vrp-capacity-check');
      if (totalCap >= totalDemand) {{
        bar.className = 'vrp-capacity-bar ok';
      }} else {{
        bar.className = 'vrp-capacity-bar warn';
      }}
    }}

    ['cfg-num-vehicles', 'cfg-vehicle-capacity'].forEach(id => {{
      document.getElementById(id).addEventListener('input', updateCapacityIndicator);
    }});

    // ================= GESTIÓN DE PUNTOS (LISTA + MARCADORES EN MAPA) =================
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

    function renderPointsList() {{
      normalizeLocationsOrder();
      const listEl = document.getElementById('vrp-points-list');
      const countEl = document.getElementById('vrp-points-count');
      countEl.textContent = LOCATIONS.length;
      listEl.innerHTML = '';

      LOCATIONS.forEach((loc, idx) => {{
        const isDepot = idx === 0;
        const tipoClass = isDepot ? 'depot' : ((loc.Tipo_Servicio || '').toLowerCase() === 'recogida' ? 'recogida' : 'entrega');
        const badgeText = isDepot ? 'D' : idx;

        const item = document.createElement('div');
        item.className = 'vrp-point-item';
        item.innerHTML = `
          <span class="vrp-point-badge ${{tipoClass}}">${{badgeText}}</span>
          <div class="vrp-point-info">
            <div class="vrp-point-name">${{loc.Nombre}}</div>
            <div class="vrp-point-meta">
              ${{isDepot ? 'Depósito' : loc.Tipo_Servicio + ' • Dem: ' + loc.Demanda + ' • Viaje #' + loc.ID_Viaje}}
              (${{loc.Horario_Inicio}}-${{loc.Horario_Fin}})
            </div>
          </div>
          <div class="vrp-point-actions">
            <button class="vrp-icon-btn btn-edit-pt" data-idx="${{idx}}" title="Editar punto" style="font-size:12px;padding:3px 5px;">
              <i class="fa fa-pen"></i>
            </button>
            ${{!isDepot ? `<button class="vrp-icon-btn btn-del-pt" data-idx="${{idx}}" title="Eliminar punto" style="font-size:12px;padding:3px 5px;color:var(--danger);">
              <i class="fa fa-xmark"></i>
            </button>` : ''}}
          </div>
        `;

        item.addEventListener('click', (e) => {{
          if (e.target.closest('.btn-edit-pt') || e.target.closest('.btn-del-pt')) return;
          map.flyTo({{
            center: [loc.Longitud, loc.Latitud],
            zoom: 16,
            pitch: is3D ? 55 : 0,
            duration: 900
          }});
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
          LOCATIONS.splice(idx, 1);
          renderPointsList();
          renderDraftMarkers();
          updateCapacityIndicator();
        }});
      }});

      // Sugerir siguiente ID_Viaje en el formulario si no se está editando
      if (document.getElementById('point-edit-index').value === '-1') {{
        document.getElementById('point-trip-id').value = Math.max(1, LOCATIONS.length);
      }}
      updateCapacityIndicator();
    }}

    function loadPointIntoForm(idx) {{
      const loc = LOCATIONS[idx];
      if (!loc) return;
      document.getElementById('point-edit-index').value = String(idx);
      document.getElementById('form-point-title').textContent = `Editando: ${{loc.Nombre}}`;
      document.getElementById('btn-cancel-edit').style.display = 'inline-block';
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
      document.getElementById('form-point-title').textContent = 'Nuevo Punto / Parada';
      document.getElementById('btn-cancel-edit').style.display = 'none';
      document.getElementById('btn-save-point-label').textContent = 'Guardar punto';
      document.getElementById('point-type').value = LOCATIONS.length === 0 ? 'Depósito' : 'Entrega';
      document.getElementById('point-name').value = '';
      document.getElementById('point-lat').value = '';
      document.getElementById('point-lon').value = '';
      document.getElementById('point-demand').value = '5';
      document.getElementById('point-trip-id').value = String(Math.max(1, LOCATIONS.length));
    }}

    document.getElementById('btn-cancel-edit').addEventListener('click', resetPointForm);

    document.getElementById('btn-save-point').addEventListener('click', () => {{
      const editIdx = parseInt(document.getElementById('point-edit-index').value, 10);
      const tipo = document.getElementById('point-type').value;
      const lat = parseFloat(document.getElementById('point-lat').value);
      const lon = parseFloat(document.getElementById('point-lon').value);

      if (Number.isNaN(lat) || Number.isNaN(lon) || lat < -90 || lat > 90 || lon < -180 || lon > 180) {{
        setStatus('Ingresa una Latitud y Longitud válidas o haz clic en el mapa.', 'error');
        return;
      }}

      const isDepot = tipo === 'Depósito';
      const defaultName = isDepot ? 'Depósito' : `Parada ${{editIdx >= 0 ? editIdx : LOCATIONS.length}}`;
      const nombre = document.getElementById('point-name').value.trim() || defaultName;
      const inicio = document.getElementById('point-start').value || '08:00';
      const fin = document.getElementById('point-end').value || '23:00';
      const demanda = isDepot ? 0 : (parseInt(document.getElementById('point-demand').value, 10) || 0);
      const idViaje = isDepot ? 0 : (parseInt(document.getElementById('point-trip-id').value, 10) || Math.max(1, LOCATIONS.length));

      const newObj = {{
        ID: isDepot ? 0 : (editIdx > 0 ? editIdx : LOCATIONS.length),
        ID_Viaje: idViaje,
        Tipo_Servicio: tipo,
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
      renderPointsList();
      renderDraftMarkers();
      setStatus(`Punto "${{nombre}}" guardado.`, 'success');
    }});

    document.getElementById('btn-clear-points').addEventListener('click', () => {{
      if (LOCATIONS.length <= 1) return;
      LOCATIONS = [LOCATIONS[0]];
      clearRoutesFromMap();
      renderPointsList();
      renderDraftMarkers();
      setStatus('Paradas eliminadas. Se conservó el depósito.', '');
    }});

    // ================= MARCADORES ARRASTRABLES DE PUNTOS EN EL MAPA =================
    function renderDraftMarkers() {{
      draftMarkers.forEach(m => m.remove());
      draftMarkers = [];

      LOCATIONS.forEach((loc, idx) => {{
        const isDepot = idx === 0;
        const el = document.createElement('div');
        const tipoClass = isDepot ? 'depot' : ((loc.Tipo_Servicio || '').toLowerCase() === 'recogida' ? 'recogida' : 'entrega');
        el.className = `draft-marker ${{tipoClass}}`;
        el.textContent = isDepot ? 'D' : String(idx);
        el.title = `${{loc.Nombre}} (Arrastra para mover)`;

        const popupHtml = `
          <b>${{loc.Nombre}}</b><br>
          Tipo: ${{isDepot ? 'Depósito' : loc.Tipo_Servicio}}<br>
          Horario: ${{loc.Horario_Inicio}} - ${{loc.Horario_Fin}}<br>
          ${{!isDepot ? `Demanda: ${{loc.Demanda}} | ID Viaje: ${{loc.ID_Viaje}}<br>` : ''}}
          Coords: ${{Number(loc.Latitud).toFixed(6)}}, ${{Number(loc.Longitud).toFixed(6)}}
        `;

        const marker = new maplibregl.Marker({{ element: el, draggable: true }})
          .setLngLat([loc.Longitud, loc.Latitud])
          .setPopup(new maplibregl.Popup({{ offset: 14 }}).setHTML(popupHtml))
          .addTo(map);

        marker.on('dragend', () => {{
          const lngLat = marker.getLngLat();
          LOCATIONS[idx].Latitud = Number(lngLat.lat.toFixed(6));
          LOCATIONS[idx].Longitud = Number(lngLat.lng.toFixed(6));
          renderPointsList();
          setStatus(`Ubicación de "${{LOCATIONS[idx].Nombre}}" actualizada en el mapa.`, 'success');
        }});

        draftMarkers.push(marker);
      }});
    }}

    // ================= MODO CLIC EN EL MAPA PARA AGREGAR PUNTOS =================
    const btnMapMode = document.getElementById('btn-map-click-mode');
    const lblMapMode = document.getElementById('map-click-mode-label');

    function setMapClickMode(active) {{
      mapClickAddMode = active;
      btnMapMode.classList.toggle('active', active);
      document.getElementById('map').classList.toggle('crosshair-mode', active);
      lblMapMode.textContent = active
        ? 'Modo activo: Haz clic en el mapa para poner puntos'
        : 'Agregar puntos haciendo clic en el mapa';
    }}

    btnMapMode.addEventListener('click', () => {{
      setMapClickMode(!mapClickAddMode);
    }});

    map.on('click', (e) => {{
      if (!mapClickAddMode) return;
      const lat = Number(e.lngLat.lat.toFixed(6));
      const lon = Number(e.lngLat.lng.toFixed(6));

      // Si el usuario está editando un punto existente en el formulario, actualizar sus coordenadas
      const editIdx = parseInt(document.getElementById('point-edit-index').value, 10);
      if (editIdx >= 0) {{
        document.getElementById('point-lat').value = lat;
        document.getElementById('point-lon').value = lon;
        setStatus('Coordenadas capturadas desde el mapa. Presiona Actualizar.', 'success');
        return;
      }}

      const tipoSelect = document.getElementById('point-type').value;
      const isDepot = LOCATIONS.length === 0 || tipoSelect === 'Depósito';
      const nextIdx = isDepot ? 0 : LOCATIONS.length;
      const customName = document.getElementById('point-name').value.trim();
      const nombre = customName || (isDepot ? 'Depósito' : `Parada ${{nextIdx}}`);
      const inicio = document.getElementById('point-start').value || (isDepot ? '07:00' : '08:00');
      const fin = document.getElementById('point-end').value || '23:00';
      const demanda = isDepot ? 0 : (parseInt(document.getElementById('point-demand').value, 10) || 5);
      const idViaje = isDepot ? 0 : nextIdx;

      const pt = {{
        ID: nextIdx,
        ID_Viaje: idViaje,
        Tipo_Servicio: isDepot ? 'Depósito' : tipoSelect,
        Nombre: nombre,
        Latitud: lat,
        Longitud: lon,
        Horario_Inicio: inicio,
        Horario_Fin: fin,
        Demanda: demanda
      }};

      if (isDepot && LOCATIONS.length > 0) {{
        LOCATIONS[0] = pt;
      }} else {{
        LOCATIONS.push(pt);
      }}

      document.getElementById('point-name').value = '';
      if (isDepot) document.getElementById('point-type').value = 'Entrega';
      renderPointsList();
      renderDraftMarkers();
      setStatus(`Agregado "${{nombre}}" (${{lat}}, ${{lon}})`, 'success');
    }});

    // ================= IMPORTAR / EXPORTAR CSV =================
    const fileInput = document.getElementById('input-csv-file');
    document.getElementById('btn-import-csv').addEventListener('click', () => fileInput.click());

    fileInput.addEventListener('change', async (e) => {{
      const file = e.target.files[0];
      if (!file) return;

      // Intentar primero vía backend /api/import-csv; si se abrió como archivo local, parsear en navegador
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
          clearRoutesFromMap();
          renderPointsList();
          renderDraftMarkers();
          fitMapToLocations();
          setStatus(data.message, 'success');
          fileInput.value = '';
          return;
        }} else {{
          const err = await resp.json();
          setStatus(err.detail || 'Error al importar CSV', 'error');
          fileInput.value = '';
          return;
        }}
      }} catch (_) {{
        // Fallback parser local en navegador si el usuario abrió el HTML directamente sin servidor
        const text = await file.text();
        parseCsvInBrowser(text);
        fileInput.value = '';
      }}
    }});

    function parseCsvInBrowser(csvText) {{
      const lines = csvText.split(new RegExp('\\r?\\n')).filter(l => l.trim().length > 0);
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
      clearRoutesFromMap();
      renderPointsList();
      renderDraftMarkers();
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
      const csvContent = [header, ...rows].join('\\n');
      const blob = new Blob([csvContent], {{ type: 'text/csv;charset=utf-8;' }});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'data.csv';
      a.click();
      URL.revokeObjectURL(url);
      setStatus('Archivo data.csv descargado.', 'success');
    }});

    function fitMapToLocations() {{
      if (!LOCATIONS.length) return;
      const b = new maplibregl.LngLatBounds();
      LOCATIONS.forEach(l => b.extend([l.Longitud, l.Latitud]));
      if (!b.isEmpty()) {{
        map.fitBounds(b, {{ padding: 90, maxZoom: 15, pitch: is3D ? 55 : 0, duration: 1000 }});
      }}
    }}

    // ================= RENDERIZADO DE RUTAS 3D Y PARADAS EN MAPLIBRE =================
    function clearRoutesFromMap() {{
      activeRouteLayerIds.forEach(id => {{
        if (map.getLayer(id)) map.removeLayer(id);
      }});
      activeRouteSourceIds.forEach(id => {{
        if (map.getSource(id)) map.removeSource(id);
      }});
      activeRouteLayerIds = [];
      activeRouteSourceIds = [];
      ['stops-label', 'stops-circle', 'depot-label', 'depot-circle'].forEach(id => {{
        if (map.getLayer(id)) map.removeLayer(id);
      }});
      ['stops-source', 'depot-source'].forEach(id => {{
        if (map.getSource(id)) map.removeSource(id);
      }});
      document.getElementById('twcvrp-routes-container').innerHTML =
        '<p class="twcvrp-hint" style="margin:6px 0;">Aún no hay rutas calculadas. Presiona <b>Calcular Rutas en el Mapa</b>.</p>';
    }}

    function renderSolvedRoutesOnMap(mapData, reportData) {{
      if (!mapLoaded) return;
      clearRoutesFromMap();

      const routesContainer = document.getElementById('twcvrp-routes-container');
      routesContainer.innerHTML = '';
      searchableLocations = [];

      const depotHtml =
        `<b>${{mapData.depot.name}}</b><br>Tipo: Depósito<br>Coords: ${{mapData.depot.lat.toFixed(6)}}, ${{mapData.depot.lon.toFixed(6)}}`;

      searchableLocations.push({{
        name: mapData.depot.name + ' (Depósito)',
        lon: mapData.depot.lon,
        lat: mapData.depot.lat,
        html: depotHtml
      }});

      if (!mapData.routes || mapData.routes.length === 0) {{
        routesContainer.innerHTML =
          '<p class="twcvrp-hint" style="margin:6px 0;">Presiona <b>Calcular Rutas en el Mapa</b> en el panel izquierdo para trazar las rutas óptimas.</p>';
        renderDraftMarkers();
        return;
      }}

      // Ocultar marcadores de borrador cuando hay rutas solucionadas activas (o mantenerlos sincronizados)
      draftMarkers.forEach(m => m.remove());
      draftMarkers = [];

      const bounds = new maplibregl.LngLatBounds();
      bounds.extend([mapData.depot.lon, mapData.depot.lat]);
      const stopFeatures = [];

      mapData.routes.forEach((route) => {{
        const vid = route.display_id;
        vehicleVisibility[vid] = true;

        const sourceId = `route-source-${{vid}}`;
        const casingLayerId = `route-casing-${{vid}}`;
        const lineLayerId = `route-line-${{vid}}`;

        route.coordinates.forEach(coord => bounds.extend(coord));

        map.addSource(sourceId, {{
          type: 'geojson',
          data: {{
            type: 'Feature',
            properties: {{
              vehicle_id: vid,
              total_time: route.total_time_str,
              total_load: route.total_load
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
          layout: {{ 'line-join': 'round', 'line-cap': 'round' }},
          paint: {{
            'line-color': '#ffffff',
            'line-width': 8,
            'line-opacity': 0.9
          }}
        }});
        activeRouteLayerIds.push(casingLayerId);

        map.addLayer({{
          id: lineLayerId,
          type: 'line',
          source: sourceId,
          layout: {{ 'line-join': 'round', 'line-cap': 'round' }},
          paint: {{
            'line-color': route.color,
            'line-width': 5,
            'line-opacity': 0.95
          }}
        }});
        activeRouteLayerIds.push(lineLayerId);

        map.on('click', lineLayerId, (e) => {{
          new maplibregl.Popup()
            .setLngLat(e.lngLat)
            .setHTML(
              `<b>Vehículo ${{vid}}</b><br>Tiempo total: ${{route.total_time_str}}<br>Carga total: ${{route.total_load}}`
            )
            .addTo(map);
        }});
        map.on('mouseenter', lineLayerId, () => {{ map.getCanvas().style.cursor = 'pointer'; }});
        map.on('mouseleave', lineLayerId, () => {{ map.getCanvas().style.cursor = ''; }});

        // Agregar fila en la leyenda derecha
        const itemEl = document.createElement('div');
        itemEl.className = 'twcvrp-route-item';
        itemEl.setAttribute('data-vehicle-id', vid);
        itemEl.innerHTML = `
          <span class="twcvrp-dot" style="background:${{route.color}}"></span>
          <span>Vehículo ${{vid}}</span>
          <span class="twcvrp-meta">${{route.stops.length}} paradas • ${{route.total_time_str}}</span>
          <button class="twcvrp-eye" data-vehicle-id="${{vid}}" title="Ocultar">
            <i class="fa fa-eye"></i>
          </button>
        `;
        routesContainer.appendChild(itemEl);

        route.stops.forEach((stop) => {{
          bounds.extend([stop.lon, stop.lat]);
          const popupHtml = `
            <b>Vehículo ${{vid}} - Parada ${{stop.sequence}}</b><br>
            <b>${{stop.name}}</b><br>
            Llegada: ${{stop.arrival}}<br>
            Tiempo de servicio: ${{stop.service_time}} min<br>
            Salida: ${{stop.departure}}<br>
            Demanda: ${{stop.demand}}
          `;
          stopFeatures.push({{
            type: 'Feature',
            properties: {{ vid: vid, seq: String(stop.sequence), color: route.color, html: popupHtml }},
            geometry: {{ type: 'Point', coordinates: [stop.lon, stop.lat] }}
          }});
          searchableLocations.push({{
            name: `${{stop.name}} (Vehículo ${{vid}} - Parada ${{stop.sequence}})`,
            lon: stop.lon,
            lat: stop.lat,
            html: popupHtml
          }});
        }});
      }});

      map.addSource('stops-source', {{
        type: 'geojson',
        data: {{ type: 'FeatureCollection', features: stopFeatures }}
      }});
      map.addLayer({{
        id: 'stops-circle',
        type: 'circle',
        source: 'stops-source',
        paint: {{
          'circle-radius': 11,
          'circle-color': '#ffffff',
          'circle-stroke-color': ['get', 'color'],
          'circle-stroke-width': 2.5
        }}
      }});
      map.addLayer({{
        id: 'stops-label',
        type: 'symbol',
        source: 'stops-source',
        layout: {{
          'text-field': ['get', 'seq'],
          'text-font': ['Noto Sans Bold'],
          'text-size': 12,
          'text-allow-overlap': true,
          'text-ignore-placement': true
        }},
        paint: {{ 'text-color': ['get', 'color'] }}
      }});

      map.addSource('depot-source', {{
        type: 'geojson',
        data: {{
          type: 'Feature',
          properties: {{ html: depotHtml }},
          geometry: {{ type: 'Point', coordinates: [mapData.depot.lon, mapData.depot.lat] }}
        }}
      }});
      map.addLayer({{
        id: 'depot-circle',
        type: 'circle',
        source: 'depot-source',
        paint: {{
          'circle-radius': 14,
          'circle-color': '#111111',
          'circle-stroke-color': '#ffffff',
          'circle-stroke-width': 3
        }}
      }});
      map.addLayer({{
        id: 'depot-label',
        type: 'symbol',
        source: 'depot-source',
        layout: {{
          'text-field': 'D',
          'text-font': ['Noto Sans Bold'],
          'text-size': 13,
          'text-allow-overlap': true,
          'text-ignore-placement': true
        }},
        paint: {{ 'text-color': '#ffffff' }}
      }});

      routesContainer.querySelectorAll('.twcvrp-eye[data-vehicle-id]').forEach(btn => {{
        btn.addEventListener('click', function() {{
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

      if (!bounds.isEmpty()) {{
        map.fitBounds(bounds, {{ padding: 90, pitch: is3D ? 55 : 0, bearing: is3D ? -15 : 0, duration: 1200 }});
      }}
    }}

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
      lblSolve.textContent = 'Optimizando rutas con OR-Tools...';
      setStatus('Calculando matriz vial y resolviendo VRP...', '');

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
          renderPointsList();
        }}
        setMapClickMode(false);
        renderSolvedRoutesOnMap(MAP_DATA, REPORT);
        setStatus(result.message, 'success');
      }} catch (err) {{
        setStatus('No se pudo conectar con el servidor local (ejecuta: python app.py).', 'error');
      }} finally {{
        btnSolve.disabled = false;
        lblSolve.textContent = 'Calcular Rutas en el Mapa';
      }}
    }});

    function setStatus(text, type) {{
      const el = document.getElementById('vrp-status-msg');
      el.textContent = text;
      el.className = 'vrp-status-msg' + (type ? ' ' + type : '');
    }}

    // ================= CARGA DEL MAPA 3D =================
    map.on('load', () => {{
      mapLoaded = true;
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

      ['stops-circle', 'depot-circle'].forEach((layerId) => {{
        map.on('click', layerId, (e) => {{
          if (mapClickAddMode) return;
          const f = e.features[0];
          new maplibregl.Popup({{ offset: 14 }})
            .setLngLat(f.geometry.coordinates)
            .setHTML(f.properties.html)
            .addTo(map);
        }});
        map.on('mouseenter', layerId, () => {{ if (!mapClickAddMode) map.getCanvas().style.cursor = 'pointer'; }});
        map.on('mouseleave', layerId, () => {{ if (!mapClickAddMode) map.getCanvas().style.cursor = ''; }});
      }});

      if (MAP_DATA.routes && MAP_DATA.routes.length > 0) {{
        renderSolvedRoutesOnMap(MAP_DATA, REPORT);
      }} else {{
        renderDraftMarkers();
        fitMapToLocations();
      }}
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
      if (map.getLayer('stops-circle')) {{
        const visibleIds = Object.keys(vehicleVisibility)
          .filter(k => vehicleVisibility[k])
          .map(k => parseInt(k, 10));
        const filter = ['in', ['get', 'vid'], ['literal', visibleIds]];
        map.setFilter('stops-circle', filter);
        map.setFilter('stops-label', filter);
      }}
      const btn = document.querySelector(`.twcvrp-eye[data-vehicle-id="${{vid}}"]`);
      if (btn) {{
        btn.innerHTML = visible ? '<i class="fa fa-eye"></i>' : '<i class="fa fa-eye-slash"></i>';
        btn.title = visible ? 'Ocultar' : 'Mostrar';
      }}
    }}

    document.getElementById('twcvrp-eye-all').addEventListener('click', function() {{
      const anyVisible = Object.values(vehicleVisibility).some(v => v);
      const target = !anyVisible;
      Object.keys(vehicleVisibility).forEach(vid => setVehicleVisible(parseInt(vid, 10), target));
      const label = document.getElementById('twcvrp-eye-all-label');
      if (label) label.textContent = target ? 'Ocultar todas' : 'Ver todas';
    }});

    let is3D = true;
    document.getElementById('twcvrp-toggle-pitch').addEventListener('click', function() {{
      is3D = !is3D;
      map.easeTo({{
        pitch: is3D ? 60 : 0,
        bearing: is3D ? -20 : 0,
        duration: 800
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
      legendToggle.style.display = 'block';
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

    // ================= BUSCADOR DE PARADAS CON FLYTO =================
    const searchInput = document.getElementById('twcvrp-search-input');
    const searchResults = document.getElementById('twcvrp-search-results');

    searchInput.addEventListener('input', function() {{
      const q = this.value.trim().toLowerCase();
      searchResults.innerHTML = '';
      if (!q) {{
        searchResults.style.display = 'none';
        return;
      }}
      const pool = searchableLocations.length > 0
        ? searchableLocations
        : LOCATIONS.map((l, idx) => ({{
            name: `${{l.Nombre}} (${{idx === 0 ? 'Depósito' : l.Tipo_Servicio}})`,
            lon: l.Longitud,
            lat: l.Latitud,
            html: `<b>${{l.Nombre}}</b><br>Coords: ${{l.Latitud}}, ${{l.Longitud}}`
          }}));

      const matches = pool.filter(loc => loc.name.toLowerCase().includes(q));
      if (!matches.length) {{
        searchResults.style.display = 'none';
        return;
      }}
      matches.forEach(loc => {{
        const item = document.createElement('div');
        item.className = 'twcvrp-search-item';
        item.textContent = loc.name;
        item.addEventListener('click', () => {{
          searchResults.style.display = 'none';
          searchInput.value = loc.name;
          map.flyTo({{
            center: [loc.lon, loc.lat],
            zoom: 17.5,
            pitch: is3D ? 60 : 0,
            bearing: is3D ? -15 : 0,
            duration: 1400
          }});
          new maplibregl.Popup({{ offset: 14 }})
            .setLngLat([loc.lon, loc.lat])
            .setHTML(loc.html)
            .addTo(map);
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
