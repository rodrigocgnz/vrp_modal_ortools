<#
.SYNOPSIS
    Descarga y preprocesa la red vial de OpenStreetMap para OSRM local en Docker.
.DESCRIPTION
    Descarga el archivo .osm.pbf de Geofabrik (por defecto México completo, o puedes pasar otra URL),
    ejecuta osrm-extract (perfil car.lua), osrm-partition y osrm-customize, y levanta el contenedor.
#>
param(
    [string]$PbfUrl = "https://download.geofabrik.de/north-america/mexico-latest.osm.pbf",
    [string]$DataDir = "osrm-data"
)

$ErrorActionPreference = "Stop"

# Si la terminal de PowerShell estaba abierta antes de instalar Docker, refrescar el PATH automáticamente
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    $MachinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $UserPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$MachinePath;$UserPath"

    $CommonDockerDirs = @(
        "$env:ProgramFiles\Docker\Docker\resources\bin",
        "$env:LocalAppData\Programs\Docker\Docker\resources\bin",
        "C:\Program Files\Docker\Docker\resources\bin"
    )
    foreach ($dir in $CommonDockerDirs) {
        if (Test-Path (Join-Path $dir "docker.exe")) {
            $env:Path = "$dir;$env:Path"
            break
        }
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: Docker Desktop no está instalado o no se encontró docker.exe." -ForegroundColor Red
    Write-Host "1. Ejecuta en PowerShell: winget install -e --id Docker.DockerDesktop" -ForegroundColor Cyan
    Write-Host "2. Abre Docker Desktop, espera a que inicie el motor y vuelve a ejecutar .\setup_osrm.ps1" -ForegroundColor Cyan
    exit 1
}

# Verificar que el motor (daemon) de Docker Desktop esté corriendo
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker está instalado, pero el motor de Docker Desktop aún no está corriendo." -ForegroundColor Yellow
    $DockerDesktopExe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if (Test-Path $DockerDesktopExe) {
        Write-Host "Iniciando Docker Desktop automáticamente (esperando a que el motor arranque)..." -ForegroundColor Cyan
        Start-Process $DockerDesktopExe
        for ($i = 1; $i -le 30; $i++) {
            Start-Sleep -Seconds 3
            docker info *> $null
            if ($LASTEXITCODE -eq 0) {
                Write-Host "¡Motor de Docker Desktop iniciado!" -ForegroundColor Green
                break
            }
            Write-Host "  Esperando a Docker Engine ($($i*3)s)..."
        }
    }
    docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Abre la aplicación 'Docker Desktop' desde el menú Inicio, espera a que diga 'Engine running' y vuelve a ejecutar .\setup_osrm.ps1" -ForegroundColor Red
        exit 1
    }
}

$FullDataDir = Join-Path $PSScriptRoot $DataDir
if (-not (Test-Path $FullDataDir)) {
    New-Item -ItemType Directory -Path $FullDataDir | Out-Null
}

$OsmFile = Join-Path $FullDataDir "region.osm"
$PbfFile = Join-Path $FullDataDir "region.osm.pbf"

# Eliminar archivos corruptos o vacíos (< 10 KB) de intentos fallidos previos
if ((Test-Path $PbfFile) -and ((Get-Item $PbfFile).Length -lt 10240)) {
    Remove-Item $PbfFile -Force
}
if ((Test-Path $OsmFile) -and ((Get-Item $OsmFile).Length -lt 10240)) {
    Remove-Item $OsmFile -Force
}

if (-not (Test-Path $OsmFile)) {
    Write-Host "1/4 Descargando red vial de la región (Mérida / Yucatán) desde OpenStreetMap Overpass API..." -ForegroundColor Cyan
    python -c "
import os, pandas as pd, requests
df = pd.read_csv('data/data.csv').dropna(subset=['Latitud', 'Longitud'])
margin = 0.12
s, n = float(df['Latitud'].min()) - margin, float(df['Latitud'].max()) + margin
w, e = float(df['Longitud'].min()) - margin, float(df['Longitud'].max()) + margin
q = f'[out:xml][timeout:180];(way[highway]({s:.4f},{w:.4f},{n:.4f},{e:.4f});relation[type=restriction]({s:.4f},{w:.4f},{n:.4f},{e:.4f});>;);out meta;'
print(f'  Consultando bbox=({s:.4f}, {w:.4f}, {n:.4f}, {e:.4f})...')
r = requests.post('https://overpass-api.de/api/interpreter', data={'data': q}, headers={'User-Agent': 'VRP-Local-OSRM-Setup/1.0'}, timeout=180)
r.raise_for_status()
with open('osrm-data/region.osm', 'wb') as f:
    f.write(r.content)
print(f'  Guardado osrm-data/region.osm ({len(r.content)/(1024*1024):.2f} MB)')
"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Error descargando la red vial desde Overpass API." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "1/4 Archivo OSM ya existe en $OsmFile (omitiendo descarga)." -ForegroundColor Green
}

Write-Host "2/4 Ejecutando osrm-extract (perfil car.lua)..." -ForegroundColor Cyan
docker run --rm -t -v "${FullDataDir}:/data" ghcr.io/project-osrm/osrm-backend:latest osrm-extract -p /opt/car.lua /data/region.osm

Write-Host "3/4 Ejecutando osrm-partition..." -ForegroundColor Cyan
docker run --rm -t -v "${FullDataDir}:/data" ghcr.io/project-osrm/osrm-backend:latest osrm-partition /data/region.osrm

Write-Host "4/4 Ejecutando osrm-customize..." -ForegroundColor Cyan
docker run --rm -t -v "${FullDataDir}:/data" ghcr.io/project-osrm/osrm-backend:latest osrm-customize /data/region.osrm

Write-Host "Iniciando servidor OSRM local en http://localhost:5000 ..." -ForegroundColor Green
docker compose up -d
Write-Host "¡Listo! OSRM está activo en http://localhost:5000" -ForegroundColor Green

