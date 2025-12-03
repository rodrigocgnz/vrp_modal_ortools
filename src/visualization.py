import folium
from folium.plugins import Search
import requests
import random
import threading
import time
import sys

class _TokenBucketRateLimiter:
    """Thread-safe token bucket rate limiter."""
    def __init__(self, rate_per_second=5.0, burst=5, max_concurrent=4):
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
                if self.rate <= 0:
                    wait_time = 0.05
                else:
                    wait_time = (1.0 - self.tokens) / self.rate
                self._cond.wait(timeout=max(0.0, wait_time))

    def release(self):
        self._semaphore.release()
        with self._cond:
            self._cond.notify_all()

class OSRMClient:
    """Parallel OSRM client for fetching route polylines."""
    def __init__(self, base_url="http://router.project-osrm.org", profile="driving", max_workers=8, timeout=15, max_retries=4):
        self.base_url = base_url.rstrip("/")
        self.profile = profile
        self.max_workers = max(1, int(max_workers))
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self._limiter = _TokenBucketRateLimiter()
        self._session = requests.Session()

    def _fetch_one(self, start_coords, end_coords):
        params = {"overview": "full", "geometries": "geojson"}
        url = f"{self.base_url}/route/v1/{self.profile}/{start_coords[1]},{start_coords[0]};{end_coords[1]},{end_coords[0]}"
        for attempt in range(self.max_retries + 1):
            self._limiter.acquire()
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get('routes'):
                        coords = data['routes'][0].get('geometry', {}).get('coordinates', [])
                        return [[c[1], c[0]] for c in coords] if coords else [start_coords, end_coords]
                return [start_coords, end_coords]
            except requests.RequestException:
                if attempt >= self.max_retries:
                    return [start_coords, end_coords]
            finally:
                self._limiter.release()
            time.sleep(0.5 * (2 ** attempt))
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
                sys.stdout.write(f"\rFetching map routes... {i+1}/{total}")
                sys.stdout.flush()
        sys.stdout.write("\n")
        return results

def create_map_visualization(routes, data, output_file="rutas_twcvrp_mapa.html"):
    """Crea el mapa con las rutas visualizadas."""
    if not routes:
        print("No hay rutas para visualizar.")
        return None
    
    # Calcular el centro del mapa
    all_coords = []
    for ubicacion in data["ubicaciones"]:
        all_coords.append([ubicacion['Latitud'], ubicacion['Longitud']])
    
    center_lat = sum(coord[0] for coord in all_coords) / len(all_coords)
    center_lon = sum(coord[1] for coord in all_coords) / len(all_coords)
    
    # Crear el mapa con estilo limpio
    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=12,
        tiles='CartoDB positron'  # Estilo limpio y minimalista
    )
    
    # Agregar tiles alternativos
    folium.TileLayer(
        tiles='https://stamen-tiles-{s}.a.ssl.fastly.net/toner-lite/{z}/{x}/{y}.png',
        attr='Map tiles by Stamen Design, under CC BY 3.0. Data by OpenStreetMap, under ODbL.',
        name='Stamen Toner Lite',
        control=True
    ).add_to(m)
    
    folium.TileLayer(
        tiles='https://cartodb-basemaps-{s}.global.ssl.fastly.net/light_all/{z}/{x}/{y}.png',
        attr='Map data &copy; <a href="https://www.openstreetmap.org/">OpenStreetMap</a> contributors, &copy; <a href="https://cartodb.com/attributions">CartoDB</a>',
        name='CartoDB Light',
        control=True
    ).add_to(m)

    # Colores para cada vehículo
    colors = [
        '#FF4757',  # Rojo vibrante
        '#3742FA',  # Azul intenso
        '#2ED573',  # Verde esmeralda
        '#FFA502',  # Naranja cálido
        '#FF6348',  # Coral
        '#5F27CD',  # Violeta eléctrico
        '#00D2D3',  # Turquesa brillante
        '#FFD700',  # Amarillo dorado
        '#FF69B4',  # Rosa neón
        '#1E90FF',  # Azul dodger
        '#32CD32',  # Verde lima
        '#8A2BE2',  # Azul violeta
        '#FF8C00',  # Naranja oscuro
        '#00FA9A',  # Verde primavera
        '#8B4513'   # Marrón cuero
    ]
    
    # Agregar marcadores para todas las ubicaciones en un grupo para habilitar búsqueda
    all_locations_group = folium.FeatureGroup(name='Ubicaciones', show=True)
    depot_index = data.get("depot", 0)
    depot_drawn = False
    for i, ubicacion in enumerate(data["ubicaciones"]):
        coords = [ubicacion['Latitud'], ubicacion['Longitud']]
        # Dibuja solo el depósito como marcador visible (una sola vez)
        if (i == depot_index) and not depot_drawn:
            popup_text = f"""
            <b>{ubicacion['Nombre']}</b><br>
            Tipo: Depósito<br>
            Coordenadas: {coords[0]:.6f}, {coords[1]:.6f}
            """
            folium.Marker(
                location=coords,
                popup=folium.Popup(popup_text, max_width=200),
                tooltip=f"{ubicacion['Nombre']} (Depósito)",
                icon=folium.Icon(color='black', icon='home'),
                title=ubicacion['Nombre']
            ).add_to(all_locations_group)
            depot_drawn = True

    # Añadir el grupo de ubicaciones al mapa
    all_locations_group.add_to(m)
    
    # Crear grupos de capas para cada ruta
    route_groups = {}
    
    # Preparar cliente OSRM con parámetros correctos
    osrm_client = OSRMClient(
        max_workers=8,
        timeout=15,
        max_retries=4
    )

    # Agregar rutas
    for route in routes:
        color = colors[route['vehicle_id'] % len(colors)]
        vehicle_id = route['vehicle_id']
        
        # Crear grupo para esta ruta
        display_id = vehicle_id + 1
        route_group = folium.FeatureGroup(name=f"Vehículo {display_id}")
        route_groups[vehicle_id] = route_group
        
        print(f"\nProcesando ruta del vehículo {display_id} con color {color}...")
        
        # Crear polilínea con todas las coordenadas de la ruta
        route_coords = []
        segment_pairs = []
        for i in range(len(route['stops']) - 1):
            start_coords = route['stops'][i]['coordinates']
            end_coords = route['stops'][i + 1]['coordinates']
            segment_pairs.append((start_coords, end_coords))
        
        if segment_pairs:
            segments_coords = osrm_client.fetch_many(segment_pairs, label=f"Vehículo {display_id}")
            for seg_idx, segment_coords in enumerate(segments_coords):
                if not segment_coords:
                    continue
                if not route_coords:
                    route_coords.extend(segment_coords)
                else:
                    route_coords.extend(segment_coords[1:])  # Evitar duplicado del punto de unión
        
        # Crear la polilínea para toda la ruta - INDENTACIÓN CORREGIDA
        if route_coords:
            total_time_hours = f"{route['total_time'] // 60:02d}:{route['total_time'] % 60:02d}"
            
            folium.PolyLine(
                locations=route_coords,
                color=color,
                weight=4,
                opacity=0.8,
                popup=f"Vehículo {display_id}<br>Tiempo total: {total_time_hours}<br>Carga total: {route['total_load']} paquetes"
            ).add_to(route_group)
        
        # Agregar números de secuencia en las paradas
        for i, stop in enumerate(route['stops']):
            # Saltar solo el depósito inicial (i == 0)
            # Si depot_return_required es true, también saltar el depósito final
            # Si depot_return_required es false, mostrar número en la última parada
            if i == 0:
                continue  # Saltar depósito inicial
            if data.get('depot_return_required', True) and i == len(route['stops']) - 1:
                continue  # Saltar depósito final solo si hay retorno al depósito
            
            arrival_time = stop['arrival_time']
            arrival_hours = f"{arrival_time // 60:02d}:{arrival_time % 60:02d}"
            service_time = stop.get('service_time', 0)
            departure_time = stop.get('departure_time', arrival_time)
            departure_hours = f"{departure_time // 60:02d}:{departure_time % 60:02d}"
            
            popup_text = f"""
            <b>Vehículo {display_id} - Parada {i}</b><br>
            <b>{stop['name']}</b><br>
            Llegada: {arrival_hours}<br>
            Tiempo de servicio: {service_time} min<br>
            Salida: {departure_hours}
            """
            
            folium.CircleMarker(
                location=stop['coordinates'],
                radius=10,
                popup=folium.Popup(popup_text, max_width=200),
                color=color,
                fill=True,
                fillColor='#ffffff',
                fillOpacity=0.95,
                weight=2
            ).add_to(route_group)
            
            folium.Marker(
                location=stop['coordinates'],
                icon=folium.DivIcon(
                    html=f"<div style=\"width:18px;height:18px;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:bold;color:{color};pointer-events:auto;cursor:pointer;\">{i}</div>",
                    icon_size=(18, 18),
                    icon_anchor=(9, 9)
                ),
                popup=folium.Popup(popup_text, max_width=200)
            ).add_to(route_group)

            # Marcador invisible solo para búsqueda
            folium.Marker(
                location=stop['coordinates'],
                title=stop['name'],
                icon=folium.DivIcon(html="<div style='opacity:0;width:0;height:0;'></div>", icon_size=(0, 0), icon_anchor=(0, 0))
            ).add_to(all_locations_group)
        
        # Agregar el grupo de la ruta al mapa
        route_group.add_to(m)

    # Añadir control de búsqueda por nombre de parada
    Search(
        layer=all_locations_group,
        search_label='title',
        placeholder='Buscar parada...',
        collapsed=False,
        search_zoom=16,
        marker=False,
        openPopup=False
    ).add_to(m)

    # Control de capas para soportar toggles de la leyenda
    layer_control = folium.LayerControl(position='bottomright')
    layer_control.add_to(m)

    # Generar items HTML para la leyenda
    routes_items_html = ""
    for route in routes:
        color = colors[route['vehicle_id'] % len(colors)]
        total_time_hours = f"{route['total_time'] // 60:02d}:{route['total_time'] % 60:02d}"
        display_id = route['vehicle_id'] + 1
        routes_items_html += (
            f"<div class=\"twcvrp-route-item\" data-vehicle-id=\"{display_id}\">"
            f"<span class=\"twcvrp-dot\" style=\"background:{color}\"></span>"
            f"Vehículo {display_id} "
            f"<span class=\"twcvrp-meta\">({total_time_hours})</span>"
            f"<button class=\"twcvrp-eye\" data-vehicle-id=\"{display_id}\" title=\"Ocultar\"><i class=\"fa fa-eye\"></i></button>"
            f"</div>"
        )

    # Leyenda moderna colapsable con Font Awesome incluido
    legend_html = f"""
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
    <style>
      .twcvrp-legend-panel {{
        position: fixed;
        top: 10px;
        right: -300px;
        width: 280px;
        max-height: calc(100% - 20px);
        overflow-y: auto;
        background: rgba(255,255,255,0.97);
        border-radius: 12px 0 0 12px;
        box-shadow: 0 8px 24px rgba(0,0,0,0.15);
        border: 1px solid rgba(0,0,0,0.08);
        z-index: 9999;
        font-family: Arial, sans-serif;
        transition: right 280ms ease;
        backdrop-filter: blur(2px);
      }}
      .twcvrp-legend-panel.open {{ right: 10px; }}
      .twcvrp-legend-header {{
        display: flex; align-items: center; justify-content: space-between;
        padding: 12px 14px; border-bottom: 1px solid #eee;
      }}
      .twcvrp-legend-title {{ margin: 0; font-size: 15px; font-weight: 700; color: #222; }}
      .twcvrp-legend-close {{
        appearance: none; border: none; background: transparent; cursor: pointer;
        font-size: 18px; line-height: 1; color: #666; padding: 4px 6px; border-radius: 6px;
      }}
      .twcvrp-legend-close:hover {{ background: #f2f2f2; color: #111; }}
      .twcvrp-legend-content {{ padding: 12px 14px; font-size: 13px; color: #333; }}
      .twcvrp-item {{ margin: 4px 0; display: flex; align-items: center; gap: 8px; }}
      .twcvrp-divider {{ margin: 10px 0; border: 0; border-top: 1px solid #eee; }}
      .twcvrp-section-title {{ margin: 0 0 6px 0; font-size: 13px; font-weight: 700; color: #333; }}
      .twcvrp-route-item {{ display: flex; align-items: center; gap: 8px; margin: 6px 0; }}
      .twcvrp-dot {{ width: 12px; height: 12px; border-radius: 50%; display: inline-block; box-shadow: inset 0 0 0 2px #fff, 0 0 0 1px rgba(0,0,0,0.1); }}
      .twcvrp-meta {{ margin-left: auto; color: #666; font-size: 12px; }}
      .twcvrp-route-actions {{ display: flex; justify-content: flex-end; margin-bottom: 6px; }}
      .twcvrp-eye {{ appearance: none; border: none; background: transparent; cursor: pointer; color: #444; padding: 4px 6px; border-radius: 6px; }}
      .twcvrp-eye:hover {{ background: #f2f2f2; color: #111; }}
      .twcvrp-hint {{ margin: 0; color: #666; font-size: 11px; }}
      .twcvrp-legend-toggle {{
        position: fixed; top: 22px; right: 10px; transform: none;
        z-index: 10000; cursor: pointer;
        background: #ffffff; border: 1px solid rgba(0,0,0,0.08); color: #222;
        padding: 10px 12px; border-radius: 10px; box-shadow: 0 8px 24px rgba(0,0,0,0.15);
        font-size: 14px; min-width: 36px;
      }}
      .twcvrp-legend-toggle:hover {{ background: #f8f8f8; }}
      
      .leaflet-control-layers {{ display: none !important; }}

      .leaflet-control {{
        font-family: Arial, sans-serif;
      }}
      
      .leaflet-control-zoom.leaflet-bar {{
        background: #ffffff;
        border: 1px solid rgba(0,0,0,0.08);
        border-radius: 10px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.15);
        overflow: hidden;
        margin: 0 10px 14px 0;
      }}
      
      .leaflet-control-zoom.leaflet-bar a {{
        width: 28px; height: 28px; line-height: 28px;
        background: #ffffff; color: #222;
        border: none;
        font-size: 16px;
      }}
      
      .leaflet-control-zoom.leaflet-bar a.leaflet-control-zoom-in {{ border-bottom: 1px solid #eee; }}
      .leaflet-control-zoom.leaflet-bar a.leaflet-control-zoom-out {{ border-bottom: 0; }}
      .leaflet-control-zoom.leaflet-bar a:hover {{ background: #f6f6f6; color: #111; }}
      .leaflet-control-zoom.leaflet-bar a.leaflet-disabled {{ opacity: 0.45; cursor: not-allowed; }}
      
      .leaflet-bottom.leaflet-right {{ padding: 0 10px 0 0; }}
      .leaflet-right .leaflet-control {{ margin: 0 10px 14px 0; }}
      .leaflet-bottom .leaflet-control-attribution {{ margin: 0 8px 0 0 !important; }}

      .leaflet-control-search {{
        background: #ffffff;
        border: 1px solid rgba(0,0,0,0.08);
        border-radius: 12px;
        box-shadow: 0 8px 24px rgba(0,0,0,0.15);
        padding: 6px 8px;
      }}
      
      .leaflet-control-search .search-input {{
        background: transparent;
        border: none;
        outline: none;
        padding: 6px 8px;
        font-size: 14px;
        min-width: 180px;
        color: #333;
      }}
      
      .leaflet-control-search .search-input::placeholder {{ color: #999; }}
      
      .leaflet-control-search .search-button {{
        background: transparent;
        border: none;
        width: 32px; height: 32px;
        color: #444; border-radius: 8px;
        cursor: pointer;
      }}
      
      .leaflet-control-search .search-button:hover {{ background: #f2f2f2; color: #111; }}
      .leaflet-control-search .search-cancel {{ color: #888; right: 8px; top: 8px; }}
      
      .leaflet-control-search .search-tooltip {{
        background: #ffffff; border: 1px solid rgba(0,0,0,0.08);
        border-radius: 10px; box-shadow: 0 8px 24px rgba(0,0,0,0.15);
        padding: 6px 0;
      }}
      
      .leaflet-control-search .search-tooltip li {{
        padding: 8px 12px; border-bottom: 1px solid #f0f0f0;
      }}
      
      .leaflet-control-search .search-tooltip li:last-child {{ border-bottom: none; }}
      
      .twcvrp-search-container {{
        position: absolute;
        top: 10px;
        left: 50%;
        transform: translateX(-50%);
        z-index: 10000;
        pointer-events: none;
        width: 100%;
      }}
      
      .twcvrp-search-container .leaflet-control-search {{
        pointer-events: auto;
        margin: 0 auto;
        width: 420px;
      }}
      
      .twcvrp-search-container .leaflet-control-search .search-input {{
        min-width: 360px;
        width: 360px;
      }}
      
      @media (max-width: 600px) {{
        .twcvrp-legend-panel {{ width: 85vw; right: -90vw; }}
        .twcvrp-legend-panel.open {{ right: 8px; }}
        .twcvrp-legend-toggle {{ top: auto; bottom: 14px; transform: none; }}
        .twcvrp-search-container .leaflet-control-search {{ width: 86vw; }}
        .twcvrp-search-container .leaflet-control-search .search-input {{
          min-width: calc(86vw - 60px);
          width: calc(86vw - 60px);
        }}
      }}
    </style>
    <div id="twcvrp-legend" class="twcvrp-legend-panel">
      <div class="twcvrp-legend-header">
        <h4 class="twcvrp-legend-title">Leyenda</h4>
        <button id="twcvrp-legend-header-toggle" class="twcvrp-legend-close" title="Ocultar">×</button>
      </div>
      <div class="twcvrp-legend-content">
        <div class="twcvrp-item"><i class="fa fa-home" style="color:black;"></i> Depósito</div>
        <div class="twcvrp-item"><i class="fa fa-store" style="color:green;"></i> Sucursal</div>
        <div class="twcvrp-item"><i class="fa fa-wrench" style="color:orange;"></i> Proveedor</div>
        <hr class="twcvrp-divider" />
        <div class="twcvrp-section-title">Rutas por vehículo</div>
        <div class="twcvrp-route-actions">
          <button id="twcvrp-eye-all" class="twcvrp-eye" title="Ocultar todas">
            <i class="fa fa-eye"></i>
            <span class="twcvrp-eye-label">Ocultar todas</span>
          </button>
        </div>
        {routes_items_html}
        <hr class="twcvrp-divider" />
        <p class="twcvrp-hint">💡 Usa los íconos de ojo para mostrar/ocultar rutas o el botón global</p>
      </div>
    </div>
    <button id="twcvrp-legend-toggle" class="twcvrp-legend-toggle" title="Mostrar/Ocultar">☰</button>
    
    <script>
      (function() {{
        var panel = document.getElementById('twcvrp-legend');
        var toggle = document.getElementById('twcvrp-legend-toggle');
        var headerToggle = document.getElementById('twcvrp-legend-header-toggle');
        var storageKey = 'twcvrp_legend_open';
        
        function setOpen(isOpen) {{
          if (isOpen) {{
            panel.classList.add('open');
            if (toggle) {{ toggle.textContent = '×'; toggle.style.display = 'none'; }}
            if (headerToggle) {{ headerToggle.textContent = '×'; }}
          }} else {{
            panel.classList.remove('open');
            if (toggle) {{ toggle.textContent = '☰'; toggle.style.display = 'block'; }}
            if (headerToggle) {{ headerToggle.textContent = '×'; }}
          }}
          try {{ localStorage.setItem(storageKey, isOpen ? '1' : '0'); }} catch (e) {{}}
        }}
        
        var saved = null;
        try {{ saved = localStorage.getItem(storageKey); }} catch (e) {{ saved = null; }}
        setOpen(saved === null ? true : saved === '1');
        
        if (toggle) {{ toggle.addEventListener('click', function() {{ setOpen(!panel.classList.contains('open')); }}); }}
        if (headerToggle) {{ headerToggle.addEventListener('click', function() {{ setOpen(false); }}); }}
      }})();
    </script>
    
    <script>
      // Script para mover el control de zoom a la esquina inferior derecha
      (function() {{
        function findLeafletMap() {{
          for (var key in window) {{
            try {{
              var val = window[key];
              if (key.indexOf('map_') === 0 && val && typeof val.on === 'function' && typeof val.eachLayer === 'function') {{
                return val;
              }}
            }} catch (e) {{}}
          }}
          return null;
        }}
        
        var attempts = 0;
        var maxAttempts = 60;
        var timer = setInterval(function() {{
          var map = findLeafletMap();
          if (!map || typeof L === 'undefined') {{
            attempts++;
            if (attempts >= maxAttempts) {{ clearInterval(timer); }}
            return;
          }}
          try {{
            var container = map.getContainer && map.getContainer();
            if (container) {{
              container.querySelectorAll('.leaflet-control-zoom').forEach(function(el) {{
                if (el && el.parentNode) {{ el.parentNode.removeChild(el); }}
              }});
            }}
            if (map.zoomControl && typeof map.zoomControl.remove === 'function') {{
              map.zoomControl.remove();
            }}
            L.control.zoom({{ position: 'bottomright' }}).addTo(map);
            clearInterval(timer);
          }} catch (e) {{
            attempts++;
            if (attempts >= maxAttempts) {{ clearInterval(timer); }}
          }}
        }}, 200);
      }})();
    </script>
    
    <script>
      // Script para centrar la barra de búsqueda
      (function() {{
        function findLeafletMap() {{
          for (var key in window) {{
            try {{
              var val = window[key];
              if (key.indexOf('map_') === 0 && val && typeof val.on === 'function' && typeof val.eachLayer === 'function') {{
                return val;
              }}
            }} catch (e) {{}}
          }}
          return null;
        }}
        
        var map = findLeafletMap();
        if (!map) return;
        var mapContainer = map.getContainer ? map.getContainer() : null;
        if (!mapContainer) return;
        
        var attempts = 0;
        var maxAttempts = 60;
        var interval = setInterval(function() {{
          var searchEl = mapContainer.querySelector('.leaflet-control-search');
          if (searchEl) {{
            clearInterval(interval);
            var wrapper = mapContainer.querySelector('#twcvrp-search-container');
            if (!wrapper) {{
              wrapper = document.createElement('div');
              wrapper.id = 'twcvrp-search-container';
              wrapper.className = 'twcvrp-search-container';
              mapContainer.appendChild(wrapper);
            }}
            wrapper.appendChild(searchEl);
          }} else {{
            attempts++;
            if (attempts >= maxAttempts) {{ clearInterval(interval); }}
          }}
        }}, 200);
      }})();
    </script>
    
    <script>
      // Script para manejar la visibilidad de rutas
      (function() {{
        function normalizeText(str) {{
          try {{
            return (str || '')
              .normalize('NFD')
              .replace(/[\u0300-\u036f]/g, '')
              .replace(/\s+/g, ' ')
              .trim()
              .toLowerCase();
          }} catch (e) {{
            return (str || '').toLowerCase();
          }}
        }}
      function buildOverlayMap() {{
          var idToCheckbox = {{}};
          var container = document.querySelector('.leaflet-control-layers-overlays');
          if (!container) return idToCheckbox;
          var labels = container.querySelectorAll('label');
          labels.forEach(function(label) {{
            var text = normalizeText(label.textContent || '');
            var input = label.querySelector('input[type="checkbox"]');
            if (!input) return;
            // Ensure the label is for vehicle overlays. Accept both 'Vehiculo' and 'Vehículo'
            if (text.indexOf('vehiculo') === -1) return;
            // Extract the last number in the label text as vehicle id
             var m = (label.textContent || '').match(/(\d+)(?!.*\d)/);
            if (!m) return;
            var id = parseInt(m[1], 10);
            if (!isNaN(id)) {{ idToCheckbox[id] = input; }}
          }});
          return idToCheckbox;
        }}

        function updateButtonState(id, visible) {{
          var btn = document.querySelector('.twcvrp-eye[data-vehicle-id="' + id + '"]');
          if (!btn) return;
          btn.innerHTML = visible ? '<i class="fa fa-eye"></i>' : '<i class="fa fa-eye-slash"></i>';
          btn.title = visible ? 'Ocultar' : 'Mostrar';
          btn.setAttribute('data-visible', visible ? '1' : '0');
        }}

        function attachHandlers(overlays) {{
          var stateKey = 'twcvrp_route_visibility';
          var saved = {{}};
          try {{ saved = JSON.parse(localStorage.getItem(stateKey) || '{{}}'); }} catch (e) {{ saved = {{}}; }}

           // Initialize visibility from saved state
          Object.keys(overlays).forEach(function(idStr) {{
            var id = parseInt(idStr, 10);
            var cb = overlays[id];
            if (!cb) return;
            var desiredVisible = saved[idStr] !== false; // default visible
            if (cb.checked !== desiredVisible) {{
              // Use programmatic click to let Leaflet handle layer add/remove
              cb.click();
            }}
            updateButtonState(id, desiredVisible);
          }});

           // Per-vehicle buttons
          document.querySelectorAll('.twcvrp-eye[data-vehicle-id]').forEach(function(btn) {{
            btn.addEventListener('click', function() {{
              var id = parseInt(this.getAttribute('data-vehicle-id'), 10);
              var cb = overlays[id];
              if (!cb) return;
              var newVisible = !cb.checked; // checked means currently visible
              cb.click();
              updateButtonState(id, newVisible);
              saved[id] = newVisible;
              try {{ localStorage.setItem(stateKey, JSON.stringify(saved)); }} catch (e) {{}}
            }});
          }});

          // Global button
          var allBtn = document.getElementById('twcvrp-eye-all');
          var allBtnLabel = allBtn ? allBtn.querySelector('.twcvrp-eye-label') : null;
          if (allBtn) {{
            allBtn.addEventListener('click', function() {{
              var anyVisible = Object.values(overlays).some(function(cb) {{ return cb.checked; }});
              var targetVisible = !anyVisible;
               Object.keys(overlays).forEach(function(idStr) {{
                 var id = parseInt(idStr, 10);
                 var cb = overlays[id];
                if (!cb) return;
                if (cb.checked !== targetVisible) {{ cb.click(); }}
                updateButtonState(id, targetVisible);
                saved[idStr] = targetVisible;
              }});
              try {{ localStorage.setItem(stateKey, JSON.stringify(saved)); }} catch (e) {{}}
              this.innerHTML = targetVisible ? '<i class="fa fa-eye"></i>' : '<i class="fa fa-eye-slash"></i>';
              // Re-append label span after swapping innerHTML
              if (allBtnLabel) {{
                var iconHtml = this.innerHTML;
                this.innerHTML = iconHtml + ' <span class="twcvrp-eye-label"></span>';
                var lbl = this.querySelector('.twcvrp-eye-label');
                if (lbl) {{ lbl.textContent = targetVisible ? 'Ocultar todas' : 'Ver todas'; }}
              }}
              this.title = targetVisible ? 'Ocultar todas' : 'Ver todas';
            }});
          }}
        }}

        function initWhenReady() {{
          var attempts = 0;
          var maxAttempts = 60; // ~12s max
          var timer = setInterval(function() {{
            var container = document.querySelector('.leaflet-control-layers-overlays');
            if (container && container.querySelector('label input[type="checkbox"]')) {{
              clearInterval(timer);
              var overlays = buildOverlayMap();
              // If still empty, don't attach to avoid errors
              if (Object.keys(overlays).length) {{ attachHandlers(overlays); }}
              return;
            }}
            attempts++;
            if (attempts >= maxAttempts) {{ clearInterval(timer); }}
          }}, 200);
        }}

        // Initialize and also set initial text for global button label
        initWhenReady();
        (function initGlobalLabel() {{
          var btn = document.getElementById('twcvrp-eye-all');
          if (!btn) return;
          var label = btn.querySelector('.twcvrp-eye-label');
          if (label) {{ label.textContent = 'Ocultar todas'; }}
        }})();
      }})();
    </script>
    <script>
      (function() {{
        function findLeafletMap() {{
          for (var key in window) {{
            try {{
              var val = window[key];
              if (key.indexOf('map_') === 0 && val && typeof val.on === 'function' && typeof val.eachLayer === 'function') {{
                return val;
              }}
            }} catch (e) {{}}
          }}
          return null;
        }}
        var map = findLeafletMap();
        if (!map || typeof L === 'undefined' || !L.CircleMarker) return;
        map.on('search:locationfound', function() {{
          var toRemove = [];
          map.eachLayer(function(layer) {{
            if (layer instanceof L.CircleMarker && layer.options && (layer.options.color === '#e03' || layer.options.fillColor === '#f03')) {{
              toRemove.push(layer);
            }}
          }});
          toRemove.forEach(function(layer) {{ try {{ map.removeLayer(layer); }} catch (e) {{}} }});
        }});
      }})();
    </script>
    """
    
    m.get_root().html.add_child(folium.Element(legend_html))

    # Guardar mapa
    try:
        m.save(output_file)
        print(f"\nMapa guardado como '{output_file}'")
        return m
    except Exception as e:
        print(f"Error al guardar el mapa: {e}")
        return None