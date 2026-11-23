import argparse
import json
import logging
import math
import os
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv(".env")

DEFAULT_OSRM_URL = os.getenv("OSRM_BASE_URL", "http://localhost:5000")
SNAP_WARNING_THRESHOLD_M = 100.0


def horario_a_minutos(horario_str) -> int:
    """Convierte un string 'HH:MM' a minutos desde medianoche."""
    try:
        horas, minutos = map(int, str(horario_str).strip().split(":"))
        return horas * 60 + minutos
    except (ValueError, AttributeError):
        return 0


def generar_ventanas_tiempo(df_valid: pd.DataFrame) -> List[Tuple[int, int]]:
    if "Horario_Inicio" not in df_valid.columns or "Horario_Fin" not in df_valid.columns:
        return [(0, 1440) for _ in range(len(df_valid))]
    return [
        (horario_a_minutos(row["Horario_Inicio"]), horario_a_minutos(row["Horario_Fin"]))
        for _, row in df_valid.iterrows()
    ]


def generar_pickup_delivery_pairs(df_valid: pd.DataFrame) -> List[dict]:
    """
    Genera los pares de pickup-delivery basándose en ID_Viaje.
    Las ventanas de tiempo se guardan en minutos desde medianoche.
    """
    if "ID_Viaje" not in df_valid.columns:
        return []

    id_to_indices = {}
    for idx, row in df_valid.iterrows():
        if row["ID_Viaje"] != 0:
            id_viaje = str(row["ID_Viaje"])
            id_to_indices.setdefault(id_viaje, []).append(idx)

    pickup_delivery_pairs = []
    pair_id = 1

    for id_viaje, indices in id_to_indices.items():
        if len(indices) == 2:
            if "Tipo_Servicio" in df_valid.columns:
                indices_ordenados = sorted(
                    indices,
                    key=lambda x: (0 if df_valid.loc[x, "Tipo_Servicio"] == "Recogida" else 1),
                )
                pickup_idx, delivery_idx = indices_ordenados
            else:
                pickup_idx, delivery_idx = indices[0], indices[1]

            pickup_demand = 0
            if "Demanda" in df_valid.columns:
                val = abs(pd.to_numeric(df_valid.loc[pickup_idx, "Demanda"], errors="coerce"))
                pickup_demand = 0 if pd.isna(val) else int(val)

            pickup_time_window = (0, 480)
            delivery_time_window = (60, 600)

            if "Horario_Inicio" in df_valid.columns and "Horario_Fin" in df_valid.columns:
                pickup_start = horario_a_minutos(df_valid.loc[pickup_idx, "Horario_Inicio"])
                pickup_end = horario_a_minutos(df_valid.loc[pickup_idx, "Horario_Fin"])
                delivery_start = horario_a_minutos(df_valid.loc[delivery_idx, "Horario_Inicio"])
                delivery_end = horario_a_minutos(df_valid.loc[delivery_idx, "Horario_Fin"])

                pickup_time_window = (int(pickup_start), int(pickup_end))
                delivery_time_window = (int(delivery_start), int(delivery_end))

            pickup_delivery_pairs.append(
                {
                    "pair_id": pair_id,
                    "pickup_index": pickup_idx,
                    "delivery_index": delivery_idx,
                    "demand": int(pickup_demand),
                    "pickup_time_window": pickup_time_window,
                    "delivery_time_window": delivery_time_window,
                }
            )
            pair_id += 1
        elif len(indices) > 2:
            logging.warning(f"ID_Viaje {id_viaje} tiene {len(indices)} registros; se esperaban 2.")

    return pickup_delivery_pairs


def detectar_tipo_modelo(df_valid: pd.DataFrame) -> Tuple[str, str]:
    """Detecta el tipo de modelo ('CVRPTW', 'PDP' o 'VRP') y el tipo de servicio."""
    if "ID_Viaje" not in df_valid.columns:
        return "VRP", "Desconocido"

    df_sin_deposito = df_valid[df_valid["ID_Viaje"] != 0].copy()
    ids = df_sin_deposito["ID_Viaje"].dropna().astype(str)
    conteo_ids = ids.value_counts()

    if len(conteo_ids) > 0 and (conteo_ids == 1).all():
        tipo_modelo = "CVRPTW"
    elif len(conteo_ids) > 0 and (conteo_ids == 2).all():
        tipo_modelo = "PDP"
    else:
        tipo_modelo = "VRP"

    if "Tipo_Servicio" in df_sin_deposito.columns:
        tipos = df_sin_deposito["Tipo_Servicio"].dropna().unique()
        tipo_servicio = tipos[0] if len(tipos) == 1 else "Mixto"
    else:
        tipo_servicio = "Desconocido"

    return tipo_modelo, tipo_servicio


def _query_osrm_table_block(
    base_url: str,
    origins_lonlat: List[Tuple[float, float]],
    destinations_lonlat: Optional[List[Tuple[float, float]]] = None,
    profile: str = "driving",
    timeout: int = 30,
) -> dict:
    """
    Consulta el endpoint /table/v1/{profile}/ de OSRM para un bloque de orígenes y destinos.
    Si destinations_lonlat es None, calcula el bloque cuadrado origins x origins.
    """
    base_url = base_url.rstrip("/")
    if destinations_lonlat is None:
        all_coords = origins_lonlat
        params = {"annotations": "duration,distance"}
    else:
        all_coords = origins_lonlat + destinations_lonlat
        n_orig = len(origins_lonlat)
        n_dest = len(destinations_lonlat)
        sources_str = ";".join(str(i) for i in range(n_orig))
        destinations_str = ";".join(str(n_orig + j) for j in range(n_dest))
        params = {
            "annotations": "duration,distance",
            "sources": sources_str,
            "destinations": destinations_str,
        }

    coords_path = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in all_coords)
    url = f"{base_url}/table/v1/{profile}/{coords_path}"

    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok":
        raise RuntimeError(f"Respuesta de error de OSRM Table API: {data}")
    return data


def generar_matrices_osrm_local(
    df: pd.DataFrame,
    base_url: str = DEFAULT_OSRM_URL,
    profile: str = "driving",
    chunk_size: int = 75,
) -> Tuple[Optional[List[List[int]]], Optional[List[List[float]]], Optional[pd.DataFrame]]:
    """
    Calcula la matriz de tiempos (minutos) y distancias (km) usando OSRM local.
    Soporta cualquier cantidad de puntos dividiendo en bloques para no exceder la longitud máxima de URL HTTP,
    y valida la distancia de 'snap' de cada coordenada a la red vial de OpenStreetMap.
    """
    df_valid = df.dropna(subset=["Latitud", "Longitud"]).copy()
    df_valid["Latitud"] = pd.to_numeric(df_valid["Latitud"], errors="coerce")
    df_valid["Longitud"] = pd.to_numeric(df_valid["Longitud"], errors="coerce")
    df_valid = df_valid.dropna(subset=["Latitud", "Longitud"]).reset_index(drop=True)

    # Validar rango geográfico (detectar inversión lat/lon accidental)
    invalid_lat = df_valid[(df_valid["Latitud"] < -90) | (df_valid["Latitud"] > 90)]
    if not invalid_lat.empty:
        raise ValueError(
            f"Coordenadas con Latitud fuera de rango [-90, 90] en filas {invalid_lat.index.tolist()}. "
            "Verifica que Latitud y Longitud no estén invertidas."
        )

    n_locations = len(df_valid)
    if n_locations == 0:
        logging.error("No hay ubicaciones válidas para procesar.")
        return None, None, None

    logging.info(f"Calculando matriz {n_locations}x{n_locations} con OSRM local ({base_url})...")

    # OSRM usa (Longitud, Latitud)
    coords_lonlat = list(zip(df_valid["Longitud"].tolist(), df_valid["Latitud"].tolist()))

    final_time_matrix = np.full((n_locations, n_locations), 999, dtype=int)
    final_distance_matrix = np.full((n_locations, n_locations), 999.0, dtype=float)
    snap_distances = np.zeros(n_locations, dtype=float)
    snapped_lats = df_valid["Latitud"].to_numpy(dtype=float).copy()
    snapped_lons = df_valid["Longitud"].to_numpy(dtype=float).copy()

    num_chunks = int(math.ceil(n_locations / chunk_size))

    for i in range(num_chunks):
        start_row = i * chunk_size
        end_row = min(start_row + chunk_size, n_locations)
        origins_block = coords_lonlat[start_row:end_row]

        for j in range(num_chunks):
            start_col = j * chunk_size
            end_col = min(start_col + chunk_size, n_locations)

            if i == j:
                result = _query_osrm_table_block(base_url, origins_block, None, profile=profile)
            else:
                dest_block = coords_lonlat[start_col:end_col]
                result = _query_osrm_table_block(base_url, origins_block, dest_block, profile=profile)

            # Registrar información de snap de los orígenes en la primera columna
            if j == 0 and "sources" in result:
                for local_idx, src_wp in enumerate(result["sources"]):
                    global_idx = start_row + local_idx
                    dist_m = float(src_wp.get("distance", 0.0))
                    loc = src_wp.get("location", [snapped_lons[global_idx], snapped_lats[global_idx]])
                    snap_distances[global_idx] = round(dist_m, 2)
                    snapped_lons[global_idx] = float(loc[0])
                    snapped_lats[global_idx] = float(loc[1])

            durations_sec = np.array(result["durations"], dtype=float)
            distances_m = np.array(result["distances"], dtype=float)

            # Convertir segundos a minutos enteros usando redondeo hacia arriba para tramos > 0s
            # (evita que tramos cortos de 15-29 segundos se conviertan en 0 minutos)
            valid_dur = ~np.isnan(durations_sec)
            minutes_block = np.full(durations_sec.shape, 999, dtype=int)
            minutes_block[valid_dur] = np.where(
                durations_sec[valid_dur] <= 0,
                0,
                np.maximum(1, np.round(durations_sec[valid_dur] / 60.0).astype(int)),
            )

            valid_dist = ~np.isnan(distances_m)
            km_block = np.full(distances_m.shape, 999.0, dtype=float)
            km_block[valid_dist] = np.round(distances_m[valid_dist] / 1000.0, 3)

            final_time_matrix[start_row:end_row, start_col:end_col] = minutes_block
            final_distance_matrix[start_row:end_row, start_col:end_col] = km_block

    np.fill_diagonal(final_time_matrix, 0)
    np.fill_diagonal(final_distance_matrix, 0.0)

    # Adjuntar metadatos de snap al DataFrame para auditoría y visualización
    df_valid["Snap_Latitud"] = snapped_lats
    df_valid["Snap_Longitud"] = snapped_lons
    df_valid["Snap_Distance_m"] = snap_distances

    far_snaps = df_valid[df_valid["Snap_Distance_m"] > SNAP_WARNING_THRESHOLD_M]
    if not far_snaps.empty:
        logging.warning(
            f"Se detectaron {len(far_snaps)} ubicaciones con snap > {SNAP_WARNING_THRESHOLD_M}m de la red vial:"
        )
        for idx, row in far_snaps.iterrows():
            logging.warning(
                f"  - Fila #{idx} '{row.get('Nombre', idx)}': snap a {row['Snap_Distance_m']:.1f}m "
                f"(Original: {row['Latitud']:.6f}, {row['Longitud']:.6f} -> "
                f"Calle: {row['Snap_Latitud']:.6f}, {row['Snap_Longitud']:.6f})"
            )

    return final_time_matrix.tolist(), final_distance_matrix.tolist(), df_valid


def imprimir_y_guardar_resultados(
    time_matrix: List[List[int]],
    distance_matrix: List[List[float]],
    time_windows: List[Tuple[int, int]],
    df_valid: pd.DataFrame,
    output_json: str = "results/resultados_matrices.json",
    osrm_url: str = DEFAULT_OSRM_URL,
) -> dict:
    """Guarda la matriz y configuración del problema en formato compatible con data_loader.py."""
    model_type, type_service = detectar_tipo_modelo(df_valid)

    if "Demanda" in df_valid.columns and model_type in ("CVRPTW", "VRP"):
        demands = pd.to_numeric(df_valid["Demanda"], errors="coerce").fillna(0).astype(int).abs().tolist()
    elif "Demanda" in df_valid.columns and model_type == "PDP":
        demands_series = pd.to_numeric(df_valid["Demanda"], errors="coerce").fillna(0).astype(int)
        demands_series = demands_series.clip(lower=0)
        demands = demands_series.tolist()
    else:
        demands = [0] * len(df_valid)

    pickup_delivery_pairs = []
    if model_type == "PDP":
        pickup_delivery_pairs = generar_pickup_delivery_pairs(df_valid)
        logging.info(f"Generados {len(pickup_delivery_pairs)} pares de pickup-delivery.")

    resultados = {
        "time_matrix": time_matrix,
        "distance_matrix": distance_matrix,
        "time_windows": time_windows,
        "ubicaciones": df_valid.to_dict("records"),
        "demands": demands,
        "pickup_delivery_pairs": pickup_delivery_pairs,
        "num_vehicles": 5,
        "vehicle_capacity": 10,
        "service_time": 10,
        "max_wait_time": 60,
        "max_time_per_vehicle": 90000,
        "depot_return_required": True,
        "api_used": f"OSRM Local ({osrm_url})",
        "metrics": ["duration", "distance"],
        "units": {"time": "minutes", "distance": "kilometers"},
        "problem_type": model_type,
        "tipo_servicio": type_service,
    }

    config_path = Path("data/config.json")
    if config_path.exists():
        try:
            with config_path.open("r", encoding="utf-8") as f:
                config_data = json.load(f)
            resultados.update(config_data)
        except Exception as e:
            logging.warning(f"No se pudo leer data/config.json: {e}")

    output_path = Path(output_json)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(resultados, f, ensure_ascii=False, indent=2)

    logging.info(f"Matriz OSRM local guardada exitosamente en '{output_json}'")
    return resultados


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Genera matriz de tiempos y distancias usando OSRM local.")
    parser.add_argument("--csv_path", type=str, default="data/data.csv", help="Ruta al archivo CSV de entrada.")
    parser.add_argument(
        "--output_json",
        type=str,
        default="results/resultados_matrices.json",
        help="Ruta donde guardar el JSON resultante.",
    )
    parser.add_argument(
        "--osrm_url",
        type=str,
        default=DEFAULT_OSRM_URL,
        help="URL base del servidor OSRM local (por defecto http://localhost:5000).",
    )
    args = parser.parse_args()

    if not os.path.exists(args.csv_path):
        logging.error(f"No se encontró el archivo '{args.csv_path}'.")
        return

    df = pd.read_csv(args.csv_path)
    try:
        time_matrix, distance_matrix, df_valid = generar_matrices_osrm_local(df, base_url=args.osrm_url)
    except requests.exceptions.ConnectionError:
        logging.error(
            f"No se pudo conectar a OSRM local en '{args.osrm_url}'. "
            "Asegúrate de que Docker Desktop esté abierto y de haber ejecutado '.\\setup_osrm.ps1' o 'docker compose up -d'."
        )
        raise SystemExit(1)

    if time_matrix is None or distance_matrix is None or df_valid is None:
        logging.error("Falló la generación de matrices con OSRM local.")
        return

    time_windows = generar_ventanas_tiempo(df_valid)
    imprimir_y_guardar_resultados(
        time_matrix,
        distance_matrix,
        time_windows,
        df_valid,
        output_json=args.output_json,
        osrm_url=args.osrm_url,
    )


if __name__ == "__main__":
    main()

