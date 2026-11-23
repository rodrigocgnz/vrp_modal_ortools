import datetime
import io
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import requests
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from src.data_loader import create_data_model
from src.generate_osrm_matrix import (
    DEFAULT_OSRM_URL,
    detectar_tipo_modelo,
    generar_matrices_osrm_local,
    generar_pickup_delivery_pairs,
    generar_ventanas_tiempo,
    imprimir_y_guardar_resultados as guardar_matriz_osrm,
)
from src.reporting import export_results, extract_routes_from_solution
from src.solver import solve_vrp
from src.visualization import OSRMClient, build_map_payload, create_map_visualization

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="VRP Route Optimizer UI",
    description="Interfaz interactiva en mapa 3D para optimización de rutas VRP / CVRPTW / PDP con OR-Tools",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_CSV_PATH = Path("data/data.csv")
CONFIG_JSON_PATH = Path("data/config.json")
MATRICES_JSON_PATH = Path("results/resultados_matrices.json")
LATEST_MAP_PATH = Path("results/rutas_twcvrp_mapa.html")
LATEST_REPORT_PATH = Path("results/resultados_rutas_detallados.json")


class LocationItem(BaseModel):
    ID: int = 0
    ID_Viaje: int = 0
    Tipo_Servicio: Optional[str] = "Entrega"
    Nombre: str
    Latitud: float
    Longitud: float
    Horario_Inicio: str = "08:00"
    Horario_Fin: str = "23:00"
    Demanda: int = 0


class SolveConfig(BaseModel):
    num_vehicles: int = Field(default=5, ge=1, le=200)
    vehicle_capacity: int = Field(default=10, ge=1)
    service_time: int = Field(default=10, ge=0)
    max_wait_time: int = Field(default=60, ge=0)
    max_time_per_vehicle: int = Field(default=90000, ge=60)
    depot_return_required: bool = True
    problem_type: str = "AUTO"  # AUTO, CVRPTW, CVRP, PDP
    service: str = "osrm"  # osrm, here, ors
    cost_per_km: float = Field(default=3.24, ge=0.0)
    driver_cost_per_hour: float = Field(default=81.25, ge=0.0)
    fixed_cost_per_vehicle: float = Field(default=34.38, ge=0.0)
    currency: str = "MXN"


class SolveRequest(BaseModel):
    locations: List[LocationItem]
    config: SolveConfig = Field(default_factory=SolveConfig)
    save_to_csv: bool = True


def _load_current_csv_locations() -> List[Dict[str, Any]]:
    if not DATA_CSV_PATH.exists():
        return [
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
    df = pd.read_csv(DATA_CSV_PATH)
    records = []
    for idx, row in df.iterrows():
        raw_id = row.get("ID", idx)
        loc_id = 0 if pd.isna(raw_id) else int(raw_id)
        raw_viaje = row.get("ID_Viaje", loc_id)
        id_viaje = 0 if pd.isna(raw_viaje) else int(raw_viaje)
        raw_tipo = row.get("Tipo_Servicio", "")
        if pd.isna(raw_tipo) or str(raw_tipo).strip().lower() in ("nan", "", "none") or loc_id == 0:
            tipo = "Depósito" if loc_id == 0 else "Entrega"
        else:
            tipo = str(raw_tipo).strip()
        raw_dem = pd.to_numeric(row.get("Demanda", 0), errors="coerce")
        demanda = 0 if pd.isna(raw_dem) or loc_id == 0 else int(raw_dem)
        records.append(
            {
                "ID": loc_id,
                "ID_Viaje": 0 if loc_id == 0 else id_viaje,
                "Tipo_Servicio": tipo,
                "Nombre": str(row.get("Nombre", f"Punto {idx}")).strip(),
                "Latitud": float(row["Latitud"]),
                "Longitud": float(row["Longitud"]),
                "Horario_Inicio": str(row.get("Horario_Inicio", "08:00")).strip() or "08:00",
                "Horario_Fin": str(row.get("Horario_Fin", "23:00")).strip() or "23:00",
                "Demanda": demanda,
            }
        )
    return records


def _locations_to_dataframe(locations: List[LocationItem]) -> pd.DataFrame:
    rows = []
    # Asegurar que el depósito (ID == 0 o Tipo_Servicio == Depósito) vaya primero
    sorted_locs = sorted(
        locations,
        key=lambda x: 0 if (x.ID == 0 or (x.Tipo_Servicio or "").lower() in ("depósito", "deposito", "nan")) else 1,
    )
    for idx, loc in enumerate(sorted_locs):
        is_depot = idx == 0
        loc_id = 0 if is_depot else (loc.ID if loc.ID > 0 else idx)
        id_viaje = 0 if is_depot else (loc.ID_Viaje if loc.ID_Viaje > 0 else loc_id)
        tipo_servicio = float("nan") if is_depot else (loc.Tipo_Servicio if loc.Tipo_Servicio not in ("Depósito", "Deposito", "") else "Entrega")
        rows.append(
            {
                "ID": loc_id,
                "ID_Viaje": id_viaje,
                "Tipo_Servicio": tipo_servicio,
                "Nombre": loc.Nombre.strip() or ("Depósito" if is_depot else f"Parada {loc_id}"),
                "Latitud": float(loc.Latitud),
                "Longitud": float(loc.Longitud),
                "Horario_Inicio": loc.Horario_Inicio.strip() or "07:00",
                "Horario_Fin": loc.Horario_Fin.strip() or "23:00",
                "Demanda": 0 if is_depot else int(loc.Demanda),
            }
        )
    return pd.DataFrame(rows)


def _load_current_config() -> Dict[str, Any]:
    default_cfg = {
        "num_vehicles": 5,
        "vehicle_capacity": 10,
        "service_time": 10,
        "max_wait_time": 60,
        "max_time_per_vehicle": 90000,
        "depot_return_required": True,
        "problem_type": "AUTO",
        "service": "osrm",
        "cost_per_km": 3.24,
        "driver_cost_per_hour": 81.25,
        "fixed_cost_per_vehicle": 34.38,
        "currency": "MXN",
    }
    if CONFIG_JSON_PATH.exists():
        try:
            with CONFIG_JSON_PATH.open("r", encoding="utf-8") as f:
                saved = json.load(f)
                default_cfg.update(saved)
        except Exception as e:
            logger.warning(f"No se pudo leer data/config.json: {e}")
    return default_cfg


@app.get("/", response_class=HTMLResponse)
async def index():
    """Sirve la interfaz principal del mapa 3D con el side panel integrado."""
    if LATEST_MAP_PATH.exists():
        html_text = LATEST_MAP_PATH.read_text(encoding="utf-8")
        if "tab-costs" in html_text and "buildRoutePopupHtml" in html_text:
            return HTMLResponse(content=html_text)

    locations = _load_current_csv_locations()
    config = _load_current_config()
    existing_report = None
    if LATEST_REPORT_PATH.exists():
        try:
            with LATEST_REPORT_PATH.open("r", encoding="utf-8") as f:
                existing_report = json.load(f)
        except Exception:
            pass

    data_stub = {
        "ubicaciones": locations,
        "depot": 0,
        "depot_return_required": config.get("depot_return_required", True),
        "num_vehicles": config.get("num_vehicles", 5),
        "vehicle_capacity": config.get("vehicle_capacity", 10),
    }

    html_path = LATEST_MAP_PATH
    html_path.parent.mkdir(parents=True, exist_ok=True)
    create_map_visualization(
        [],
        data_stub,
        output_file=str(html_path),
        initial_locations=locations,
        initial_config=config,
        initial_report=existing_report,
    )
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/api/state")
async def get_state():
    """Devuelve las ubicaciones actuales de data/data.csv y la configuración."""
    locations = _load_current_csv_locations()
    config = _load_current_config()
    report = None
    if LATEST_REPORT_PATH.exists():
        try:
            with LATEST_REPORT_PATH.open("r", encoding="utf-8") as f:
                report = json.load(f)
        except Exception:
            pass
    return {"locations": locations, "config": config, "report": report}


@app.post("/api/import-csv")
async def import_csv(file: UploadFile = File(...)):
    """Importa y valida un archivo CSV con el formato de data/data.csv."""
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="El archivo debe tener extensión .csv")

    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    try:
        df = pd.read_csv(io.StringIO(text))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No se pudo leer el archivo CSV: {e}")

    required_cols = {"Nombre", "Latitud", "Longitud"}
    missing = required_cols - set(df.columns)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"El CSV no contiene las columnas obligatorias: {', '.join(sorted(missing))}. "
            "Columnas esperadas: ID, ID_Viaje, Tipo_Servicio, Nombre, Latitud, Longitud, Horario_Inicio, Horario_Fin, Demanda",
        )

    df = df.dropna(subset=["Latitud", "Longitud"]).copy()
    df["Latitud"] = pd.to_numeric(df["Latitud"], errors="coerce")
    df["Longitud"] = pd.to_numeric(df["Longitud"], errors="coerce")
    df = df.dropna(subset=["Latitud", "Longitud"]).reset_index(drop=True)

    if len(df) == 0:
        raise HTTPException(status_code=400, detail="El CSV no contiene coordenadas válidas.")

    items: List[LocationItem] = []
    for idx, row in df.iterrows():
        raw_id = row.get("ID", idx)
        loc_id = 0 if pd.isna(raw_id) else int(raw_id)
        raw_viaje = row.get("ID_Viaje", loc_id)
        id_viaje = 0 if pd.isna(raw_viaje) else int(raw_viaje)
        raw_tipo = row.get("Tipo_Servicio", "")
        if pd.isna(raw_tipo) or str(raw_tipo).strip().lower() in ("nan", "", "none") or loc_id == 0:
            tipo = "Depósito" if (loc_id == 0 or idx == 0) else "Entrega"
        else:
            tipo = str(raw_tipo).strip()
        raw_dem = pd.to_numeric(row.get("Demanda", 0), errors="coerce")
        demanda = 0 if (pd.isna(raw_dem) or loc_id == 0 or idx == 0) else int(raw_dem)
        items.append(
            LocationItem(
                ID=0 if idx == 0 else loc_id,
                ID_Viaje=0 if idx == 0 else id_viaje,
                Tipo_Servicio=tipo,
                Nombre=str(row.get("Nombre", f"Punto {idx}")).strip(),
                Latitud=float(row["Latitud"]),
                Longitud=float(row["Longitud"]),
                Horario_Inicio=str(row.get("Horario_Inicio", "08:00")).strip() or "08:00",
                Horario_Fin=str(row.get("Horario_Fin", "23:00")).strip() or "23:00",
                Demanda=demanda,
            )
        )

    df_clean = _locations_to_dataframe(items)
    DATA_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    df_clean.to_csv(DATA_CSV_PATH, index=False, encoding="utf-8")

    locations_out = _load_current_csv_locations()
    model_type, service_type = detectar_tipo_modelo(df_clean)
    return {
        "message": f"Se importaron {len(locations_out)} ubicaciones exitosamente.",
        "locations": locations_out,
        "detected_model": model_type,
        "detected_service": service_type,
    }


@app.get("/api/export-csv")
async def export_csv():
    """Descarga el archivo data/data.csv actual."""
    if not DATA_CSV_PATH.exists():
        raise HTTPException(status_code=404, detail="No existe data/data.csv todavía.")
    return FileResponse(
        path=str(DATA_CSV_PATH),
        filename="data.csv",
        media_type="text/csv",
    )


@app.post("/api/solve")
async def solve_endpoint(req: SolveRequest):
    """
    Guarda los puntos y configuración, calcula la matriz de distancias/tiempos,
    ejecuta el solver OR-Tools y devuelve las rutas 3D y métricas detalladas.
    """
    if len(req.locations) < 2:
        raise HTTPException(
            status_code=400,
            detail="Necesitas al menos 2 puntos (1 Depósito y 1 o más Paradas) para calcular rutas.",
        )

    df = _locations_to_dataframe(req.locations)
    if req.save_to_csv:
        DATA_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(DATA_CSV_PATH, index=False, encoding="utf-8")

    # Guardar config.json para mantener sincronía con los scripts CLI
    config_dict = {
        "num_vehicles": req.config.num_vehicles,
        "vehicle_capacity": req.config.vehicle_capacity,
        "service_time": req.config.service_time,
        "max_wait_time": req.config.max_wait_time,
        "max_time_per_vehicle": req.config.max_time_per_vehicle,
        "depot_return_required": req.config.depot_return_required,
        "cost_per_km": req.config.cost_per_km,
        "driver_cost_per_hour": req.config.driver_cost_per_hour,
        "fixed_cost_per_vehicle": req.config.fixed_cost_per_vehicle,
        "currency": req.config.currency,
    }
    if req.config.problem_type and req.config.problem_type.upper() != "AUTO":
        config_dict["problem_type"] = req.config.problem_type.upper()

    CONFIG_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_JSON_PATH.open("w", encoding="utf-8") as f:
        json.dump(config_dict, f, indent=2, ensure_ascii=False)

    # 1. Generar matriz según el servicio elegido
    service = req.config.service.lower()
    time_matrix = None
    distance_matrix = None
    df_valid = None
    osrm_used_url = DEFAULT_OSRM_URL

    if service == "here":
        from src.generate_here_matrix import encontrar_mayor_divisor_csv, generar_matrices_por_chunks as gen_here

        api_key = os.getenv("HERE_API_KEY")
        if not api_key:
            raise HTTPException(status_code=400, detail="Falta la variable de entorno HERE_API_KEY en .env")
        chunk_size = min(len(df), 100)
        time_matrix, distance_matrix, df_valid = gen_here(df, chunk_size, api_key, usar_asincrono=False)
    elif service == "ors":
        from src.generate_ors_matrix import generar_matrices_por_chunks as gen_ors

        api_key = os.getenv("ORSERV_API_KEY")
        if not api_key:
            raise HTTPException(status_code=400, detail="Falta la variable de entorno ORSERV_API_KEY en .env")
        chunk_size = min(len(df), 50)
        time_matrix, distance_matrix, df_valid = gen_ors(df, chunk_size, api_key)
    else:
        # OSRM Local por defecto, con fallback a router.project-osrm.org si Docker no está activo
        try:
            time_matrix, distance_matrix, df_valid = generar_matrices_osrm_local(df, base_url=DEFAULT_OSRM_URL)
        except Exception as e_local:
            logger.warning(f"OSRM local ({DEFAULT_OSRM_URL}) no disponible ({e_local}). Intentando OSRM público...")
            try:
                osrm_used_url = "https://router.project-osrm.org"
                time_matrix, distance_matrix, df_valid = generar_matrices_osrm_local(
                    df, base_url=osrm_used_url, chunk_size=40
                )
            except Exception as e_pub:
                raise HTTPException(
                    status_code=502,
                    detail=f"No se pudo conectar a OSRM local ({DEFAULT_OSRM_URL}) ni de respaldo: {e_pub}",
                )

    if time_matrix is None or distance_matrix is None or df_valid is None:
        raise HTTPException(status_code=500, detail="Error al generar la matriz de tiempos y distancias.")

    time_windows = generar_ventanas_tiempo(df_valid)
    resultados_matriz = guardar_matriz_osrm(
        time_matrix,
        distance_matrix,
        time_windows,
        df_valid,
        output_json=str(MATRICES_JSON_PATH),
        osrm_url=osrm_used_url,
    )

    # Aplicar overrides explícitos desde el side panel
    resultados_matriz.update(config_dict)
    if req.config.problem_type and req.config.problem_type.upper() != "AUTO":
        resultados_matriz["problem_type"] = req.config.problem_type.upper()
        if resultados_matriz["problem_type"] == "PDP" and not resultados_matriz.get("pickup_delivery_pairs"):
            resultados_matriz["pickup_delivery_pairs"] = generar_pickup_delivery_pairs(df_valid)

    with MATRICES_JSON_PATH.open("w", encoding="utf-8") as f:
        json.dump(resultados_matriz, f, ensure_ascii=False, indent=2)

    # 2. Validar capacidad total vs demanda antes de llamar a create_data_model
    data = create_data_model(str(MATRICES_JSON_PATH))
    if data is not None:
        data["cost_per_km"] = req.config.cost_per_km
        data["driver_cost_per_hour"] = req.config.driver_cost_per_hour
        data["fixed_cost_per_vehicle"] = req.config.fixed_cost_per_vehicle
        data["currency"] = req.config.currency
    if data is None:
        total_demand = sum(
            int(loc.Demanda) for idx, loc in enumerate(req.locations) if idx != 0 and loc.ID != 0
        )
        total_cap = req.config.num_vehicles * req.config.vehicle_capacity
        raise HTTPException(
            status_code=400,
            detail=f"La capacidad total de la flota ({req.config.num_vehicles} vehículos × {req.config.vehicle_capacity} = {total_cap}) "
            f"es menor que la demanda total de las paradas ({total_demand}). "
            "Aumenta el número de vehículos o la capacidad por vehículo en el panel lateral.",
        )

    # 3. Ejecutar el solver OR-Tools
    try:
        manager, routing, solution = solve_vrp(data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error en el solver VRP: {e}")

    if not solution:
        raise HTTPException(
            status_code=422,
            detail="OR-Tools no encontró una solución factible con las restricciones actuales. "
            "Prueba ampliando los horarios, aumentando vehículos/capacidad o cambiando el tipo de problema a CVRP.",
        )

    routes = extract_routes_from_solution(data, manager, routing, solution)
    if not routes:
        raise HTTPException(
            status_code=422,
            detail="Se resolvió el modelo pero ninguna ruta tiene paradas asignadas.",
        )

    # 4. Guardar resultados en directorio con timestamp y actualizar archivos principales
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    output_dir = Path("results") / timestamp
    output_dir.mkdir(parents=True, exist_ok=True)

    export_filename = output_dir / "resultados_rutas_detallados.json"
    map_filename = output_dir / "rutas_twcvrp_mapa.html"

    export_results(routes, data, str(export_filename))
    shutil.copyfile(str(export_filename), str(LATEST_REPORT_PATH))

    with export_filename.open("r", encoding="utf-8") as f:
        detailed_report = json.load(f)

    clean_locations = _load_current_csv_locations()
    create_map_visualization(
        routes,
        data,
        output_file=str(map_filename),
        osrm_base_url=osrm_used_url,
        initial_locations=clean_locations,
        initial_config=req.config.model_dump(),
        initial_report=detailed_report,
    )
    shutil.copyfile(str(map_filename), str(LATEST_MAP_PATH))

    map_payload = build_map_payload(routes, data, osrm_base_url=osrm_used_url)

    return {
        "message": f"¡Optimización completada! {len(routes)} vehículo(s) utilizado(s).",
        "map_data": map_payload,
        "report": detailed_report,
        "locations": clean_locations,
        "problem_type": data.get("problem_type", "CVRPTW"),
        "output_dir": str(output_dir),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
