import argparse
import datetime
import logging
import os
import subprocess
import sys
from typing import Optional

from src.data_loader import create_data_model
from src.solver import solve_vrp
from src.reporting import extract_routes_from_solution, export_results
from src.visualization import create_map_visualization


logging.basicConfig(level=logging.INFO)

SEPARATOR = "=" * 60


def check_and_generate_matrix(
    json_path: str,
    service_choice: Optional[str] = None,
    force_regenerate: bool = False,
) -> None:
    """
    Verifica si el archivo de matriz existe.
    Si no existe (o force_regenerate es True), genera la matriz usando OSRM local, ORS o HERE.
    
    Args:
        json_path: Ruta al archivo JSON de la matriz.
        service_choice: "osrm", "ors" o "here". Si None, se pedirá al usuario.
        force_regenerate: Si True, recalcula la matriz aunque el archivo ya exista.
    """
    if os.path.exists(json_path) and not force_regenerate:
        return

    if not os.path.exists(json_path):
        logging.warning(f"El archivo de datos '{json_path}' no existe.")
    else:
        logging.info(f"Regenerando matriz en '{json_path}'...")

    if not service_choice:
        choice = input("Ingresa 1 para OSRM Local (Docker), 2 para ORS o 3 para HERE [1]: ").strip() or "1"

        if choice == "1":
            service_choice = "osrm"
        elif choice == "2":
            service_choice = "ors"
        elif choice == "3":
            service_choice = "here"
        else:
            logging.error("Opción inválida.")
            sys.exit(1)

    script_map = {
        "osrm": "src/generate_osrm_matrix.py",
        "ors": "src/generate_ors_matrix.py",
        "here": "src/generate_here_matrix.py",
    }

    script_to_run = script_map.get(service_choice.lower())
    if not script_to_run or not os.path.exists(script_to_run):
        logging.error(f"No se encontró el script '{script_to_run}'.")
        sys.exit(1)

    logging.info(f"\nEjecutando el script de generación: '{script_to_run}'...")
    cmd = [sys.executable, script_to_run]
    if service_choice.lower() == "osrm":
        cmd.extend(["--output_json", json_path])
    env = os.environ.copy()
    env["PYTHONPATH"] = os.getcwd()

    try:
        subprocess.run(cmd, check=True, env=env)
        logging.info("Script de generación completado.")
    except subprocess.CalledProcessError as e:
        logging.error(f"Error al ejecutar el script '{script_to_run}': {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        logging.warning("Proceso de generación cancelado por el usuario.")
        sys.exit(1)


def prepare_output_dir() -> str:
    """Crea un directorio de resultados con timestamp y lo retorna."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    output_dir = os.path.join("results", timestamp)
    os.makedirs(output_dir, exist_ok=True)
    logging.info(f"\nLos resultados se guardarán en: '{output_dir}'")
    return output_dir


def print_summary(routes: list, data: dict, output_dir: str, map_filename: str, export_filename: str) -> None:
    """Imprime un resumen de los resultados encontrados."""
    logging.info(f"Vehículos utilizados: {len(routes)} de {data['num_vehicles']} disponibles")
    logging.info("Archivos generados:")
    logging.info(f"{map_filename} (Mapa interactivo)")
    logging.info(f"{export_filename} (Resultados detallados)")



def run_solver(data: dict, output_dir: str) -> None:
    """Ejecuta el solver VRP y genera resultados."""
    manager, routing, solution = solve_vrp(data)

    if not solution:
        logging.error("No se pudo encontrar una solución para el problema VRP.")
        return

    logging.info("¡Solución encontrada!")
    routes = extract_routes_from_solution(data, manager, routing, solution)

    if not routes:
        logging.warning("No se encontraron rutas válidas en la solución.")
        return

    logging.info(f"Se encontraron {len(routes)} rutas:")
    for route in routes:
        hours, minutes = divmod(route["total_time"], 60)
        display_id = route["vehicle_id"] + 1
        logging.info(
            f"Vehículo {display_id}: {len(route['stops']) - 2} paradas, "
            f"tiempo: {hours:02d}:{minutes:02d}, carga: {route['total_load']}"
        )

    # Definir rutas de salida
    export_filename = os.path.join(output_dir, "resultados_rutas_detallados.json")
    map_filename = os.path.join(output_dir, "rutas_twcvrp_mapa.html")

    # Exportar resultados
    export_results(routes, data, export_filename)
    create_map_visualization(routes, data, map_filename)

    # Resumen final
    print_summary(routes, data, output_dir, map_filename, export_filename)



def main() -> None:
    """Punto de entrada principal para ejecutar el optimizador VRP."""
    parser = argparse.ArgumentParser(
        description="Resuelve un problema de ruteo de vehículos (VRP) a partir de un archivo de matriz pre-calculado."
    )
    parser.add_argument(
        "--json_path",
        type=str,
        default="results/resultados_matrices.json",
        help="Ruta al archivo JSON con la matriz de distancias/tiempos y otros datos del problema."
    )
    parser.add_argument(
        "--service",
        type=str,
        choices=["osrm", "ors", "here"],
        default="osrm",
        help="Servicio a usar para generar la matriz (por defecto: osrm local en Docker)."
    )
    parser.add_argument(
        "--force_matrix",
        action="store_true",
        help="Fuerza el recálculo de la matriz aunque el archivo JSON ya exista."
    )
    args = parser.parse_args()

    # Validar archivo de matriz
    check_and_generate_matrix(args.json_path, args.service, force_regenerate=args.force_matrix)

    # Directorio de salida
    output_dir = prepare_output_dir()

    # Cargar datos
    data = create_data_model(args.json_path)
    if data is None:
        logging.error("Finalizando ejecución por errores en la carga de datos.")
        return

    # Resolver y exportar
    run_solver(data, output_dir)


if __name__ == "__main__":
    main()
