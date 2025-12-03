#!/usr/bin/env python3
"""
Map Generator for Pickup and Delivery Data
Creates an interactive map visualization of locations from data.csv
"""

import pandas as pd
import folium
from folium import plugins
import json
from datetime import datetime
import os

def load_data(csv_file='data/data.csv'):
    """Load and process the CSV data"""
    try:
        df = pd.read_csv(csv_file)
        print(f"Data loaded successfully: {len(df)} locations")
        return df
    except Exception as e:
        print(f"Error loading data: {e}")
        return None

def create_map(df, output_file='mapa_locations.html'):
    """Create an interactive map with pickup and delivery locations"""
    
    # Calculate center point
    center_lat = df['Latitud'].mean()
    center_lon = df['Longitud'].mean()
    
    # Create base map
    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=12,
        tiles='OpenStreetMap'
    )
    
    # Color mapping for service types
    colors = {
        'Entrega': 'red',
        'Recogida': 'blue'
    }
    
    # Icon mapping for service types
    icons = {
        'Entrega': 'truck',
        'Recogida': 'shopping-cart'
    }
    
    # Add markers for each location
    for idx, row in df.iterrows():
        # Determine color and icon based on service type
        color = colors.get(row['Tipo_Servicio'], 'gray')
        icon = icons.get(row['Tipo_Servicio'], 'info-sign')
        
        # Create popup content
        popup_content = f"""
        <div style="width: 200px;">
            <h4>{row['Nombre']}</h4>
            <p><strong>Tipo:</strong> {row['Tipo_Servicio']}</p>
            <p><strong>ID Viaje:</strong> {row['ID_Viaje']}</p>
            <p><strong>Horario:</strong> {row['Horario_Inicio']} - {row['Horario_Fin']}</p>
            <p><strong>Demanda:</strong> {row['Demanda']}</p>
            <p><strong>Coordenadas:</strong><br>
            Lat: {row['Latitud']:.6f}<br>
            Lon: {row['Longitud']:.6f}</p>
        </div>
        """
        
        # Add marker
        folium.Marker(
            [row['Latitud'], row['Longitud']],
            popup=folium.Popup(popup_content, max_width=250),
            tooltip=f"{row['Nombre']} ({row['Tipo_Servicio']})",
            icon=folium.Icon(
                color=color,
                icon=icon,
                prefix='fa'
            )
        ).add_to(m)
    
    # Add layer control
    folium.LayerControl().add_to(m)
    
    # Add minimap
    minimap = plugins.MiniMap()
    m.add_child(minimap)
    
    # Add fullscreen button
    plugins.Fullscreen().add_to(m)
    
    # Add measure control
    plugins.MeasureControl().add_to(m)
    
    # Save map
    m.save(output_file)
    print(f"Map saved as: {output_file}")
    
    return m

def create_grouped_map(df, output_file='mapa_grouped.html'):
    """Create a map with grouped markers by service type"""
    
    # Calculate center point
    center_lat = df['Latitud'].mean()
    center_lon = df['Longitud'].mean()
    
    # Create base map
    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=12,
        tiles='OpenStreetMap'
    )
    
    # Create feature groups for different service types
    entrega_group = folium.FeatureGroup(name='Entregas (Restaurantes)')
    recogida_group = folium.FeatureGroup(name='Recogidas (Comercios)')
    
    # Add markers to respective groups
    for idx, row in df.iterrows():
        popup_content = f"""
        <div style="width: 200px;">
            <h4>{row['Nombre']}</h4>
            <p><strong>Tipo:</strong> {row['Tipo_Servicio']}</p>
            <p><strong>ID Viaje:</strong> {row['ID_Viaje']}</p>
            <p><strong>Horario:</strong> {row['Horario_Inicio']} - {row['Horario_Fin']}</p>
            <p><strong>Demanda:</strong> {row['Demanda']}</p>
        </div>
        """
        
        if row['Tipo_Servicio'] == 'Entrega':
            folium.Marker(
                [row['Latitud'], row['Longitud']],
                popup=folium.Popup(popup_content, max_width=250),
                tooltip=f"{row['Nombre']} (Entrega)",
                icon=folium.Icon(color='red', icon='truck', prefix='fa')
            ).add_to(entrega_group)
        else:
            folium.Marker(
                [row['Latitud'], row['Longitud']],
                popup=folium.Popup(popup_content, max_width=250),
                tooltip=f"{row['Nombre']} (Recogida)",
                icon=folium.Icon(color='blue', icon='shopping-cart', prefix='fa')
            ).add_to(recogida_group)
    
    # Add feature groups to map
    entrega_group.add_to(m)
    recogida_group.add_to(m)
    
    # Add layer control
    folium.LayerControl().add_to(m)
    
    # Add plugins
    plugins.MiniMap().add_to(m)
    plugins.Fullscreen().add_to(m)
    plugins.MeasureControl().add_to(m)
    
    # Save map
    m.save(output_file)
    print(f"Grouped map saved as: {output_file}")
    
    return m

def create_route_map(df, output_file='mapa_rutas.html'):
    """Create a map showing pickup-delivery pairs as routes"""
    
    # Calculate center point
    center_lat = df['Latitud'].mean()
    center_lon = df['Longitud'].mean()
    
    # Create base map
    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=12,
        tiles='OpenStreetMap'
    )
    
    # Group by ID_Viaje to create routes
    routes = df.groupby('ID_Viaje')
    
    # Color palette for different routes
    colors = ['red', 'blue', 'green', 'purple', 'orange', 'darkred', 
              'lightred', 'beige', 'darkblue', 'darkgreen']
    
    for i, (route_id, route_df) in enumerate(routes):
        color = colors[i % len(colors)]
        
        # Sort by service type (Entrega first, then Recogida)
        route_df = route_df.sort_values('Tipo_Servicio')
        
        # Add markers for this route
        for idx, row in route_df.iterrows():
            icon_type = 'truck' if row['Tipo_Servicio'] == 'Entrega' else 'shopping-cart'
            
            folium.Marker(
                [row['Latitud'], row['Longitud']],
                popup=f"<b>{row['Nombre']}</b><br>Tipo: {row['Tipo_Servicio']}<br>Ruta: {route_id}",
                tooltip=f"Ruta {route_id}: {row['Nombre']}",
                icon=folium.Icon(color=color, icon=icon_type, prefix='fa')
            ).add_to(m)
        
        # Add route line if there are multiple points
        if len(route_df) > 1:
            coordinates = [[row['Latitud'], row['Longitud']] for _, row in route_df.iterrows()]
            folium.PolyLine(
                coordinates,
                color=color,
                weight=3,
                opacity=0.7,
                popup=f"Ruta {route_id}"
            ).add_to(m)
    
    # Add plugins
    plugins.MiniMap().add_to(m)
    plugins.Fullscreen().add_to(m)
    plugins.MeasureControl().add_to(m)
    
    # Save map
    m.save(output_file)
    print(f"Route map saved as: {output_file}")
    
    return m

def main():
    """Main function to generate all map types"""
    print("=== Map Generator for Pickup and Delivery Data ===")
    
    # Load data
    df = load_data()
    if df is None:
        return
    
    # Display data summary
    print(f"\nData Summary:")
    print(f"Total locations: {len(df)}")
    print(f"Service types: {df['Tipo_Servicio'].value_counts().to_dict()}")
    print(f"Number of routes: {df['ID_Viaje'].nunique()}")
    
    # Create timestamp for output files
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Generate different map types
    print("\nGenerating maps...")
    
    # 1. Basic map with all locations
    create_map(df, f'mapa_basico_{timestamp}.html')
    
    # 2. Grouped map by service type
    create_grouped_map(df, f'mapa_grupos_{timestamp}.html')
    
    # 3. Route map showing pickup-delivery pairs
    create_route_map(df, f'mapa_rutas_{timestamp}.html')
    
    print(f"\nAll maps generated successfully!")
    print(f"Open any of the HTML files in your browser to view the maps.")

if __name__ == "__main__":
    main()
