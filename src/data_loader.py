

import json
import sys

def load_json_data(file_path):
    """Carga los datos del archivo JSON."""
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            return json.load(file)
    except FileNotFoundError:
        print(f"✗ Error: No se encontró el archivo de datos '{file_path}'.")
        print("Asegúrate de haber generado primero la matriz de distancias ejecutando un script desde el directorio /scripts.")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"✗ Error: El archivo '{file_path}' no es un JSON válido.")
        sys.exit(1)


def create_data_model(json_path="results/resultados_matrices.json"):
    """Stores the data for the problem."""
    print(f"Cargando datos desde '{json_path}'...")
    data = {}
    
    json_data = load_json_data(json_path)
    
    data["time_matrix"] = json_data["time_matrix"]
    data["distance_matrix"] = json_data["distance_matrix"]
    
    # Las ventanas de tiempo ya vienen en minutos desde el JSON
    data["time_windows"] = json_data["time_windows"]
    
    num_locations = len(data["time_matrix"])
    
    data["num_vehicles"] = json_data.get("num_vehicles", 1)
    vehicle_capacity = json_data.get("vehicle_capacity", max(1, num_locations - 1))
    
    demands_source = "default"
    demands_list = None

    # Determinar índice de depósito según ID == 0 en ubicaciones (si existen)
    ubicaciones = json_data.get("ubicaciones", []) if isinstance(json_data.get("ubicaciones", []), list) else []
    depot_index = 0
    for i, u in enumerate(ubicaciones):
        try:
            if str(u.get("ID", "")).strip() == "0":
                depot_index = i
                break
        except Exception:
            continue

    if "demands" in json_data and isinstance(json_data["demands"], list):
        raw_demands = [int(x) for x in json_data["demands"]]
        if len(raw_demands) == num_locations:
            # Solo asegurar que la demanda en el depósito sea 0
            raw_demands[depot_index] = 0
            demands_list = raw_demands
            demands_source = "json.demands (longitud n)"
        elif len(raw_demands) == num_locations - 1:
            # Insertar 0 en la posición del depósito
            demands_list = raw_demands[:]
            demands_list.insert(depot_index, 0)
            demands_source = "json.demands (longitud n-1)"
    if demands_list is None and ubicaciones:
        try:
            derived = [int(u.get("Demanda", 1)) for u in ubicaciones]
            if len(derived) == num_locations:
                derived[depot_index] = 0
                demands_list = derived
                demands_source = "json.ubicaciones.Demanda (longitud n)"
            elif len(derived) == num_locations - 1:
                temp = derived[:]
                temp.insert(depot_index, 0)
                demands_list = temp
                demands_source = "json.ubicaciones.Demanda (longitud n-1)"
        except Exception:
            pass
    if demands_list is None:
        # Por defecto todos 1 excepto depósito 0
        demands_list = [1] * num_locations
        demands_list[depot_index] = 0
        demands_source = "default(1 por cliente)"

    data["demands"] = demands_list
    data["vehicle_capacities"] = [vehicle_capacity] * data["num_vehicles"]
    data["depot"] = depot_index
    
    # Sumar demanda de todos excepto el depósito
    total_demand = sum(d for idx, d in enumerate(data["demands"]) if idx != data["depot"])
    total_capacity = data["num_vehicles"] * vehicle_capacity


    if total_capacity < total_demand:
        print(f"✗ Error: La capacidad total ({total_capacity}) es menor que la demanda total ({total_demand})")
        print("El programa se detendrá. Aumenta el número de vehículos o la capacidad por vehículo en el JSON de entrada.")
        return None
    else:
        print(f"✓ La capacidad total ({total_capacity}) es suficiente para la demanda total ({total_demand})")
    
    data["ubicaciones"] = json_data["ubicaciones"]

    data["problem_type"] = json_data["problem_type"]
    
    if "service_time" in json_data:
        data["service_time"] = json_data["service_time"]
    
    if "max_time_per_vehicle" in json_data:
        data["max_time_per_vehicle"] = json_data["max_time_per_vehicle"]
    
    if "depot_return_required" in json_data:
        data["depot_return_required"] = json_data["depot_return_required"]
    
    # Manejar pickup_delivery_pairs para PDP
    if "pickup_delivery_pairs" in json_data:
        data["pickup_delivery_pairs"] = json_data["pickup_delivery_pairs"]
        print(f" {len(data['pickup_delivery_pairs'])} Pares Identificados")

    print("\n" + "="*50)
    print("RESUMEN DE CONFIGURACIÓN (cargado desde JSON):")
    print("="*50)
    print(f"Número de ubicaciones: {num_locations}")
    print(f"Número de vehículos: {data['num_vehicles']}")
    print(f"Capacidad por vehículo: {vehicle_capacity}")
    print(f"Demanda total: {total_demand} (fuente: {demands_source})")
    
    problem_type = str(json_data.get("problem_type", "")).strip().upper()
    if problem_type in ("CVRPTW", "TWCVRP"):
        data["problem_type"] = "CVRPTW"
        data["use_time_windows"] = True
        print("Tipo de problema: CVRPTW (ventanas de tiempo ACTIVAS)")
    elif problem_type == "CVRP":
        data["problem_type"] = "CVRP"
        data["use_time_windows"] = False
        print("Tipo de problema: CVRP (ventanas de tiempo DESACTIVADAS por JSON)")
    elif problem_type == "PDP":
        data["problem_type"] = "PDP"
        data["use_time_windows"] = False
        print("Tipo de problema: PDP (Pickup-Delivery Problem)")
    else:
        data["use_time_windows"] = True
        data["problem_type"] = "CVRPTW"
        print("Tipo de problema: CVRPTW (por defecto)")
    
    data["tipo_servicio"] = json_data.get("tipo_servicio")

    print("="*50)
    
    return data
