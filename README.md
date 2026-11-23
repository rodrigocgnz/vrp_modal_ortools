# Optimizador de Rutas VRP con OR-Tools y Mapa 3D Interactivo

Aplicación de optimización logística para problemas de ruteo de vehículos (**VRP**, **CVRP**, **CVRPTW** con ventanas de tiempo y **PDP** de recogida y entrega) impulsada por **Google OR-Tools**, **FastAPI** y un frontend en **Mapa 3D WebGL** (**MapLibre GL JS** + **OpenFreeMap Vector Tiles** + **OSRM**).

---

## Características Principales

- **Interfaz en Mapa 3D (WebGL)**:
  - Renderizado vectorial con edificios extruidos en 3D, inclinación/rotación de cámara, selector de estilos visuales (*Positron Claro*, *Liberty Calles*, *Bright Detallado*) y puntos acelerados por GPU sin lag.
  - **Clic en cualquier punto (Depósito o Parada)**: Muestra su tarjeta de detalle con horario de llegada/salida, vehículo y secuencia asignada, demanda, coordenadas y acciones de edición/eliminación.
  - **Clic en cualquier línea de ruta**: Muestra el resumen del vehículo, número de paradas, tiempo total, distancia en km, carga transportada, costo de la ruta y secuencia del recorrido.
- **Side Panel `Planificador VRP`** con 3 ventanas integradas:
  1. **Puntos**:
     - **Importar / Exportar CSV** (compatible con selección de archivo o *Drag & Drop* directo sobre el panel).
     - **Colocar puntos haciendo clic en el mapa** con configuración rápida de rol (*Entrega*, *Recogida* o *Mover Depósito*), demanda y ventana de horario.
     - **Formulario manual colapsable (`+ Manual`)** para ingresar o editar coordenadas exactas, horarios, demanda e `ID_Viaje`.
     - **Botón `Demo`** para cargar un escenario de prueba multi-ruta en un clic.
  2. **Opciones**:
     - Configuración de **Vehículos disponibles**, **Capacidad por vehículo**, **Tiempo de servicio (min)** y **Espera máxima (min)**.
     - Selección del **Tipo de problema VRP** (*Automático*, *CVRPTW*, *CVRP*, *PDP*), **Motor de matriz vial** (*OSRM*, *HERE Maps*, *OpenRouteService*) y opción de **Retorno obligatorio al depósito**.
     - Barra en vivo de validación **Demanda total vs. Capacidad de flota**.
  3. **Costos**:
     - Gestión en tiempo real de tarifas operativas: **Costo por km** (combustible y mantenimiento), **Costo por hora de conductor**, **Costo fijo por vehículo** y **Moneda** (`MXN`, `USD`, `EUR`).
     - **Calculadora integrada de combustible**: Calcula automáticamente el costo por kilómetro a partir del precio por litro, rendimiento (`km/L`) y mantenimiento por km.
     - **Resumen financiero en vivo**: Desglose proporcional por rubro (combustible, conductores, costo fijo), indicadores unitarios (**Costo / Parada**, **Costo / Unidad**, **Costo / km**) y costo desglosado por cada vehículo.

---

## Estructura del Proyecto

```text
vrp_modal_ortools/
├── app.py                      # Servidor web FastAPI (UI interactiva + endpoints API)
├── run_solver.py               # Script CLI para ejecutar el optimizador por consola
├── setup_osrm.ps1              # Script PowerShell para levantar OSRM local en Docker
├── docker-compose.yml          # Configuración del contenedor OSRM local
├── requirements.txt            # Dependencias de Python
├── data/
│   ├── data.csv                # Ubicaciones actuales (Depósito y Paradas)
│   └── config.json             # Configuración persistida de flota y costos
├── src/
│   ├── data_loader.py          # Carga y validación del modelo de datos VRP
│   ├── solver.py               # Motor de optimización con Google OR-Tools
│   ├── reporting.py            # Extracción de rutas, métricas de eficiencia y costos
│   ├── visualization.py        # Generador del Mapa 3D WebGL y Side Panel interactivo
│   ├── generate_osrm_matrix.py # Cálculo de matrices de tiempo/distancia con OSRM
│   ├── generate_here_matrix.py # Cálculo de matrices con HERE Maps API
│   └── generate_ors_matrix.py  # Cálculo de matrices con OpenRouteService API
└── results/
    ├── rutas_twcvrp_mapa.html           # Último mapa 3D interactivo generado
    ├── resultados_matrices.json         # Matriz vial precalculada
    └── resultados_rutas_detallados.json # Reporte JSON detallado de rutas y costos
```

---

## Instalación

1. Instala las dependencias de Python:

```bash
pip install -r requirements.txt
```

2. *(Opcional)* Si deseas ejecutar el motor vial **OSRM Local en Docker** en `http://localhost:5000` (si no está activo, la aplicación utiliza automáticamente el servidor OSRM de respaldo):

```powershell
.\setup_osrm.ps1
```

---

## Uso de la Aplicación

### Opción 1: Interfaz Web Interactiva (Recomendado)

Inicia el servidor FastAPI ejecutando:

```bash
python app.py
```

Abre en tu navegador:

```text
http://127.0.0.1:8000
```

Desde la interfaz puedes:
1. Importar tu archivo `.csv` o activar **"Poner puntos haciendo clic en el mapa"**.
2. Ajustar el número de vehículos, capacidades y horarios en la pestaña **Opciones**.
3. Configurar tus tarifas por kilómetro, hora de conductor y costo fijo en la pestaña **Costos**.
4. Presionar **"Calcular Rutas en el Mapa"** para trazar las rutas sobre las calles en 3D y consultar los itinerarios y costos por vehículo.

> También puedes abrir directamente el archivo generado en `results/rutas_twcvrp_mapa.html` en cualquier navegador.

---

### Opción 2: Ejecución por Línea de Comandos (CLI)

Si prefieres ejecutar el flujo clásico desde terminal a partir de `data/data.csv`:

```bash
python run_solver.py --service osrm --force_matrix
```

Opciones disponibles en `run_solver.py`:
- `--service`: Proveedor de matriz vial (`osrm`, `here`, `ors`). Por defecto: `osrm`.
- `--json_path`: Ruta al archivo JSON de matriz (por defecto `results/resultados_matrices.json`).
- `--force_matrix`: Fuerza el recálculo de la matriz de distancias y tiempos antes de optimizar.

---

## Formato del Archivo CSV (`data/data.csv`)

El archivo CSV de entrada (tanto para importar en la UI como en `data/data.csv`) utiliza las siguientes columnas:

```csv
ID,ID_Viaje,Tipo_Servicio,Nombre,Latitud,Longitud,Horario_Inicio,Horario_Fin,Demanda
0,0,NaN,Depósito,20.967073,-89.623739,07:00,23:00,0
1,1,Entrega,Sucursal Caucel,20.998512,-89.701420,08:00,18:00,4
2,2,Entrega,Plaza Altabrisa,21.017840,-89.584310,09:00,21:00,5
```

- **Depósito**: Se identifica con `ID = 0` (primera fila), `ID_Viaje = 0` y `Demanda = 0`.
- **Paradas (`CVRPTW` / `CVRP`)**: Cada parada tiene un `ID_Viaje` único, `Tipo_Servicio` (`Entrega` o `Recogida`), coordenadas (`Latitud`, `Longitud`), ventana de tiempo (`Horario_Inicio`, `Horario_Fin` en formato `HH:MM`) y `Demanda`.
- **Pares Recogida-Entrega (`PDP`)**: Dos filas que comparten el mismo `ID_Viaje` (una con `Tipo_Servicio = Recogida` y otra con `Tipo_Servicio = Entrega`) se vinculan automáticamente como un par origen-destino.

---

## Endpoints Principales de la API (`app.py`)

- `GET /`: Sirve la interfaz del mapa 3D con el planificador lateral.
- `GET /api/state`: Devuelve las ubicaciones actuales (`data/data.csv`), la configuración (`data/config.json`) y el último reporte.
- `POST /api/import-csv`: Valida e importa un archivo `.csv` actualizando `data/data.csv`.
- `GET /api/export-csv`: Descarga las ubicaciones actuales en formato `.csv`.
- `POST /api/solve`: Calcula la matriz vial, ejecuta el solver de Google OR-Tools, genera el reporte financiero y devuelve las geometrías de ruta 3D.
