import requests
import json
import pandas as pd
import time
import logging
from pathlib import Path
from typing import Optional, List
import numpy as np 
from dotenv import load_dotenv
import os 

# --- FUNCIONES DE DIVISIÓN EN CHUNKS ---

def encontrar_mayor_divisor_csv(ruta_archivo: str, max_divisor: int = 10000) -> Optional[int]:
    """
    Encuentra el mayor divisor del número de filas de datos en un archivo CSV,
    considerando un divisor máximo para no exceder los límites de la API (50 para ORS).
    """
    logger = logging.getLogger(__name__)
    try:
        archivo_path = Path(ruta_archivo)
        with archivo_path.open('r', encoding='utf-8') as f:
            num_filas = sum(1 for _ in f) - 1
        
        logger.info(f"El archivo '{archivo_path.name}' tiene {num_filas} filas de datos.")

        if num_filas <= 0:
            logger.warning("El archivo no contiene filas de datos para analizar.")
            return None
            
        if num_filas <= max_divisor:
            logger.info(f"El número de filas ({num_filas}) es menor o igual al máximo ({max_divisor}). Se usará como tamaño de chunk.")
            return num_filas

        for divisor in range(max_divisor, 0, -1):
            if num_filas % divisor == 0:
                logger.info(f"El mayor divisor encontrado para {num_filas} (con límite {max_divisor}) es: {divisor}")
                return divisor
    except FileNotFoundError:
        logger.error(f"Error: El archivo no se encontró en la ruta '{ruta_archivo}'")
        raise
    except Exception as e:
        logger.error(f"Ocurrió un error inesperado al procesar el archivo: {e}")
        raise
    return None

# --- FUNCIONES DE LA API DE OPENROUTESERVICE ---

def get_distance_matrix(locations, sources, destinations, metrics=["duration", "distance"], api_key=None):
    """
    Obtiene una sub-matriz de distancias/duración usando la API de OpenRouteService.
    """
    url = "https://api.openrouteservice.org/v2/matrix/driving-car"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json, application/geo+json, application/gpx+xml, img/png; charset=utf-8",
        "Authorization": api_key
    }
    
    data = {
        "locations": locations,
        "sources": sources,
        "destinations": destinations,
        "metrics": metrics,
        "units": "m"
    }
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=60)
        if response.status_code == 200:
            return response.json()
        else:
            logging.error(f"Error en la respuesta de la API: {response.status_code} - {response.text}")
            return None
    except requests.exceptions.RequestException as e:
        logging.error(f"Error en la request a la API: {e}")
        return None

def generar_matrices_por_chunks(df, chunk_size, api_key=None):
    """
    Genera matrices de tiempos y distancias procesando el DataFrame en chunks.
    """
    df_valid = df.dropna(subset=['Latitud', 'Longitud']).copy()
    df_valid = df_valid[
        pd.to_numeric(df_valid['Latitud'], errors='coerce').notna() &
        pd.to_numeric(df_valid['Longitud'], errors='coerce').notna()
    ].copy().reset_index(drop=True)
    
    n_locations = len(df_valid)
    if n_locations == 0:
        print("Error: No hay ubicaciones válidas para procesar.")
        return None, None, None

    print(f"Procesando {n_locations} ubicaciones en chunks de tamaño ~{chunk_size}...")
    all_locations_coords = df_valid[['Longitud', 'Latitud']].values.tolist()

    final_time_matrix = np.full((n_locations, n_locations), 999, dtype=int)
    final_distance_matrix = np.full((n_locations, n_locations), 999.0, dtype=float)

    num_chunks = int(np.ceil(n_locations / chunk_size))
    
    for i in range(num_chunks):
        start_row = i * chunk_size
        end_row = min(start_row + chunk_size, n_locations)
        source_indices = list(range(start_row, end_row))

        for j in range(num_chunks):
            start_col = j * chunk_size
            end_col = min(start_col + chunk_size, n_locations)
            dest_indices = list(range(start_col, end_col))

            print(f"Procesando chunk: Orígenes {start_row}-{end_row-1} -> Destinos {start_col}-{end_col-1}")
            result = get_distance_matrix(all_locations_coords, source_indices, dest_indices, api_key=api_key)

            if result and "durations" in result and "distances" in result:
                durations_seconds = np.array(result["durations"], dtype=float)
                durations_minutes = np.where(np.isnan(durations_seconds), 999, (durations_seconds / 60)).round().astype(int)
                final_time_matrix[start_row:end_row, start_col:end_col] = durations_minutes

                distances_meters = np.array(result["distances"], dtype=float)
                distances_km = np.where(np.isnan(distances_meters), 999.0, (distances_meters / 1000)).round(2)
                final_distance_matrix[start_row:end_row, start_col:end_col] = distances_km
                print("  -> Chunk procesado exitosamente.")
            else:
                print("  -> ERROR al procesar este chunk. Se usarán valores por defecto (999).")
            time.sleep(1)
    
    print("\n¡Ensamblaje de matrices completado!")
    return final_time_matrix.tolist(), final_distance_matrix.tolist(), df_valid

# --- FUNCIONES DE PROCESAMIENTO Y SALIDA ---

def horario_a_horas(horario_str):
    try:
        horas, minutos = map(int, str(horario_str).split(':'))
        return horas*60 + minutos
    except (ValueError, AttributeError):
        return 0

def generar_ventanas_tiempo(df_valid):
    return [(horario_a_horas(row['Horario_Inicio']), horario_a_horas(row['Horario_Fin'])) for _, row in df_valid.iterrows()]

def generar_pickup_delivery_pairs(df_valid):
    """
    Genera los pares de pickup-delivery basándose en el ID_Viaje.
    Retorna una lista de diccionarios con la estructura completa para cada par.
    """
    df_sin_deposito = df_valid[df_valid["ID_Viaje"] != 0].copy()
    
    if "ID_Viaje" not in df_sin_deposito.columns:
        return []
    
    # Crear un mapeo de ID_Viaje a índices en el DataFrame original
    id_to_indices = {}
    for idx, row in df_valid.iterrows():
        if row["ID_Viaje"] != 0:  # Excluir depósito
            id_viaje = str(row["ID_Viaje"])
            if id_viaje not in id_to_indices:
                id_to_indices[id_viaje] = []
            id_to_indices[id_viaje].append(idx)
    
    pickup_delivery_pairs = []
    pair_id = 1
    
    for id_viaje, indices in id_to_indices.items():
        if len(indices) == 2:
            # Ordenar por Tipo_Servicio: "Recogida" primero, luego "Entrega"
            if "Tipo_Servicio" in df_valid.columns:
                indices_ordenados = sorted(indices, key=lambda x: 
                    (0 if df_valid.loc[x, "Tipo_Servicio"] == "Recogida" else 1))
                pickup_idx, delivery_idx = indices_ordenados
            else:
                # Si no hay Tipo_Servicio, usar el orden original
                pickup_idx, delivery_idx = indices[0], indices[1]
            
            # Obtener demanda del pickup (valor absoluto)
            pickup_demand = 0
            if "Demanda" in df_valid.columns:
                pickup_demand = abs(pd.to_numeric(df_valid.loc[pickup_idx, "Demanda"], errors='coerce'))
                if pd.isna(pickup_demand):
                    pickup_demand = 0
            
            # Generar ventanas de tiempo
            pickup_time_window = (0, 480)  # Default: 0:00-8:00
            delivery_time_window = (60, 600)  # Default: 1:00-10:00
            
            if "Horario_Inicio" in df_valid.columns and "Horario_Fin" in df_valid.columns:
                # Convertir horarios a minutos desde medianoche
                pickup_start = horario_a_horas(df_valid.loc[pickup_idx, "Horario_Inicio"]) * 60
                pickup_end = horario_a_horas(df_valid.loc[pickup_idx, "Horario_Fin"]) * 60
                delivery_start = horario_a_horas(df_valid.loc[delivery_idx, "Horario_Inicio"]) * 60
                delivery_end = horario_a_horas(df_valid.loc[delivery_idx, "Horario_Fin"]) * 60
                
                pickup_time_window = (int(pickup_start), int(pickup_end))
                delivery_time_window = (int(delivery_start), int(delivery_end))
            
            pair_data = {
                "pair_id": pair_id,
                "pickup_index": pickup_idx,
                "delivery_index": delivery_idx,
                "demand": int(pickup_demand),
                "pickup_time_window": pickup_time_window,
                "delivery_time_window": delivery_time_window
            }
            
            pickup_delivery_pairs.append(pair_data)
            pair_id += 1
            
        elif len(indices) > 2:
            print(f"Advertencia: ID_Viaje {id_viaje} tiene {len(indices)} registros, se esperaban 2.")
    
    return pickup_delivery_pairs

def detectar_tipo_modelo(df_valid):
    """
    Detecta el tipo de modelo basándose en la columna 'ID' y 'Tipo_Servicio'.
    
    Retorna:
    - tipo_modelo: "CVRPTW", "PDP" o "VRP"
    - usa_time_windows: True/False
    - tipo_servicio: "Entrega", "Recolección" o "Mixto"
    """
    usa_time_windows = True  # Default para ORS

    df_sin_deposito = df_valid[df_valid["ID_Viaje"] != 0].copy()

    if "ID_Viaje" not in df_sin_deposito.columns:
        print("Columna 'ID_Viaje' no encontrada, se asume VRP.")
        tipo_modelo = "VRP"
    else:
        ids = df_sin_deposito["ID_Viaje"].dropna().astype(str)
        conteo_ids = ids.value_counts()

        # Caso CVRPTW → todos únicos
        if (conteo_ids == 1).all():
            print("Detectado: CVRPTW")
            tipo_modelo = "CVRPTW"

        # Caso PDP → todos los IDs tienen exactamente 2 registros
        elif (conteo_ids == 2).all():
            print("Detectado: PDP (Pickup-Delivery Problem)")
            tipo_modelo = "PDP"

        else:
            print("Advertencia: Datos mixtos, algunos ID_Viaje no cumplen la regla PDP o CVRPTW.")
            tipo_modelo = "VRP"

    # Analizar Tipo_Servicio
    if "Tipo_Servicio" in df_sin_deposito.columns:
        tipos = df_sin_deposito["Tipo_Servicio"].dropna().unique()
        if len(tipos) == 1:
            tipo_servicio = tipos[0]  # "Entrega" o "Recogida"
        else:
            tipo_servicio = "Mixto"
    else:
        tipo_servicio = "Desconocido"

    return tipo_modelo, tipo_servicio

# --- FUNCIÓN DE GUARDADO (MODIFICADA) ---
def imprimir_y_guardar_resultados(time_matrix, distance_matrix, time_windows, df_valid):
    """
    Imprime un resumen y guarda los resultados en archivos JSON y CSV con la estructura completa.
    """
    # Detectar tipo de modelo automáticamente
    model_type, type_service = detectar_tipo_modelo(df_valid)

    # --- DEMANDAS ---
    if 'Demanda' in df_valid.columns and (model_type == "CVRPTW" or model_type == "VRP"):
        demands = pd.to_numeric(df_valid['Demanda'], errors='coerce').fillna(0).astype(int).abs().tolist()
    elif 'Demanda' in df_valid.columns and model_type == "PDP":
        demands = pd.to_numeric(df_valid['Demanda'], errors='coerce').fillna(0).astype(int)
        df_valid.loc[df_valid['Demanda'] < 0, 'Demanda'] = 0
        demands = df_valid['Demanda'].tolist()
    else:
        demands = [0] * len(df_valid)

    # --- PICKUP-DELIVERY PAIRS ---
    pickup_delivery_pairs = []
    if model_type == "PDP":
        pickup_delivery_pairs = generar_pickup_delivery_pairs(df_valid)
        print(f"Generados {len(pickup_delivery_pairs)} pares de pickup-delivery")

    # --- ESTRUCTURA JSON COMPLETA ---
    resultados = {
        "time_matrix": time_matrix,
        "distance_matrix": distance_matrix,
        "time_windows": time_windows,
        "ubicaciones": df_valid.to_dict('records'),
        "demands": demands,
        "pickup_delivery_pairs": pickup_delivery_pairs,
        "num_vehicles": 5,
        "vehicle_capacity": 10,
        "service_time": 10,
        "max_wait_time": 60,
        "max_time_per_vehicle": 90000,
        "depot_return_required": True,
        "api_used": "OpenRouteService",
        "metrics": ["duration", "distance"],
        "units": {
            "time": "minutes",
            "distance": "kilometers"
        },
        "problem_type": model_type,
        "tipo_servicio": type_service
    }
    
    # Guardar en archivo JSON
    with open('results/resultados_matrices.json', 'w', encoding='utf-8') as f:
        json.dump(resultados, f, ensure_ascii=False, indent=2)
    print(f"Resultados completos guardados en 'results/resultados_matrices.json'")


# --- FUNCIÓN PRINCIPAL ---

def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    print("Iniciando generación de matrices con OpenRouteService por chunks...")
    
    # --- IMPORTANTE: REEMPLAZA CON TU API KEY REAL ---
    load_dotenv('.env')
    API_KEY: str = os.getenv('ORSERV_API_KEY')
    if not API_KEY :
        print("\nERROR: Por favor, introduce tu API Key de OpenRouteService en la variable API_KEY del script.")
        return

    archivo_csv = 'data/data.csv'
    try:
        df = pd.read_csv(archivo_csv)
    except FileNotFoundError:
        print(f"ERROR: No se encontró el archivo '{archivo_csv}'. Asegúrate de que exista en la misma carpeta.")
        return

    # ORS permite matrices más grandes que antes, pero para chunks usamos un tamaño conservador
    chunk_size = encontrar_mayor_divisor_csv(archivo_csv, max_divisor=10000)
    if not chunk_size:
        print("No se pudo determinar un tamaño de chunk válido. Abortando.")
        return
        
    try:
        time_matrix, distance_matrix, df_valid = generar_matrices_por_chunks(df, chunk_size, API_KEY)
        
        if time_matrix is None or distance_matrix is None:
            print("No se pudieron generar las matrices. Proceso finalizado con errores.")
            return
            
        time_windows = generar_ventanas_tiempo(df_valid)
        imprimir_y_guardar_resultados(time_matrix, distance_matrix, time_windows, df_valid)
        
        print("\nProceso completado exitosamente!")
        
    except Exception as e:
        print(f"\nERROR fatal durante el proceso: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
