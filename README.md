
# Proyecto de Optimización de Rutas VRP con OR-Tools

Este proyecto utiliza Google OR-Tools para resolver problemas de ruteo de vehículos (VRP, CVRP, TWCWRP) y visualiza las rutas óptimas en un mapa interactivo.

La estructura del proyecto ha sido refactorizada para ser modular y escalable.

## Estructura del Proyecto

```
ORTOOLS/
├── scripts/                # Scripts para generar matrices de distancia/tiempo
│   ├── generate_here_matrix.py
│   └── generate_ors_matrix.py
├── src/                    # Código fuente principal de la aplicación
│   ├── data_loader.py      # Carga y valida los datos del problema
│   ├── solver.py           # Contiene la lógica del optimizador VRP
│   ├── reporting.py        # Genera reportes JSON detallados
│   └── visualization.py    # Crea el mapa HTML con Folium
├── data/                   # Datos de entrada (CSV)
│   └── data.csv
├── results/                # Archivos de salida generados
├── tests/                  # Futuras pruebas unitarias
├── run_solver.py           # Script principal para ejecutar el optimizador
├── requirements.txt        # Dependencias de Python
└── README.md               # Este archivo
```

## ¿Cómo Usarlo?

El proceso se divide en dos pasos principales:

### Paso 1: Generar la Matriz de Distancias

Primero, necesitas calcular las distancias y tiempos de viaje entre todas las ubicaciones de tu archivo `data/data.csv`. Puedes usar uno de los dos proveedores de API disponibles.

**Opción A: Usando HERE Maps (Recomendado)**

Ejecuta el siguiente script. Te pedirá que elijas entre un cálculo síncrono o asíncrono.

```bash
python scripts/generate_here_matrix.py
```

**Opción B: Usando OpenRouteService**

```bash
python scripts/generate_ors_matrix.py
```

Ambos scripts leerán `data/data.csv` y guardarán un archivo de matriz en el directorio `results/` (ej. `resultados_matrices_here.json`).

**IMPORTANTE:** Asegúrate de poner tus claves de API dentro de cada script.

### Paso 2: Ejecutar el Optimizador

Una vez que tengas el archivo de matriz, puedes ejecutar el optimizador. Por defecto, buscará el archivo generado por HERE.

```bash
python run_solver.py
```

Si usaste OpenRouteService o tienes el archivo en otra ruta, puedes especificarlo con el argumento `--json_path`:

```bash
python run_solver.py --json_path results/resultados_matrices.json
```

### Resultados

Después de la ejecución, encontrarás dos archivos nuevos:

1.  `rutas_twcvrp_mapa.html`: Un mapa interactivo con las rutas de los vehículos.
2.  `resultados_rutas_detallados.json`: Un informe completo con todas las métricas de la solución.
