import json
import logging
import os
import sys
import threading
import time
import requests

DEFAULT_OSRM_BASE_URL = os.getenv("OSRM_BASE_URL", "http://localhost:5000")


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
    """OSRM client for fetching street-level route polylines from local OSRM in Docker."""

    def __init__(self, base_url=None, profile="driving", max_workers=8, timeout=15, max_retries=2):
        resolved_url = base_url or DEFAULT_OSRM_BASE_URL
        self.base_url = resolved_url.rstrip("/")
        self.profile = profile
        self.max_workers = max(1, int(max_workers))
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self._limiter = _TokenBucketRateLimiter(rate_per_second=50.0, burst=50, max_concurrent=max_workers)
        self._session = requests.Session()
        self._auto_start_attempted = False

    def _ensure_local_osrm_running(self):
        """Si apunta a localhost y OSRM no responde, ejecuta setup_osrm.ps1 automáticamente."""
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
        en una sola llamada a /route/v1/{profile}/ en OSRM local.
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
        url = f"{self.base_url}/route/v1/{self.profile}/{coords_str}"

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
                logging.warning(
                    f"OSRM route request devolvió status {resp.status_code}. Intentando por segmentos..."
                )
                break
            except requests.RequestException as e:
                if not self._auto_start_attempted:
                    self._limiter.release()
                    if self._ensure_local_osrm_running():
                        continue
                    self._limiter.acquire()
                if attempt >= self.max_retries:
                    logging.warning(
                        f"No se pudo conectar a OSRM en '{self.base_url}' ({e}). "
                        "Se intentará por segmentos o línea recta de respaldo."
                    )
            finally:
                try:
                    self._limiter.release()
                except ValueError:
                    pass
            time.sleep(0.25 * (2 ** attempt))

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
        url = f"{self.base_url}/route/v1/{self.profile}/{start_coords[1]},{start_coords[0]};{end_coords[1]},{end_coords[0]}"
        for attempt in range(self.max_retries + 1):
            self._limiter.acquire()
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("routes"):
                        coords = data["routes"][0].get("geometry", {}).get("coordinates", [])
                        return [[c[1], c[0]] for c in coords] if coords else [start_coords, end_coords]
                return [start_coords, end_coords]
            except requests.RequestException as e:
                if attempt >= self.max_retries:
                    logging.warning(
                        f"Fallo al obtener geometría de OSRM ({self.base_url}) para tramo "
                        f"{start_coords} -> {end_coords}: {e}. Usando línea recta de respaldo."
                    )
                    return [start_coords, end_coords]
            finally:
                self._limiter.release()
            time.sleep(0.25 * (2 ** attempt))
        return [start_coords, end_coords]

    def fetch_many(self, pairs, label=None):
        from concurrent.futures import ThreadPoolExecutor, as_completed

        total = len(pairs)
        results = [None] * total
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_idx = {executor.submit(self._fetch_one, a, b): i for i, (a, b) in enumerate(pairs)}
            for i, future in enumerate(as_completed(future_to_idx)):
                idx = future_to_idx[future]
                results[idx] = future.result()
                sys.stdout.write(f"\rFetching map routes... {i + 1}/{total}")
                sys.stdout.flush()
        sys.stdout.write("\n")
        return results


def create_map_visualization(routes, data, output_file="rutas_twcvrp_mapa.html"):
    """
    Crea un mapa interactivo en 3D (WebGL con MapLibre GL JS + OpenFreeMap Vector Tiles)
    con edificios extruidos en 3D, inclinación/rotación de cámara, zoom vectorial hasta nivel 22
    y trazado de rutas sobre las calles reales calculadas por OSRM local.
    """
    if not routes:
        print("No hay rutas para visualizar.")
        return None

    all_coords = [
        [float(u["Latitud"]), float(u["Longitud"])]
        for u in data["ubicaciones"]
    ]
    center_lat = sum(c[0] for c in all_coords) / len(all_coords)
    center_lon = sum(c[1] for c in all_coords) / len(all_coords)

    colors = [
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

    depot_index = data.get("depot", 0)
    depot_ubicacion = data["ubicaciones"][depot_index]
    depot_info = {
        "name": str(depot_ubicacion.get("Nombre", "Depósito")),
        "lat": float(depot_ubicacion["Latitud"]),
        "lon": float(depot_ubicacion["Longitud"]),
    }

    osrm_client = OSRMClient(max_workers=8, timeout=15, max_retries=2)

    routes_payload = []
    depot_return_required = data.get("depot_return_required", True)

    for route in routes:
        vehicle_id = route["vehicle_id"]
        display_id = vehicle_id + 1
        color = colors[vehicle_id % len(colors)]

        print(f"\nProcesando ruta 3D del vehículo {display_id} con color {color}...")
        stops_coords = [stop["coordinates"] for stop in route["stops"]]
        route_latlon = osrm_client.fetch_full_route(stops_coords) if len(stops_coords) >= 2 else []
        # MapLibre / GeoJSON usa [lon, lat]
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

    map_data_json = json.dumps(
        {
            "center": [center_lon, center_lat],
            "depot": depot_info,
            "routes": routes_payload,
        },
        ensure_ascii=False,
    )

    routes_items_html = ""
    for r in routes_payload:
        routes_items_html += (
            f'<div class="twcvrp-route-item" data-vehicle-id="{r["display_id"]}">'
            f'<span class="twcvrp-dot" style="background:{r["color"]}"></span>'
            f'Vehículo {r["display_id"]} '
            f'<span class="twcvrp-meta">({r["total_time_str"]})</span>'
            f'<button class="twcvrp-eye" data-vehicle-id="{r["display_id"]}" title="Ocultar">'
            f'<i class="fa fa-eye"></i></button>'
            f"</div>"
        )

    html_content = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Mapa 3D de Rutas VRP</title>
  <link rel="stylesheet" href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" />
  <script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" />
  <style>
    html, body {{
      margin: 0;
      padding: 0;
      width: 100%;
      height: 100%;
      overflow: hidden;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    }}
    #map {{
      position: absolute;
      top: 0;
      bottom: 0;
      left: 0;
      right: 0;
      width: 100%;
      height: 100%;
    }}

    /* Barra de búsqueda centrada */
    .twcvrp-search-bar {{
      position: fixed;
      top: 14px;
      left: 50%;
      transform: translateX(-50%);
      z-index: 10000;
      width: 380px;
      max-width: 85vw;
      background: rgba(255, 255, 255, 0.96);
      border-radius: 12px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.14);
      border: 1px solid rgba(0, 0, 0, 0.08);
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
      font-size: 14px;
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
      font-size: 13px;
      color: #333;
      cursor: pointer;
      border-bottom: 1px solid #f2f2f2;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .twcvrp-search-item:last-child {{ border-bottom: none; }}
    .twcvrp-search-item:hover {{ background: #f5f7fa; }}

    /* Panel de leyenda */
    .twcvrp-legend-panel {{
      position: fixed;
      top: 14px;
      right: -320px;
      width: 285px;
      max-height: calc(100% - 28px);
      overflow-y: auto;
      background: rgba(255, 255, 255, 0.96);
      border-radius: 12px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.16);
      border: 1px solid rgba(0, 0, 0, 0.08);
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
    .twcvrp-legend-title {{ margin: 0; font-size: 15px; font-weight: 700; color: #222; }}
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
    .twcvrp-item {{ margin: 5px 0; display: flex; align-items: center; gap: 8px; }}
    .twcvrp-divider {{ margin: 10px 0; border: 0; border-top: 1px solid #eee; }}
    .twcvrp-section-title {{ margin: 0 0 6px 0; font-size: 13px; font-weight: 700; color: #333; }}
    .twcvrp-route-item {{ display: flex; align-items: center; gap: 8px; margin: 6px 0; }}
    .twcvrp-dot {{
      width: 12px;
      height: 12px;
      border-radius: 50%;
      display: inline-block;
      box-shadow: inset 0 0 0 2px #fff, 0 0 0 1px rgba(0,0,0,0.15);
    }}
    .twcvrp-meta {{ margin-left: auto; color: #666; font-size: 12px; }}
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
      font-size: 14px;
      display: none;
    }}

    /* Marcadores 3D */
    .depot-marker {{
      width: 32px;
      height: 32px;
      border-radius: 50%;
      background: #111;
      color: #fff;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 15px;
      border: 2.5px solid #fff;
      box-shadow: 0 4px 12px rgba(0,0,0,0.35);
      cursor: pointer;
    }}
    .stop-marker {{
      width: 24px;
      height: 24px;
      border-radius: 50%;
      background: #ffffff;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 11px;
      font-weight: 800;
      box-shadow: 0 3px 8px rgba(0,0,0,0.28);
      cursor: pointer;
      transition: transform 0.15s ease;
    }}
    .stop-marker:hover {{
      transform: scale(1.2);
    }}
    .maplibregl-popup-content {{
      border-radius: 10px;
      padding: 12px 14px;
      box-shadow: 0 6px 20px rgba(0,0,0,0.18);
      font-size: 13px;
      line-height: 1.45;
    }}
  </style>
</head>
<body>
  <div id="map"></div>

  <!-- Barra de búsqueda -->
  <div class="twcvrp-search-bar">
    <i class="fa fa-search"></i>
    <input id="twcvrp-search-input" class="twcvrp-search-input" type="text" placeholder="Buscar parada o depósito..." autocomplete="off" />
    <div id="twcvrp-search-results" class="twcvrp-search-results"></div>
  </div>

  <!-- Leyenda y controles 3D -->
  <div id="twcvrp-legend" class="twcvrp-legend-panel open">
    <div class="twcvrp-legend-header">
      <h4 class="twcvrp-legend-title">Leyenda y Vista 3D</h4>
      <button id="twcvrp-legend-close" class="twcvrp-legend-close" title="Ocultar">×</button>
    </div>
    <div class="twcvrp-legend-content">
      <div class="twcvrp-item"><i class="fa fa-home" style="color:#111;"></i> Depósito</div>
      <div class="twcvrp-item"><i class="fa fa-circle" style="color:#FF6348;"></i> Parada numerada</div>
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
      {routes_items_html}
      <hr class="twcvrp-divider" />
      <p class="twcvrp-hint">
        💡 <b>Controles 3D:</b> Arrastra con <b>clic derecho</b> (o <b>Ctrl + clic izquierdo</b>) para inclinar y girar la cámara en 3D.
      </p>
    </div>
  </div>
  <button id="twcvrp-legend-toggle" class="twcvrp-legend-toggle" title="Mostrar leyenda">☰</button>

  <script>
    const MAP_DATA = {map_data_json};

    // Inicializar mapa vectorial 3D con OpenFreeMap Positron (minimalista, sin API key, zoom hasta 22)
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

    // Controles de zoom, brújula 3D e inclinación en la esquina inferior derecha
    map.addControl(new maplibregl.NavigationControl({{
      visualizePitch: true,
      showZoom: true,
      showCompass: true
    }}), 'bottom-right');

    const vehicleVisibility = {{}};
    const vehicleMarkers = {{}};
    const searchableLocations = [];

    // Marcador del depósito
    // El depósito y las paradas se dibujan como capas nativas WebGL (sin marcadores DOM => sin lag)
    const depotHtml =
      `<b>${{MAP_DATA.depot.name}}</b><br>Tipo: Depósito<br>Coords: ${{MAP_DATA.depot.lat.toFixed(6)}}, ${{MAP_DATA.depot.lon.toFixed(6)}}`;

    searchableLocations.push({{
      name: MAP_DATA.depot.name + ' (Depósito)',
      lon: MAP_DATA.depot.lon,
      lat: MAP_DATA.depot.lat,
      html: depotHtml
    }});

    const stopFeatures = [];

    map.on('load', () => {{
      // 1. Agregar capa de edificios en 3D extruidos (fill-extrusion) desde la fuente vectorial openmaptiles
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

      // 2. Dibujar cada ruta y sus paradas
      const bounds = new maplibregl.LngLatBounds();
      bounds.extend([MAP_DATA.depot.lon, MAP_DATA.depot.lat]);

      MAP_DATA.routes.forEach((route) => {{
        const vid = route.display_id;
        vehicleVisibility[vid] = true;
        vehicleMarkers[vid] = [];

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

        // Borde blanco para dar contraste 3D sobre calles y edificios
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

        // Línea principal del vehículo
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

        // Popup al hacer clic sobre la línea de la ruta
        map.on('click', lineLayerId, (e) => {{
          new maplibregl.Popup()
            .setLngLat(e.lngLat)
            .setHTML(
              `<b>Vehículo ${{vid}}</b><br>Tiempo total: ${{route.total_time_str}}<br>Carga total: ${{route.total_load}} paquetes`
            )
            .addTo(map);
        }});
        map.on('mouseenter', lineLayerId, () => {{ map.getCanvas().style.cursor = 'pointer'; }});
        map.on('mouseleave', lineLayerId, () => {{ map.getCanvas().style.cursor = ''; }});

        // Marcadores numerados de paradas
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

      // Paradas: un solo source GeoJSON + capas circle/symbol (renderizadas por la GPU)
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

      // Depósito
      map.addSource('depot-source', {{
        type: 'geojson',
        data: {{
          type: 'Feature',
          properties: {{ html: depotHtml }},
          geometry: {{ type: 'Point', coordinates: [MAP_DATA.depot.lon, MAP_DATA.depot.lat] }}
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

      ['stops-circle', 'depot-circle'].forEach((layerId) => {{
        map.on('click', layerId, (e) => {{
          const f = e.features[0];
          new maplibregl.Popup({{ offset: 14 }})
            .setLngLat(f.geometry.coordinates)
            .setHTML(f.properties.html)
            .addTo(map);
        }});
        map.on('mouseenter', layerId, () => {{ map.getCanvas().style.cursor = 'pointer'; }});
        map.on('mouseleave', layerId, () => {{ map.getCanvas().style.cursor = ''; }});
      }});

      if (!bounds.isEmpty()) {{
        map.fitBounds(bounds, {{ padding: 80, pitch: 55, bearing: -15, duration: 1200 }});
      }}
    }});

    // Mostrar / ocultar ruta de un vehículo
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

    document.querySelectorAll('.twcvrp-eye[data-vehicle-id]').forEach(btn => {{
      btn.addEventListener('click', function() {{
        const vid = parseInt(this.getAttribute('data-vehicle-id'), 10);
        setVehicleVisible(vid, !vehicleVisibility[vid]);
      }});
    }});

    document.getElementById('twcvrp-eye-all').addEventListener('click', function() {{
      const anyVisible = Object.values(vehicleVisibility).some(v => v);
      const target = !anyVisible;
      Object.keys(vehicleVisibility).forEach(vid => setVehicleVisible(parseInt(vid, 10), target));
      const label = document.getElementById('twcvrp-eye-all-label');
      if (label) label.textContent = target ? 'Ocultar todas' : 'Ver todas';
    }});

    // Alternar cámara 3D / 2D
    let is3D = true;
    document.getElementById('twcvrp-toggle-pitch').addEventListener('click', function() {{
      is3D = !is3D;
      map.easeTo({{
        pitch: is3D ? 60 : 0,
        bearing: is3D ? -20 : 0,
        duration: 800
      }});
    }});

    // Colapsar / expandir panel de leyenda
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

    // Buscador de paradas con vuelo 3D (flyTo)
    const searchInput = document.getElementById('twcvrp-search-input');
    const searchResults = document.getElementById('twcvrp-search-results');

    searchInput.addEventListener('input', function() {{
      const q = this.value.trim().toLowerCase();
      searchResults.innerHTML = '';
      if (!q) {{
        searchResults.style.display = 'none';
        return;
      }}
      const matches = searchableLocations.filter(loc => loc.name.toLowerCase().includes(q));
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
            pitch: 60,
            bearing: -15,
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
