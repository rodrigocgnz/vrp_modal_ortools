import logging
import sys
import time
import threading
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def loading_animation(stop_event):
    """Muestra animación de carga durante la optimización."""
    animation_chars = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
    messages = [
        "Analizando matriz de distancias...",
        "Calculando rutas optimas...",
        "Verificando restricciones de tiempo...",
        "Optimizando secuencia de paradas...",
        "Validando capacidad de vehiculos...",
        "Aplicando algoritmos de busqueda...",
        "Refinando solucion...",
        "Evaluando costos totales..."
    ]
    i, msg_idx = 0, 0

    while not stop_event.is_set():
        sys.stdout.write(f'\r{animation_chars[i]} {messages[msg_idx % len(messages)]}')
        sys.stdout.flush()
        time.sleep(0.1)
        i = (i + 1) % len(animation_chars)
        if i == 0:
            msg_idx += 1

    sys.stdout.write('\r' + ' ' * 80 + '\r')
    sys.stdout.flush()


@dataclass
class VRPConfig:
    """Configuración para problemas VRP.

    Args:
        max_time_per_vehicle: Tiempo máximo por vehículo en segundos (default: 4320 = 1.2h)
        max_wait_time: Tiempo máximo de espera en segundos (default: 300 = 5min)
        relaxed_max_time: Tiempo relajado para problemas pequeños (default: 86400 = 24h)
    """
    max_time_per_vehicle: int = 4320
    max_wait_time: int = 300
    min_locations_for_strict: int = 3
    relaxed_max_time: int = 86400
    search_time_limit: int = 60
    time_limit_per_location: int = 2
    min_search_time: int = 10
    max_search_time: int = 300


class DataValidator:
    @staticmethod
    def validate_vrp_data(data: Dict) -> None:
        required_keys = ["time_matrix", "num_vehicles", "depot"]
        for key in required_keys:
            if key not in data:
                raise ValueError(f"Missing required key: {key}")

        time_matrix = data["time_matrix"]
        if not isinstance(time_matrix, list) or len(time_matrix) == 0:
            raise ValueError("time_matrix must be a non-empty square list")

        matrix_size = len(time_matrix)
        for row in time_matrix:
            if not isinstance(row, list) or len(row) != matrix_size:
                raise ValueError("time_matrix must be square (same number of rows and columns)")

        depot = data["depot"]
        if not (0 <= depot < matrix_size):
            raise ValueError("Depot index out of range of time_matrix")

        if "num_vehicles" in data and (not isinstance(data["num_vehicles"], int) or data["num_vehicles"] < 1):
            raise ValueError("num_vehicles must be a positive integer")

        # Optional consistency checks
        if "demands" in data:
            demands = data["demands"]
            if len(demands) != matrix_size:
                raise ValueError("demands length must match time_matrix size")

        if "vehicle_capacities" in data:
            caps = data["vehicle_capacities"]
            if not isinstance(caps, list) or len(caps) != data.get("num_vehicles", 1):
                raise ValueError("vehicle_capacities must be a list with length == num_vehicles")

        # pickup_delivery_pairs validation
        if "pickup_delivery_pairs" in data:
            for pair in data["pickup_delivery_pairs"]:
                if not all(k in pair for k in ("pickup_index", "delivery_index", "demand")):
                    raise ValueError("Each pickup_delivery_pair must include pickup_index, delivery_index and demand")
                if not (0 <= pair["pickup_index"] < matrix_size) or not (0 <= pair["delivery_index"] < matrix_size):
                    raise ValueError("pickup/delivery indices must be within time_matrix range")


class VRPSolver:
    """Solver optimizado para ruteo de vehículos con configuración dinámica."""

    def __init__(self, data: Dict, config: Optional[VRPConfig] = None):
        # Validar estructura básica de datos al inicio para fallar rápido
        DataValidator.validate_vrp_data(data)

        self.data = data
        self.config = config or VRPConfig()
        self.depot_return_required = data.get("depot_return_required", True)
        self.problem_type = data.get("problem_type", "VRP")

        # Precompute/normalize lookups para callbacks rápidos
        self._build_lookups()
        self._adjust_constraints()

    def _build_lookups(self):
        """Construye estructuras O(1) para demandas y parejas pickup-delivery."""
        matrix_size = len(self.data.get("time_matrix", []))
        # Demands lookup (default zero)
        demands = self.data.get("demands", [0] * matrix_size)
        if len(demands) < matrix_size:
            demands = demands + [0] * (matrix_size - len(demands))
        self._demands_lookup = demands

        # Pickup-delivery map: node -> delta demand (positive for pickups, negative for deliveries)
        self._pd_delta_map = {}
        for pair in self.data.get("pickup_delivery_pairs", []):
            self._pd_delta_map[pair["pickup_index"]] = self._pd_delta_map.get(pair["pickup_index"], 0) + pair.get("demand", 0)
            self._pd_delta_map[pair["delivery_index"]] = self._pd_delta_map.get(pair["delivery_index"], 0) - pair.get("demand", 0)

        # Vehicle capacities normalized list
        num_vehicles = self.data.get("num_vehicles", 1)
        caps = self.data.get("vehicle_capacities", [float('inf')] * num_vehicles)
        if not isinstance(caps, list):
            caps = [caps] * num_vehicles
        if len(caps) < num_vehicles:
            caps = caps + [caps[-1]] * (num_vehicles - len(caps))
        self._vehicle_capacities = caps

    def _adjust_constraints(self):
        """Ajusta restricciones según tamaño del problema."""
        num_locations = len(self.data.get("time_matrix", []))

        if num_locations < self.config.min_locations_for_strict:
            logger.warning(f"Pocas ubicaciones ({num_locations}). Relajando restricciones.")
            self.max_time = self.config.relaxed_max_time
            self.max_wait = self.config.relaxed_max_time
        else:
            self.max_time = self.data.get("max_time_per_vehicle", self.config.max_time_per_vehicle)
            self.max_wait = self.data.get("max_wait_time", self.config.max_wait_time)

    def _create_time_callback(self, manager: pywrapcp.RoutingIndexManager):
        """Crea callback de tiempo incluyendo tiempo de servicio del nodo origen y restricciones de depósito.

        Este callback valida índices y atrapa errores de matriz para dar mensajes útiles.
        """
        service_time = int(self.data.get("service_time", 0))
        depot = self.data["depot"]

        def time_callback(from_index: int, to_index: int) -> int:
            try:
                from_node = manager.IndexToNode(from_index)
                to_node = manager.IndexToNode(to_index)
            except Exception as e:
                logger.exception("Index conversion failed")
                raise

            try:
                travel_time = self.data["time_matrix"][from_node][to_node]
            except IndexError:
                raise ValueError(f"time_matrix index out of range: from {from_node} to {to_node}")

            if not self.depot_return_required and to_node == depot:
                travel_time = 0

            # En OR-Tools el tiempo de servicio en un cliente se suma al salir de from_node
            # para que CumulVar(to_index) refleje la hora real de llegada al siguiente nodo.
            node_service = 0 if from_node == depot else service_time
            return int(travel_time) + node_service

        return time_callback

    def _setup_time_dimension(self, routing: pywrapcp.RoutingModel, transit_callback_index: int,
                              manager: pywrapcp.RoutingIndexManager):
        """Configura dimensión de tiempo con ventanas y optimización."""
        routing.AddDimension(transit_callback_index, self.max_wait, self.max_time, False, "Time")
        time_dimension = routing.GetDimensionOrDie("Time")

        if self.data.get("use_time_windows", True):
            self._apply_time_windows(manager, time_dimension)
        else:
            logger.info("Omitiendo ventanas de tiempo")

        self._setup_depot_times(routing, time_dimension, manager)
        self._minimize_total_time(routing, time_dimension)

        return time_dimension

    def _apply_time_windows(self, manager: pywrapcp.RoutingIndexManager, time_dimension):
        """Aplica ventanas de tiempo según tipo de problema."""
        logger.info("Aplicando restricciones de ventanas de tiempo")

        if self.problem_type == "PDP":
            self._apply_pdp_time_windows(manager, time_dimension)
        else:
            self._apply_standard_time_windows(manager, time_dimension)

    def _apply_standard_time_windows(self, manager: pywrapcp.RoutingIndexManager, time_dimension):
        """Aplica ventanas estándar para VRP/CVRPTW."""
        time_windows = self.data.get("time_windows", [])
        depot = self.data["depot"]

        for location_idx, time_window in enumerate(time_windows):
            if location_idx == depot:
                continue
            index = manager.NodeToIndex(location_idx)
            time_dimension.CumulVar(index).SetRange(time_window[0], time_window[1])

    def _apply_pdp_time_windows(self, manager: pywrapcp.RoutingIndexManager, time_dimension):
        """Aplica ventanas para Pickup-Delivery con precedencia."""
        for pair in self.data.get("pickup_delivery_pairs", []):
            pickup_idx = manager.NodeToIndex(pair["pickup_index"]) 
            delivery_idx = manager.NodeToIndex(pair["delivery_index"])

            time_dimension.CumulVar(pickup_idx).SetRange(
                pair["pickup_time_window"][0], pair["pickup_time_window"][1])
            time_dimension.CumulVar(delivery_idx).SetRange(
                pair["delivery_time_window"][0], pair["delivery_time_window"][1])

    def _setup_depot_times(self, routing: pywrapcp.RoutingModel, time_dimension,
                           manager: pywrapcp.RoutingIndexManager):
        """Configura ventanas de tiempo del depósito para cada vehículo."""
        depot_idx = self.data["depot"]
        use_windows = self.data.get("use_time_windows", True)

        for vehicle_id in range(self.data["num_vehicles"]):
            index = routing.Start(vehicle_id)

            if use_windows and "time_windows" in self.data:
                time_dimension.CumulVar(index).SetRange(
                    self.data["time_windows"][depot_idx][0],
                    self.data["time_windows"][depot_idx][1])
            else:
                time_dimension.CumulVar(index).SetRange(0, self.max_time)

    def _minimize_total_time(self, routing: pywrapcp.RoutingModel, time_dimension):
        """Minimiza tiempo total agregando variables de inicio y fin."""
        for i in range(self.data["num_vehicles"]):
            # Solo minimizar el tiempo de fin, no el de inicio
            # Esto permite que el vehículo salga más tarde del depósito si es necesario
            routing.AddVariableMinimizedByFinalizer(time_dimension.CumulVar(routing.End(i)))

    def _setup_capacity_dimension_standard(self, routing: pywrapcp.RoutingModel,
                                          manager: pywrapcp.RoutingIndexManager):
        """Configura capacidad estándar basada en demandas (lookup O(1))."""
        def demand_callback(from_index: int) -> int:
            node = manager.IndexToNode(from_index)
            return int(self._demands_lookup[node])

        demand_callback_index = routing.RegisterUnaryTransitCallback(demand_callback)
        routing.AddDimensionWithVehicleCapacity(
            demand_callback_index, 0, self._vehicle_capacities, True, "Capacity")

    def _setup_capacity_dimension_pdp(self, routing: pywrapcp.RoutingModel,
                                     manager: pywrapcp.RoutingIndexManager):
        """Configura capacidad para Pickup-Delivery usando mapa precomputado O(1)."""
        def capacity_callback(from_index: int) -> int:
            node = manager.IndexToNode(from_index)
            return int(self._pd_delta_map.get(node, 0))

        capacity_callback_index = routing.RegisterUnaryTransitCallback(capacity_callback)
        routing.AddDimensionWithVehicleCapacity(
            capacity_callback_index, 0, self._vehicle_capacities, True, "Capacity")

    def _setup_pickup_delivery_constraints(self, routing: pywrapcp.RoutingModel,
                                          manager: pywrapcp.RoutingIndexManager):
        """Configura precedencia y mismo vehículo para pares pickup-delivery."""
        logger.info("Configurando restricciones pickup-delivery")

        for pair in self.data.get("pickup_delivery_pairs", []):
            pickup_idx = manager.NodeToIndex(pair["pickup_index"])
            delivery_idx = manager.NodeToIndex(pair["delivery_index"])

            routing.AddPickupAndDelivery(pickup_idx, delivery_idx)
            routing.solver().Add(
                routing.VehicleVar(pickup_idx) == routing.VehicleVar(delivery_idx))

    def _calculate_dynamic_time_limit(self) -> int:
        """Calcula tiempo límite basado en complejidad del problema."""
        num_locations = len(self.data.get("time_matrix", []))
        num_vehicles = self.data.get("num_vehicles", 1)

        base_time = self.config.search_time_limit
        additional_time = num_locations * self.config.time_limit_per_location

        if self.problem_type == "PDP":
            additional_time = int(additional_time * 1.5)

        vehicle_factor = 1 + (num_vehicles - 1) * 0.1
        total_time = int((base_time + additional_time) * vehicle_factor)
        total_time = max(self.config.min_search_time, min(total_time, self.config.max_search_time))

        logger.info(f"Tiempo límite: {total_time}s para {num_locations} ubicaciones")
        return total_time

    def _get_optimal_first_solution_strategy(self):
        """Selecciona estrategia inicial óptima según características del problema."""
        num_locations = len(self.data.get("time_matrix", []))
        has_time_windows = self.data.get("use_time_windows", True)

        if num_locations <= 10:
            strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
            name = "PATH_CHEAPEST_ARC"
        elif self.problem_type == "PDP":
            strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
            name = "PARALLEL_CHEAPEST_INSERTION"
        elif has_time_windows and num_locations > 20:
            strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
            name = "PARALLEL_CHEAPEST_INSERTION"
        elif num_locations > 30:
            strategy = routing_enums_pb2.FirstSolutionStrategy.GLOBAL_CHEAPEST_ARC
            name = "GLOBAL_CHEAPEST_ARC"
        else:
            strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
            name = "PATH_CHEAPEST_ARC"

        logger.info(f"Estrategia: {name}")
        return strategy

    def _get_optimal_local_search(self):
        """Selecciona metaheurística según tamaño del problema."""
        num_locations = len(self.data.get("time_matrix", []))

        if num_locations <= 15:
            metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.AUTOMATIC
            name = "AUTOMATIC"
        elif num_locations <= 50:
            metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
            name = "GUIDED_LOCAL_SEARCH"
        else:
            metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.SIMULATED_ANNEALING
            name = "SIMULATED_ANNEALING"

        logger.info(f"Metaheurística: {name}")
        return metaheuristic

    def _get_search_parameters(self) -> pywrapcp.DefaultRoutingSearchParameters:
        """Configura parámetros de búsqueda optimizados dinámicamente."""
        search_params = pywrapcp.DefaultRoutingSearchParameters()
        search_params.first_solution_strategy = self._get_optimal_first_solution_strategy()
        search_params.local_search_metaheuristic = self._get_optimal_local_search()
        search_params.time_limit.FromSeconds(self._calculate_dynamic_time_limit())
        search_params.log_search = False
        return search_params

    def _solve_with_animation(self, routing: pywrapcp.RoutingModel, search_params, loading_animation):
        """Resuelve con animación visual del progreso.

        Se crea un thread no-daemon y se asegura su cierre apropiado.
        """
        logger.info(f"Iniciando optimización {self.problem_type}")

        loading_stop = threading.Event()
        loading_thread = threading.Thread(target=loading_animation, args=(loading_stop,))
        # No marcar como daemon: cerramos explícitamente
        loading_thread.start()

        try:
            solution = routing.SolveWithParameters(search_params)
        finally:
            loading_stop.set()
            loading_thread.join(timeout=5)
            if loading_thread.is_alive():
                logger.warning("El hilo de animación no terminó a tiempo; continúa shutdown")

        return solution

    def solve(self, loading_animation=None) -> Tuple:
        """Resuelve el problema VRP completo y retorna manager, routing y solución.

        Esta función actúa como orquestador. Si los datos son inválidos, ya falló en el init.
        """
        manager = pywrapcp.RoutingIndexManager(
            len(self.data["time_matrix"]),
            self.data["num_vehicles"],
            self.data["depot"])

        routing = pywrapcp.RoutingModel(manager)

        time_callback = self._create_time_callback(manager)
        transit_callback_index = routing.RegisterTransitCallback(time_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

        self._setup_time_dimension(routing, transit_callback_index, manager)

        if self.problem_type == "PDP":
            self._setup_capacity_dimension_pdp(routing, manager)
            self._setup_pickup_delivery_constraints(routing, manager)
        else:
            self._setup_capacity_dimension_standard(routing, manager)

        search_params = self._get_search_parameters()

        if loading_animation:
            solution = self._solve_with_animation(routing, search_params, loading_animation)
        else:
            logger.info(f"Iniciando optimización {self.problem_type}")
            solution = routing.SolveWithParameters(search_params)

        return manager, routing, solution


def solve_vrp(data: Dict, loading_animation=None) -> Tuple:
    """Función de conveniencia para compatibilidad con código existente."""
    solver = VRPSolver(data)
    return solver.solve(loading_animation)
