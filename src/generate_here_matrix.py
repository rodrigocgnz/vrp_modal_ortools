import requests
import json
import pandas as pd
import time
import logging
from pathlib import Path
from typing import Optional, List, Tuple
import numpy as np
import math
from dotenv import load_dotenv
import os 

# --- FUNCIONES DE DIVISIÓN EN CHUNKS ---

def encontrar_mayor_divisor_csv(ruta_archivo: str, max_divisor: int = 10000) -> Optional[int]:
    """
    Encuentra el mayor divisor del número de filas de datos en un archivo CSV,
    considerando un divisor máximo para no exceder los límites de la API de HERE.
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

# --- FUNCIONES DE LA API DE HERE ---

def calculate_region_definition(locations: List[List[float]]) -> dict:
    """
    Calcula automáticamente una definición de región circular que contenga todas las ubicaciones.
    """
    if not locations:
        return {"type": "autoCircle"}
    
    lats = [loc[0] for loc in locations]  # lat está en índice 0
    lngs = [loc[1] for loc in locations]  # lng está en índice 1
    
    center_lat = (min(lats) + max(lats)) / 2
    center_lng = (min(lngs) + max(lngs)) / 2
    
    # Calcular radio aproximado usando la distancia máxima desde el centro
    max_distance = 0
    for lat, lng in locations:
        # Fórmula haversine simplificada para distancia aproximada
        dlat = math.radians(lat - center_lat)
        dlng = math.radians(lng - center_lng)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(center_lat)) * math.cos(math.radians(lat)) * math.sin(dlng/2)**2
        c = 2 * math.asin(math.sqrt(a))
        distance = 6371000 * c  # Radio de la Tierra en metros
        max_distance = max(max_distance, distance)
    
    # Añadir un margen del 10% al radio
    radius = int(max_distance * 1.1)
    
    return {
        "type": "circle",
        "center": {"lat": center_lat, "lng": center_lng},
        "radius": radius
    }

def get_here_matrix_sync(origins: List[dict], destinations: List[dict], api_key: str) -> Optional[dict]:
    """
    Obtiene una matriz de distancias/tiempos usando la API síncrona de HERE Matrix Routing.
    """
    url = "https://matrix.router.hereapi.com/v8/matrix"
    
    params = {
        "async": "false",
        "apikey": api_key
    }
    
    headers = {
        "Content-Type": "application/json"
    }
    
    # Convertir coordenadas al formato requerido por HERE
    here_origins = [{"lat": coord[0], "lng": coord[1]} for coord in origins]
    
    # Si origins y destinations son iguales, omitimos destinations para optimizar
    use_same_destinations = origins == destinations
    
    data = {
        "origins": here_origins,
        "profile": "carFast",  # Perfil optimizado para automóviles
        "regionDefinition": {
            "type": "world"  # Permite rutas de cualquier longitud
        },
        "matrixAttributes": ["travelTimes", "distances"]  # Solicitar tanto tiempos como distancias
    }
    
    # Solo incluir destinations si son diferentes a origins
    if not use_same_destinations:
        here_destinations = [{"lat": coord[0], "lng": coord[1]} for coord in destinations]
        data["destinations"] = here_destinations
    
    try:
        response = requests.post(url, params=params, headers=headers, json=data, timeout=120)
        if response.status_code == 200:
            return response.json()
        else:
            logging.error(f"Error en la respuesta de HERE API: {response.status_code} - {response.text}")
            return None
    except requests.exceptions.RequestException as e:
        logging.error(f"Error en la request a HERE API: {e}")
        return None

def get_here_matrix_async(origins: List[dict], destinations: List[dict], api_key: str) -> Optional[dict]:
    """
    Obtiene una matriz de distancias/tiempos usando la API asíncrona de HERE Matrix Routing.
    """
    # Paso 1: Enviar solicitud de cálculo
    url = "https://matrix.router.hereapi.com/v8/matrix"
    
    headers = {
        "Content-Type": "application/json"
    }
    
    params = {
        "apikey": api_key
    }
    
    # Convertir coordenadas al formato requerido por HERE
    here_origins = [{"lat": coord[0], "lng": coord[1]} for coord in origins]
    
    # Si origins y destinations son iguales, omitimos destinations para optimizar
    use_same_destinations = origins == destinations
    
    data = {
        "origins": here_origins,
        "profile": "carFast",  # Perfil optimizado para automóviles
        "regionDefinition": {
            "type": "world"  # Permite rutas de cualquier longitud
        },
        "matrixAttributes": ["travelTimes", "distances"]  # Solicitar tanto tiempos como distancias
    }
    
    # Solo incluir destinations si son diferentes a origins
    if not use_same_destinations:
        here_destinations = [{"lat": coord[0], "lng": coord[1]} for coord in destinations]
        data["destinations"] = here_destinations
    
    try:
        # Enviar solicitud inicial
        response = requests.post(url, params=params, headers=headers, json=data, timeout=60)
        if response.status_code != 200:
            logging.error(f"Error al enviar solicitud a HERE API: {response.status_code} - {response.text}")
            return None
            
        initial_result = response.json()
        matrix_id = initial_result.get("matrixId")
        status_url = initial_result.get("statusUrl")
        
        if not status_url:
            logging.error("No se recibió statusUrl en la respuesta inicial")
            return None
        
        # Paso 2: Polling del estado
        print(f"  -> Esperando cálculo asíncrono (ID: {matrix_id})...")
        max_attempts = 30
        attempt = 0
        
        while attempt < max_attempts:
            time.sleep(5)  # Esperar 5 segundos entre polls
            
            status_response = requests.get(status_url, params={"apikey": api_key}, timeout=30)
            
            if status_response.status_code == 303:  # Cálculo completado
                result_url = status_response.headers.get("Location")
                if not result_url:
                    result_data = status_response.json()
                    result_url = result_data.get("resultUrl")
                
                if result_url:
                    # Paso 3: Descargar resultado
                    result_response = requests.get(result_url, params={"apikey": api_key}, 
                                                 headers={"Accept-Encoding": "gzip"}, timeout=60)
                    if result_response.status_code == 200:
                        return result_response.json()
                    else:
                        logging.error(f"Error al descargar resultado: {result_response.status_code}")
                        return None
            elif status_response.status_code == 200:
                status_data = status_response.json()
                status = status_data.get("status", "unknown")
                print(f"    Estado: {status}")
                
                if status == "completed":
                    result_url = status_data.get("resultUrl")
                    if result_url:
                        result_response = requests.get(result_url, params={"apikey": api_key},
                                                     headers={"Accept-Encoding": "gzip"}, timeout=60)
                        if result_response.status_code == 200:
                            return result_response.json()
            else:
                logging.error(f"Error en polling de estado: {status_response.status_code}")
                break
                
            attempt += 1
        
        logging.error("Timeout esperando el cálculo de la matriz")
        return None
        
    except requests.exceptions.RequestException as e:
        logging.error(f"Error en la request asíncrona a HERE API: {e}")
        return None

def generar_matrices_por_chunks(df, chunk_size, api_key: str, usar_asincrono: bool = False):
    """
    Genera matrices de tiempos y distancias procesando el DataFrame en chunks usando HERE API.
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
    print(f"Modo: {'Asíncrono' if usar_asincrono else 'Síncrono'}")
    
    # HERE usa [lat, lng] mientras que el CSV original tiene [lng, lat]
    all_locations_coords = df_valid[['Latitud', 'Longitud']].values.tolist()

    final_time_matrix = np.full((n_locations, n_locations), 999, dtype=int)
    final_distance_matrix = np.full((n_locations, n_locations), 999.0, dtype=float)

    num_chunks = int(np.ceil(n_locations / chunk_size))
    
    for i in range(num_chunks):
        start_row = i * chunk_size
        end_row = min(start_row + chunk_size, n_locations)
        origins_chunk = all_locations_coords[start_row:end_row]

        for j in range(num_chunks):
            start_col = j * chunk_size
            end_col = min(start_col + chunk_size, n_locations)
            destinations_chunk = all_locations_coords[start_col:end_col]

            print(f"Procesando chunk: Orígenes {start_row}-{end_row-1} -> Destinos {start_col}-{end_col-1}")
            
            # Elegir método síncrono o asíncrono
            if usar_asincrono:
                result = get_here_matrix_async(origins_chunk, destinations_chunk, api_key)
            else:
                result = get_here_matrix_sync(origins_chunk, destinations_chunk, api_key)

            if result and "matrix" in result:
                matrix_data = result["matrix"]
                travel_times = matrix_data.get("travelTimes", [])
                distances = matrix_data.get("distances", [])
                
                if travel_times:
                    # Convertir a matriz 2D
                    num_origins = len(origins_chunk)
                    num_destinations = len(destinations_chunk)
                    
                    # Procesar tiempos de viaje
                    time_matrix_chunk = np.array(travel_times).reshape(num_origins, num_destinations)
                    # Convertir de segundos a minutos
                    time_matrix_minutes = np.where(time_matrix_chunk == 0, 999, 
                                                 (time_matrix_chunk / 60).round().astype(int))
                    
                    final_time_matrix[start_row:end_row, start_col:end_col] = time_matrix_minutes
                    
                    # Procesar distancias si están disponibles
                    if distances:
                        distance_matrix_chunk = np.array(distances).reshape(num_origins, num_destinations)
                        # Convertir de metros a kilómetros
                        distance_matrix_km = np.where(distance_matrix_chunk == 0, 999.0,
                                                    (distance_matrix_chunk / 1000).round(2))
                        final_distance_matrix[start_row:end_row, start_col:end_col] = distance_matrix_km
                    else:
                        # Si no hay distancias, aproximar basándose en tiempo
                        velocidad_promedio = 0.8  # ~48 km/h
                        distance_matrix_chunk = time_matrix_minutes * velocidad_promedio
                        distance_matrix_chunk = np.where(distance_matrix_chunk == 999 * velocidad_promedio, 
                                                       999.0, distance_matrix_chunk)
                        final_distance_matrix[start_row:end_row, start_col:end_col] = distance_matrix_chunk
                    
                    print("  -> Chunk procesado exitosamente.")
                else:
                    print("  -> ERROR: No se recibieron tiempos de viaje. Se usarán valores por defecto (999).")
            else:
                print("  -> ERROR al procesar este chunk. Se usarán valores por defecto (999).")
            
            time.sleep(2)  # Pausa entre requests para evitar rate limiting
    
    print("\n¡Ensamblaje de matrices completado!")

    return final_time_matrix.tolist(), final_distance_matrix.tolist(), df_valid

# --- FUNCIONES DE PROCESAMIENTO Y SALIDA ---

def horario_a_minutos(horario_str):
    try:
        horas, minutos = map(int, str(horario_str).split(':'))
        return horas * 60 + minutos
    except (ValueError, AttributeError):
        return 0

def generar_ventanas_tiempo(df_valid):
    return [(horario_a_minutos(row['Horario_Inicio']), horario_a_minutos(row['Horario_Fin'])) for _, row in df_valid.iterrows()]

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
                pickup_start = horario_a_minutos(df_valid.loc[pickup_idx, "Horario_Inicio"])
                pickup_end = horario_a_minutos(df_valid.loc[pickup_idx, "Horario_Fin"])
                delivery_start = horario_a_minutos(df_valid.loc[delivery_idx, "Horario_Inicio"])
                delivery_end = horario_a_minutos(df_valid.loc[delivery_idx, "Horario_Fin"])
                
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
        "api_used": "HERE Matrix Routing",
        "metrics": ["duration", "distance"],
        "units": {
            "time": "minutes",
            "distance": "kilometers"
        },
        "problem_type": model_type,
        "tipo_servicio": type_service
    }

    if os.path.exists("data/config.json"):
        with open("data/config.json", "r", encoding="utf-8") as f:
            config_data = json.load(f)
        resultados.update(config_data)

    # Guardar en archivo JSON
    with open('results/resultados_matrices.json', 'w', encoding='utf-8') as f:
        json.dump(resultados, f, ensure_ascii=False, indent=2)

    print(f"Resultados completos guardados en 'results/resultados_matrices.json'")

# --- FUNCIÓN PRINCIPAL ---

def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    print("Iniciando generación de matrices con HERE Matrix Routing API por chunks...")
    
    # --- IMPORTANTE: REEMPLAZA CON TU API KEY REAL DE HERE ---
    load_dotenv('.env')
    API_KEY: str = os.getenv('HERE_API_KEY')
    if not API_KEY :
        print("\nERROR: Por favor, introduce tu API Key de HERE en la variable API_KEY del script.")
        print("Puedes obtener una API Key gratuita en: https://developer.here.com/")
        return

    archivo_csv = 'data/data.csv'
    try:
        df = pd.read_csv(archivo_csv)
    except FileNotFoundError:
        print(f"ERROR: No se encontró el archivo '{archivo_csv}'. Asegúrate de que exista en la misma carpeta.")
        return

    # HERE permite matrices más grandes que ORS, pero para chunks usamos un tamaño conservador
    chunk_size = encontrar_mayor_divisor_csv(archivo_csv, max_divisor=10000)
    if not chunk_size:
        print("No se pudo determinar un tamaño de chunk válido. Abortando.")
        return
    
    # Preguntar al usuario qué método usar
    print("\nSelecciona el método de cálculo:")
    print("1. Síncrono (más rápido para matrices pequeñas)")
    print("2. Asíncrono (recomendado para matrices grandes)")
    
    try:
        opcion = input("Ingresa 1 o 2: ").strip()
        usar_asincrono = opcion == "2"
    except KeyboardInterrupt:
        print("\nProceso cancelado por el usuario.")
        return
        
    try:
        time_matrix, distance_matrix, df_valid = generar_matrices_por_chunks(
            df, chunk_size, API_KEY, usar_asincrono=usar_asincrono
        )
        
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