
import logging
import json
import time

logging.basicConfig(level=logging.INFO)

# Constantes de costos (MXN)
COSTO_POR_KM = 3.24
COSTO_POR_HORA_CONDUCTOR = 81.25
COSTO_FIJO_VEHICULO = 34.38

def extract_routes_from_solution(data, manager, routing, solution):
    """Extrae las rutas de la solución."""
    if not solution:
        return None
    
    time_dimension = routing.GetDimensionOrDie("Time")
    routes = []
    
    for vehicle_id in range(data["num_vehicles"]):
        route = {
            'vehicle_id': vehicle_id,
            'stops': [],
            'total_time': 0,
            'total_load': 0
        }
        
        index = routing.Start(vehicle_id)
        start_time_var = time_dimension.CumulVar(index)
        start_time = solution.Min(start_time_var)
        last_departure_time = start_time
        
        while not routing.IsEnd(index):
            node_index = manager.IndexToNode(index)
            time_var = time_dimension.CumulVar(index)
            
            ubicacion = data["ubicaciones"][node_index]
            
            service_time = 0
            if node_index != data["depot"] and "service_time" in data:
                service_time = data["service_time"]
            
            arrival_time = solution.Min(time_var)
            
            # Calcular hora de salida según el tipo de nodo
            if node_index == data["depot"]:
                if len(route['stops']) == 0:
                    # Es el primer stop (inicio de ruta) - calcular hora óptima de salida
                    # Buscar el siguiente cliente en la ruta para calcular hora óptima de salida
                    next_index = solution.Value(routing.NextVar(index))
                    if next_index != routing.End(0):  # Si hay siguiente parada
                        next_node = manager.IndexToNode(next_index)
                        next_time_window = data["time_windows"][next_node]
                        travel_time_to_next = data["time_matrix"][node_index][next_node]
                        # Calcular hora óptima de salida para llegar justo a tiempo
                        optimal_departure = next_time_window[0] - travel_time_to_next
                        # Asegurar que no sea antes del inicio del time window del depósito
                        depot_window_start = data["time_windows"][node_index][0]
                        departure_time = max(optimal_departure, depot_window_start)
                    else:
                        departure_time = arrival_time
                else:
                    # Para el depósito de retorno, usar el tiempo de llegada
                    departure_time = arrival_time
            else:
                # Para clientes, agregar tiempo de servicio
                departure_time = arrival_time + service_time
            
            stop_info = {
                'node_index': node_index,
                'name': ubicacion['Nombre'],
                'coordinates': [ubicacion['Latitud'], ubicacion['Longitud']],
                'arrival_time': arrival_time,
                'service_time': service_time,
                'departure_time': departure_time,
                'time_window': data["time_windows"][node_index],
                'demand': data["demands"][node_index]
            }
            
            route['stops'].append(stop_info)
            
            if node_index != data["depot"]:
                route['total_load'] += data["demands"][node_index]

            last_departure_time = stop_info['departure_time']
            
            index = solution.Value(routing.NextVar(index))
        
        node_index = manager.IndexToNode(index)
        time_var = time_dimension.CumulVar(index)
        end_time = solution.Min(time_var)

        if data.get('depot_return_required', True):
            ubicacion = data["ubicaciones"][node_index]
            stop_info = {
                'node_index': node_index,
                'name': ubicacion['Nombre'] + " (RETORNO)",
                'coordinates': [ubicacion['Latitud'], ubicacion['Longitud']],
                'arrival_time': end_time,
                'service_time': 0,
                'departure_time': end_time,
                'time_window': data["time_windows"][node_index],
                'demand': 0
            }
            route['stops'].append(stop_info)
            
            # Calcular tiempo real de viaje usando la matriz de tiempos
            total_travel_time = 0
            total_service_time = 0
            
            for i in range(len(route['stops']) - 1):
                from_node = route['stops'][i]['node_index']
                to_node = route['stops'][i + 1]['node_index']
                travel_time = data["time_matrix"][from_node][to_node]
                total_travel_time += travel_time
                
                # Sumar tiempo de servicio (excluyendo depósitos)
                if from_node != data["depot"]:
                    total_service_time += route['stops'][i].get('service_time', 0)
            
            route['total_time'] = total_travel_time + total_service_time
        else:
            # Si no hay retorno al depósito, calcular tiempo real de viaje
            total_travel_time = 0
            total_service_time = 0
            
            for i in range(len(route['stops']) - 1):
                from_node = route['stops'][i]['node_index']
                to_node = route['stops'][i + 1]['node_index']
                travel_time = data["time_matrix"][from_node][to_node]
                total_travel_time += travel_time
                
                # Sumar tiempo de servicio (excluyendo depósitos)
                if from_node != data["depot"]:
                    total_service_time += route['stops'][i].get('service_time', 0)
            
            route['total_time'] = total_travel_time + total_service_time
        
        if len(route['stops']) > 2 or (not data.get('depot_return_required', True) and len(route['stops']) > 1):
            routes.append(route)
    
    return routes

def export_results(routes, data, filename="resultados_rutas_detallados.json"):
    """Exporta los resultados en formato JSON para análisis posterior"""
    total_time = sum(route["total_time"] for route in routes)
    total_load = sum(route["total_load"] for route in routes)
    # Calcular paradas reales (excluyendo depósitos)
    total_stops = 0
    for route in routes:
        if data.get('depot_return_required', True):
            # Excluir depósito inicial y final
            total_stops += len(route['stops']) - 2
        else:
            # Excluir solo depósito inicial
            total_stops += len(route['stops']) - 1

    # Calcular tiempo total de servicio (excluyendo depósitos)
    total_service_time = 0
    for route in routes:
        for stop in route['stops']:
            if stop['node_index'] != data["depot"]:
                total_service_time += stop.get("service_time", 0)

    vehicles_efficiency = []
    total_distance = 0
    
    for route in routes:
        route_distance = sum(data["distance_matrix"][route["stops"][i]["node_index"]][route["stops"][i + 1]["node_index"]] for i in range(len(route["stops"]) - 1))
        total_distance += route_distance
        
        # Add more calculations here if needed

    total_fuel_maintenance_cost = total_distance * COSTO_POR_KM
    total_driver_cost = (total_time / 60) * COSTO_POR_HORA_CONDUCTOR
    total_vehicle_fixed_cost = len(routes) * COSTO_FIJO_VEHICULO
    total_cost = total_fuel_maintenance_cost + total_driver_cost + total_vehicle_fixed_cost

    results = {
        "summary": {
            "total_vehicles_used": len(routes),
            "total_vehicles_available": data["num_vehicles"],
            "total_stops_served": total_stops,
            "total_time_minutes": total_time,
            "total_distance_km": round(total_distance, 2),
            "cost_analysis": {
                "total_cost": round(total_cost, 2),
                "fuel_maintenance_cost": round(total_fuel_maintenance_cost, 2),
                "driver_cost": round(total_driver_cost, 2),
                "vehicle_fixed_cost": round(total_vehicle_fixed_cost, 2),
            }
        },
        "detailed_routes": [],
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    
    for route in routes:
        # Calcular distancia total de la ruta
        route_distance = sum(data["distance_matrix"][route["stops"][i]["node_index"]][route["stops"][i + 1]["node_index"]] for i in range(len(route["stops"]) - 1))
        
        # Calcular tiempo de servicio total de la ruta (excluyendo depósitos)
        route_service_time = 0
        for stop in route['stops']:
            if stop['node_index'] != data["depot"]:
                route_service_time += stop.get("service_time", 0)
        
        # Obtener capacidad del vehículo
        vehicle_capacity = data.get("vehicle_capacity", 10)
        if isinstance(vehicle_capacity, list):
            vehicle_capacity = vehicle_capacity[route["vehicle_id"]] if route["vehicle_id"] < len(vehicle_capacity) else vehicle_capacity[0]
        
        # Análisis de eficiencia de carga
        route_utilization_percentage = (route["total_load"] / vehicle_capacity * 100) if vehicle_capacity > 0 else 0
        route_load_efficiency_per_km = route["total_load"] / route_distance if route_distance > 0 else 0
        route_load_efficiency_per_service_minute = route["total_load"] / route_service_time if route_service_time > 0 else 0
        # Calcular carga por parada (excluyendo depósitos)
        num_real_stops = len(route["stops"]) - 2 if data.get('depot_return_required', True) else len(route["stops"]) - 1
        route_load_per_stop = route["total_load"] / num_real_stops if num_real_stops > 0 else 0
        
        # Análisis de costos por ruta
        fuel_maintenance_cost = route_distance * COSTO_POR_KM
        driver_cost = (route["total_time"] / 60) * COSTO_POR_HORA_CONDUCTOR
        vehicle_fixed_cost = COSTO_FIJO_VEHICULO
        total_route_cost = fuel_maintenance_cost + driver_cost + vehicle_fixed_cost
        
        # Determinar tipo de parada para PDP
        def get_stop_type(stop, data):
            if stop['node_index'] == data["depot"]:
                return "depot"
            elif data.get("problem_type") == "PDP" and "pickup_delivery_pairs" in data:
                for pair in data["pickup_delivery_pairs"]:
                    if stop['node_index'] == pair["pickup_index"]:
                        return "pickup"
                    elif stop['node_index'] == pair["delivery_index"]:
                        return "delivery"
            return "customer"
        
        route_detail = {
            "vehicle_id": route["vehicle_id"],
            "total_time_minutes": route["total_time"],
            "total_time_hours": round(route["total_time"] / 60, 2),
            "total_service_time_minutes": route_service_time,
            "total_service_time_hours": round(route_service_time / 60, 2),
            "total_load": route["total_load"],
            "num_stops": num_real_stops,  # Número real de paradas (excluyendo depósitos)
            "total_distance_km": round(route_distance, 2),
            "load_efficiency_analysis": {
                "utilization_percentage": route_utilization_percentage,
                "load_efficiency_per_km": route_load_efficiency_per_km,
                "load_efficiency_per_service_minute": route_load_efficiency_per_service_minute,
                "load_per_stop": route_load_per_stop,
                "vehicle_capacity": vehicle_capacity
            },
            "cost_analysis": {
                "fuel_maintenance_cost": round(fuel_maintenance_cost, 2),
                "driver_cost": round(driver_cost, 2),
                "vehicle_fixed_cost": round(vehicle_fixed_cost, 2),
                "total_route_cost": round(total_route_cost, 2)
            },
            "stops_sequence": []
        }
        
        
        tipo_servicio = data.get("tipo_servicio", "").lower()

        # Log service type only once at the beginning
        if route == routes[0]:  # Only log for the first route
            if tipo_servicio == "entrega":
                logging.info("Tipo de servicio: Entrega")
            elif tipo_servicio == "recogida":
                logging.info("Tipo de servicio: Recogida")
            else:
                logging.info("Tipo de servicio: Mixto")

        # Set current load based on service type
        if tipo_servicio == "entrega":
            current_load = route["total_load"]
        else:
            current_load = 0
                
        for stop in route["stops"]:
            if stop["node_index"] != data["depot"]:  
                if tipo_servicio == "entrega":
                    current_load -= stop["demand"]
                elif tipo_servicio == "recogida":
                    current_load += stop["demand"]
                else:
                    current_load += stop["demand"]
            
            stop_detail = {
                "sequence": len(route_detail["stops_sequence"]) + 1,
                "name": stop["name"],
                "type": get_stop_type(stop, data),
                "coordinates": stop["coordinates"],
                "arrival_time_minutes": stop["arrival_time"],
                "arrival_time_formatted": f"{stop['arrival_time'] // 60:02d}:{stop['arrival_time'] % 60:02d}",
                "service_time_minutes": stop.get("service_time", 0),
                "departure_time_minutes": stop.get("departure_time", stop["arrival_time"]),
                "departure_time_formatted": f"{stop.get('departure_time', stop['arrival_time']) // 60:02d}:{stop.get('departure_time', stop['arrival_time']) % 60:02d}",
                "time_window": {
                    "start_minutes": stop["time_window"][0],
                    "end_minutes": stop["time_window"][1],
                    "start_formatted": f"{stop['time_window'][0] // 60:02d}:{stop['time_window'][0] % 60:02d}",
                    "end_formatted": f"{stop['time_window'][1] // 60:02d}:{stop['time_window'][1] % 60:02d}"
                },
                "demand": stop["demand"],
                "load_at_stop": current_load,
                "capacity_utilization_percentage": round((current_load / vehicle_capacity) * 100, 2) if vehicle_capacity > 0 else 0
            }
            route_detail["stops_sequence"].append(stop_detail)
        
        results["detailed_routes"].append(route_detail)

    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logging.error(f"Error al exportar resultados: {e}")
        return False
