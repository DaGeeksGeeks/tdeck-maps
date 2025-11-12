#!/usr/bin/env python3
"""
Meshtastic Map Tile Generator for T-Deck
Generates map tiles from various sources for offline use
"""

import os
import sys
import math
import time
import requests
from PIL import Image, ImageDraw, ImageFont
import argparse
from pathlib import Path
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

class CityLookup:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'MeshtasticTileGenerator/1.0'
        })
    
    def get_coordinates(self, city, state=None, country=None):
        """Get coordinates using OpenStreetMap Nominatim (free)"""
        base_url = "https://nominatim.openstreetmap.org/search"
        
        # Build query
        query = city
        if state:
            query += f", {state}"
        if country:
            query += f", {country}"
        
        params = {
            'q': query,
            'format': 'json',
            'limit': 1,
            'addressdetails': 1
        }
        
        try:
            response = self.session.get(base_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if not data:
                return None
            
            result = data[0]
            return {
                'name': result.get('display_name', 'Unknown'),
                'lat': float(result['lat']),
                'lon': float(result['lon']),
                'type': result.get('type', 'unknown')
            }
            
        except Exception as e:
            print(f"Error looking up coordinates for {query}: {e}")
            return None
    
    def get_bounding_box_for_cities(self, cities, buffer_km=10):
        """Get bounding box for multiple cities with buffer in kilometers"""
        all_coords = []
        
        print(f"Looking up coordinates for {len(cities)} cities...")
        for city_info in cities:
            if isinstance(city_info, str):
                city, state, country = city_info, None, None
            else:
                city = city_info.get('city')
                state = city_info.get('state')
                country = city_info.get('country')
            
            result = self.get_coordinates(city, state, country)
            if result:
                all_coords.append(result)
                print(f"✓ {city}: {result['lat']:.4f}, {result['lon']:.4f}")
            else:
                print(f"✗ {city}: Not found")
        
        if not all_coords:
            print("No valid coordinates found")
            return None
        
        # Calculate bounding box
        lats = [c['lat'] for c in all_coords]
        lons = [c['lon'] for c in all_coords]
        
        # Convert km buffer to degrees (approximate)
        buffer_deg = buffer_km / 111.0  # ~111km per degree
        
        north = max(lats) + buffer_deg
        south = min(lats) - buffer_deg
        east = max(lons) + buffer_deg
        west = min(lons) - buffer_deg
        
        print(f"\n📦 Bounding box for {len(all_coords)} cities (±{buffer_km}km buffer):")
        print(f"   North: {north:.4f}")
        print(f"   South: {south:.4f}")
        print(f"   East:  {east:.4f}")
        print(f"   West:  {west:.4f}")
        
        return {
            'north': north,
            'south': south,
            'east': east,
            'west': west,
            'cities': all_coords
        }

class MeshtasticTileGenerator:
    def __init__(self, output_dir="tiles", tile_size=256, delay=0.1):
        self.output_dir = Path(output_dir)
        self.tile_size = tile_size
        self.delay = delay  # Delay between requests to be respectful
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'MeshtasticTileGenerator/1.0'
        })
        
        # Create output directory
        self.output_dir.mkdir(exist_ok=True)
        
    def deg2num(self, lat_deg, lon_deg, zoom):
        """Convert lat/lon to tile numbers"""
        lat_rad = math.radians(lat_deg)
        n = 2.0 ** zoom
        x = int((lon_deg + 180.0) / 360.0 * n)
        y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
        return (x, y)
    
    def num2deg(self, x, y, zoom):
        """Convert tile numbers to lat/lon"""
        n = 2.0 ** zoom
        lon_deg = x / n * 360.0 - 180.0
        lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * y / n)))
        lat_deg = math.degrees(lat_rad)
        return (lat_deg, lon_deg)
    
    def get_tile_url(self, x, y, zoom, source="osm"):
        """Get tile URL for different map sources"""
        sources = {
            "osm": f"https://localosm.dggs.cloud/tile/{zoom}/{x}/{y}.png",
            "real-osm": f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png",
            "satellite": f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{zoom}/{y}/{x}",
            "terrain": f"https://tile.opentopomap.org/{zoom}/{x}/{y}.png",
            "cycle": f"https://tile.thunderforest.com/cycle/{zoom}/{x}/{y}.png"
        }
        return sources.get(source, sources["osm"])
    
    def download_tile(self, x, y, zoom, source="osm"):
        """Download a single tile"""
        url = self.get_tile_url(x, y, zoom, source)
        
        # Create directory structure
        tile_dir = self.output_dir / str(zoom) / str(x)
        tile_dir.mkdir(parents=True, exist_ok=True)
        
        tile_path = tile_dir / f"{y}.png"
        
        # Skip if tile already exists
        if tile_path.exists():
            return tile_path, True
        
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            
            # Save the tile
            with open(tile_path, 'wb') as f:
                f.write(response.content)
            
            time.sleep(self.delay)  # Be respectful to tile servers
            return tile_path, True
            
        except Exception as e:
            print(f"Error downloading tile {x},{y},{zoom}: {e}")
            return None, False
    
    def generate_tiles(self, north, south, east, west, min_zoom=8, max_zoom=16, source="osm", max_workers=4):
        """Generate tiles for a bounding box"""
        print(f"Generating tiles for bounds: N:{north}, S:{south}, E:{east}, W:{west}")
        print(f"Zoom levels: {min_zoom} to {max_zoom}")
        print(f"Source: {source}")
        
        # Validate coordinates
        if north <= south:
            print("Error: North latitude must be greater than south latitude")
            return
        if east <= west:
            print("Error: East longitude must be greater than west longitude")
            return
        
        total_tiles = 0
        downloaded_tiles = 0
        
        # Calculate total tiles for progress tracking
        for zoom in range(min_zoom, max_zoom + 1):
            # Calculate tile boundaries correctly
            x_min, y_max = self.deg2num(south, west, zoom)  # Bottom-left
            x_max, y_min = self.deg2num(north, east, zoom)  # Top-right
            
            # Ensure proper ordering
            if x_min > x_max:
                x_min, x_max = x_max, x_min
            if y_min > y_max:
                y_min, y_max = y_max, y_min
            
            tiles_this_zoom = (x_max - x_min + 1) * (y_max - y_min + 1)
            total_tiles += tiles_this_zoom
            print(f"Zoom {zoom}: {tiles_this_zoom} tiles (x:{x_min}-{x_max}, y:{y_min}-{y_max})")
        
        print(f"Total tiles to process: {total_tiles}")
        
        if total_tiles == 0:
            print("No tiles to download. Check your coordinates.")
            return
        
        # Download tiles with threading
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            
            for zoom in range(min_zoom, max_zoom + 1):
                # Calculate tile boundaries correctly
                x_min, y_max = self.deg2num(south, west, zoom)  # Bottom-left
                x_max, y_min = self.deg2num(north, east, zoom)  # Top-right
                
                # Ensure proper ordering
                if x_min > x_max:
                    x_min, x_max = x_max, x_min
                if y_min > y_max:
                    y_min, y_max = y_max, y_min
                
                print(f"Processing zoom level {zoom} (x:{x_min}-{x_max}, y:{y_min}-{y_max})...")
                
                for x in range(x_min, x_max + 1):
                    for y in range(y_min, y_max + 1):
                        future = executor.submit(self.download_tile, x, y, zoom, source)
                        futures.append(future)
            
            # Process completed downloads
            for future in as_completed(futures):
                tile_path, success = future.result()
                if success:
                    downloaded_tiles += 1
                
                if downloaded_tiles % 100 == 0:
                    print(f"Downloaded {downloaded_tiles}/{total_tiles} tiles")
        
        print(f"Completed! Downloaded {downloaded_tiles}/{total_tiles} tiles")
        
        # Generate metadata
        self.generate_metadata(north, south, east, west, min_zoom, max_zoom, source)
    
    def generate_metadata(self, north, south, east, west, min_zoom, max_zoom, source):
        """Generate metadata file for Meshtastic"""
        metadata = {
            "name": f"Generated tiles ({source})",
            "description": f"Map tiles for Meshtastic T-Deck",
            "bounds": [west, south, east, north],
            "minzoom": min_zoom,
            "maxzoom": max_zoom,
            "format": "png",
            "type": "baselayer",
            "source": source,
            "generated": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        
        metadata_path = self.output_dir / "metadata.json"
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)
        
        print(f"Metadata saved to: {metadata_path}")
    
    def create_sample_tile(self, text="Sample Tile"):
        """Create a sample tile for testing"""
        img = Image.new('RGB', (self.tile_size, self.tile_size), color='lightblue')
        draw = ImageDraw.Draw(img)
        
        # Try to use a font, fallback to default
        try:
            font = ImageFont.truetype("arial.ttf", 20)
        except:
            font = ImageFont.load_default()
        
        # Draw text in center
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        
        x = (self.tile_size - text_width) // 2
        y = (self.tile_size - text_height) // 2
        
        draw.text((x, y), text, fill='black', font=font)
        
        # Save sample tile
        sample_dir = self.output_dir / "sample"
        sample_dir.mkdir(exist_ok=True)
        img.save(sample_dir / "sample.png")
        print(f"Sample tile saved to: {sample_dir / 'sample.png'}")

def get_region_bounds(region):
    """Get predefined bounds for common regions"""
    regions = {
        # =========================
        # UNITED KINGDOM — NATIONAL
        # =========================
        'united_kingdom': {'north': 60.9, 'south': 49.8, 'east': 1.8, 'west': -8.7},

        # -------------------------
        # UK — COUNTRIES
        # -------------------------
        'england':  {'north': 55.9, 'south': 49.9, 'east': 1.8,  'west': -6.4},
        'scotland': {'north': 60.9, 'south': 54.5, 'east': -0.1, 'west': -8.7},
        'wales':    {'north': 53.5, 'south': 51.3, 'east': -2.6, 'west': -5.5},
        'northern_ireland': {'north': 55.3, 'south': 54.0, 'east': -5.4, 'west': -8.2},

        # -------------------------
        # UK — ENGLISH REGIONS (NUTS1-style)
        # -------------------------
        'north_east_england':      {'north': 55.8, 'south': 54.5, 'east': -0.8, 'west': -2.7},
        'north_west_england':      {'north': 55.2, 'south': 53.3, 'east': -1.9, 'west': -3.6},
        'yorks_humber':            {'north': 54.5, 'south': 53.2, 'east': 0.1,  'west': -2.6},
        'east_midlands':           {'north': 53.6, 'south': 52.7, 'east': 0.3,  'west': -2.2},
        'west_midlands_region':    {'north': 53.2, 'south': 52.2, 'east': -1.4, 'west': -3.1},
        'east_of_england':         {'north': 52.9, 'south': 51.4, 'east': 1.8,  'west': -0.7},
        'south_east_england':      {'north': 52.2, 'south': 50.7, 'east': 1.8,  'west': -2.0},
        'south_west_england':      {'north': 51.7, 'south': 49.9, 'east': -1.6, 'west': -6.4},
        'greater_london_region':   {'north': 51.72,'south': 51.25,'east': 0.33, 'west': -0.55},

        # -------------------------
        # UK — MAJOR METROPOLITAN AREAS
        # -------------------------
        'london':               {'north': 51.72, 'south': 51.25, 'east': 0.33,  'west': -0.55},
        'greater_manchester':   {'north': 53.65, 'south': 53.35, 'east': -2.10, 'west': -2.55},
        'birmingham_west_mids': {'north': 52.60, 'south': 52.35, 'east': -1.70, 'west': -2.10},
        'liverpool_merseyside': {'north': 53.50, 'south': 53.30, 'east': -2.80, 'west': -3.10},
        'leeds_west_yorkshire': {'north': 53.90, 'south': 53.70, 'east': -1.35, 'west': -1.70},
        'sheffield_s_yorkshire': {'north': 53.45, 'south': 53.30, 'east': -1.35, 'west': -1.60},
        'newcastle_tyneside':   {'north': 55.05, 'south': 54.90, 'east': -1.45, 'west': -1.75},
        'nottingham':           {'north': 53.00, 'south': 52.85, 'east': -1.05, 'west': -1.25},
        'bristol':              {'north': 51.55, 'south': 51.35, 'east': -2.45, 'west': -2.75},
        'cardiff':              {'north': 51.55, 'south': 51.40, 'east': -3.10, 'west': -3.35},
        'glasgow':              {'north': 55.95, 'south': 55.75, 'east': -4.05, 'west': -4.45},
        'edinburgh':            {'north': 55.98, 'south': 55.86, 'east': -3.10, 'west': -3.35},
        'belfast':              {'north': 54.65, 'south': 54.50, 'east': -5.80, 'west': -6.10},
        'cambridge':            {'north': 52.25, 'south': 52.15, 'east': 0.20,  'west': 0.00},
        'oxford':               {'north': 51.80, 'south': 51.70, 'east': -1.20, 'west': -1.35},

        # -------------------------
        # UK — COUNTIES (starter set; extend as needed)
        # (Ceremonial/unitary mix chosen for practicality in bounding boxes)
        # -------------------------
        'greater_london':   {'north': 51.72, 'south': 51.25, 'east': 0.33,  'west': -0.55},
        'kent':             {'north': 51.50, 'south': 50.90, 'east': 1.45,  'west': 0.15},
        'essex':            {'north': 52.10, 'south': 51.50, 'east': 1.30,  'west': 0.15},
        'surrey':           {'north': 51.45, 'south': 51.10, 'east': -0.10, 'west': -0.90},
        'hampshire':        {'north': 51.40, 'south': 50.70, 'east': -0.70, 'west': -1.90},
        'berkshire':        {'north': 51.55, 'south': 51.30, 'east': -0.45, 'west': -1.60},
        'oxfordshire':      {'north': 52.20, 'south': 51.45, 'east': -0.90, 'west': -1.90},
        'buckinghamshire':  {'north': 52.15, 'south': 51.48, 'east': -0.45, 'west': -1.20},
        'hertfordshire':    {'north': 52.10, 'south': 51.60, 'east': 0.05,  'west': -0.80},
        'cambridgeshire':   {'north': 52.60, 'south': 52.00, 'east': 0.55,  'west': -0.45},
        'norfolk':          {'north': 53.30, 'south': 52.35, 'east': 1.75,  'west': 0.20},
        'suffolk':          {'north': 52.60, 'south': 51.90, 'east': 1.80,  'west': 0.30},
        'lincolnshire':     {'north': 54.10, 'south': 52.70, 'east': 0.40,  'west': -0.90},
        'north_yorkshire':  {'north': 54.50, 'south': 53.70, 'east': -0.40, 'west': -2.60},
        'west_yorkshire':   {'north': 53.95, 'south': 53.63, 'east': -1.20, 'west': -1.95},
        'south_yorkshire':  {'north': 53.60, 'south': 53.25, 'east': -1.00, 'west': -1.85},
        'lancashire':       {'north': 54.25, 'south': 53.45, 'east': -2.15, 'west': -3.20},
        'merseyside':       {'north': 53.60, 'south': 53.25, 'east': -2.70, 'west': -3.25},
        'cheshire':         {'north': 53.45, 'south': 52.95, 'east': -2.20, 'west': -3.10},
        'greater_manchester_county': {'north': 53.65, 'south': 53.35, 'east': -2.05, 'west': -2.60},
        'derbyshire':       {'north': 53.55, 'south': 52.75, 'east': -1.25, 'west': -2.15},
        'nottinghamshire':  {'north': 53.45, 'south': 52.85, 'east': -0.75, 'west': -1.45},
        'leicestershire':   {'north': 52.90, 'south': 52.45, 'east': -0.60, 'west': -1.55},
        'staffordshire':    {'north': 53.35, 'south': 52.45, 'east': -1.45, 'west': -2.30},
        'warwickshire':     {'north': 52.60, 'south': 52.10, 'east': -1.10, 'west': -1.90},
        'gloucestershire':  {'north': 52.15, 'south': 51.55, 'east': -1.55, 'west': -2.60},
        'somerset':         {'north': 51.45, 'south': 50.90, 'east': -2.25, 'west': -3.75},
        'dorset':           {'north': 51.05, 'south': 50.55, 'east': -1.65, 'west': -2.95},
        'devon':            {'north': 51.25, 'south': 50.20, 'east': -2.90, 'west': -4.70},
        'cornwall':         {'north': 50.75, 'south': 49.95, 'east': -4.35, 'west': -5.75},
        'bristol_unitary':  {'north': 51.55, 'south': 51.40, 'east': -2.45, 'west': -2.70},
        'tyne_and_wear':    {'north': 55.10, 'south': 54.85, 'east': -1.35, 'west': -1.80},
        'county_durham':    {'north': 54.95, 'south': 54.45, 'east': -1.30, 'west': -2.35},
        'northumberland':   {'north': 55.80, 'south': 54.80, 'east': -1.30, 'west': -2.50},
        'powys':            {'north': 52.95, 'south': 51.80, 'east': -2.90, 'west': -3.90},
        'gwynedd':          {'north': 53.30, 'south': 52.55, 'east': -3.50, 'west': -4.80},

        # =========================
        # UNITED STATES — NATIONAL
        # =========================
        'united_states': {'north': 49.4, 'south': 24.5, 'east': -66.9, 'west': -124.8},  # CONUS-ish

        # -------------------------
        # US — STATES (starter set; extend to all 50 + DC)
        # -------------------------
        'california': {'north': 42.0, 'south': 32.5, 'east': -114.13, 'west': -124.41},
        'texas':      {'north': 36.5, 'south': 25.8, 'east': -93.5,  'west': -106.6},
        'florida':    {'north': 31.1, 'south': 24.4, 'east': -80.0,  'west': -87.7},
        'new_york':   {'north': 45.0, 'south': 40.5, 'east': -71.8,  'west': -79.8},
        'illinois':   {'north': 42.5, 'south': 36.9, 'east': -87.4,  'west': -91.5},
        'pennsylvania': {'north': 42.5, 'south': 39.7, 'east': -74.7, 'west': -80.6},
        'ohio':       {'north': 41.9, 'south': 38.4, 'east': -80.5,  'west': -84.9},
        'georgia':    {'north': 35.0, 'south': 30.4, 'east': -80.8,  'west': -85.6},
        'north_carolina': {'north': 36.6, 'south': 33.8, 'east': -75.4, 'west': -84.3},
        'michigan':   {'north': 48.3, 'south': 41.7, 'east': -82.1,  'west': -90.4},
        'washington': {'north': 49.1, 'south': 45.5, 'east': -116.9, 'west': -124.9},
        'arizona':    {'north': 37.1, 'south': 31.2, 'east': -109.0, 'west': -114.9},
        'colorado':   {'north': 41.1, 'south': 36.9, 'east': -102.0, 'west': -109.1},
        'massachusetts': {'north': 42.9, 'south': 41.2, 'east': -69.9, 'west': -73.6},
        'new_jersey': {'north': 41.4, 'south': 38.9, 'east': -73.9,  'west': -75.6},
        'dc':         {'north': 38.995, 'south': 38.79, 'east': -76.91, 'west': -77.12},

        # -------------------------
        # US — MAJOR METROPOLITAN AREAS (starter set)
        # -------------------------
        'new_york_city':     {'north': 41.20, 'south': 40.40, 'east': -73.55, 'west': -74.30},
        'los_angeles':       {'north': 34.40, 'south': 33.65, 'east': -118.00,'west': -118.90},
        'chicago':           {'north': 42.10, 'south': 41.55, 'east': -87.40, 'west': -87.95},
        'dallas_fort_worth': {'north': 33.25, 'south': 32.45, 'east': -96.50, 'west': -97.50},
        'houston':           {'north': 30.20, 'south': 29.45, 'east': -95.00, 'west': -95.90},
        'atlanta':           {'north': 34.10, 'south': 33.45, 'east': -84.10, 'west': -84.70},
        'miami':             {'north': 25.99, 'south': 25.40, 'east': -80.00, 'west': -80.40},
        'washington_dc':     {'north': 39.05, 'south': 38.70, 'east': -76.80, 'west': -77.35},
        'san_francisco_bay': {'north': 38.35, 'south': 37.10, 'east': -121.50,'west': -123.00},
        'seattle':           {'north': 47.85, 'south': 47.35, 'east': -122.00,'west': -122.55},
        'boston':            {'north': 42.55, 'south': 42.15, 'east': -70.95, 'west': -71.25},
        'phoenix':           {'north': 33.80, 'south': 33.15, 'east': -111.60,'west': -112.45},
        'philadelphia':      {'north': 40.20, 'south': 39.80, 'east': -74.90, 'west': -75.35},
        'detroit':           {'north': 42.55, 'south': 42.10, 'east': -82.85, 'west': -83.30},
        'minneapolis_st_paul': {'north': 45.25, 'south': 44.75, 'east': -92.80, 'west': -93.60},
        'denver':            {'north': 39.95, 'south': 39.55, 'east': -104.70,'west': -105.10},
        'san_diego':         {'north': 33.15, 'south': 32.50, 'east': -116.90,'west': -117.30}
    }
    return regions.get(region.lower())

def main():
    parser = argparse.ArgumentParser(description='Generate map tiles for Meshtastic T-Deck')
    
    # Method selection (mutually exclusive)
    method_group = parser.add_mutually_exclusive_group(required=True)
    method_group.add_argument('--region', type=str, 
                        choices=['north_america', 'usa', 'canada', 'mexico', 'california', 'texas', 'alaska'],
                        help='Predefined region')
    method_group.add_argument('--city', type=str, help='City name (e.g., "San Francisco" or "Portland, Oregon")')
    method_group.add_argument('--cities', type=str, help='Multiple cities separated by semicolons (e.g., "San Francisco; Oakland; San Jose")')
    method_group.add_argument('--coords', action='store_true', help='Use custom coordinates (requires --north, --south, --east, --west)')
    
    # City options
    parser.add_argument('--buffer', type=int, default=20, help='Buffer around city/cities in kilometers (default: 20)')
    
    # Custom coordinates (only used with --coords)
    parser.add_argument('--north', type=float, help='North latitude (required with --coords)')
    parser.add_argument('--south', type=float, help='South latitude (required with --coords)')
    parser.add_argument('--east', type=float, help='East longitude (required with --coords)')
    parser.add_argument('--west', type=float, help='West longitude (required with --coords)')
    
    # Tile generation options
    parser.add_argument('--min-zoom', type=int, default=8, help='Minimum zoom level')
    parser.add_argument('--max-zoom', type=int, default=12, help='Maximum zoom level')
    parser.add_argument('--source', default='osm', choices=['osm', 'satellite', 'terrain', 'cycle'],
                        help='Map source')
    parser.add_argument('--output-dir', default='tiles', help='Output directory')
    parser.add_argument('--delay', type=float, default=0.2, help='Delay between requests (seconds)')
    parser.add_argument('--max-workers', type=int, default=3, help='Maximum concurrent downloads')
    parser.add_argument('--sample-only', action='store_true', help='Generate sample tile only')
    
    args = parser.parse_args()
    
    # Create generator
    generator = MeshtasticTileGenerator(
        output_dir=args.output_dir,
        delay=args.delay
    )
    
    if args.sample_only:
        generator.create_sample_tile()
        return
    
    # Determine coordinates based on method
    north = south = east = west = None
    area_name = "unknown"
    
    if args.region:
        # Use predefined region
        bounds = get_region_bounds(args.region)
        if not bounds:
            print(f"Unknown region: {args.region}")
            return
        north, south, east, west = bounds['north'], bounds['south'], bounds['east'], bounds['west']
        area_name = args.region
        
    elif args.city:
        # Single city lookup
        lookup = CityLookup()
        coord = lookup.get_coordinates(args.city)
        if not coord:
            print(f"Could not find coordinates for: {args.city}")
            return
        
        print(f"Found {args.city}: {coord['lat']:.4f}, {coord['lon']:.4f}")
        
        # Create bounding box around city
        buffer_deg = args.buffer / 111.0  # Convert km to degrees
        north = coord['lat'] + buffer_deg
        south = coord['lat'] - buffer_deg
        east = coord['lon'] + buffer_deg
        west = coord['lon'] - buffer_deg
        area_name = args.city
        
    elif args.cities:
        # Multiple cities lookup
        lookup = CityLookup()
        cities = [city.strip() for city in args.cities.split(';')]
        bbox = lookup.get_bounding_box_for_cities(cities, args.buffer)
        if not bbox:
            print("Could not determine bounding box for cities")
            return
        
        north, south, east, west = bbox['north'], bbox['south'], bbox['east'], bbox['west']
        area_name = f"{len(bbox['cities'])} cities"
        
    elif args.coords:
        # Custom coordinates
        if not all([args.north, args.south, args.east, args.west]):
            print("Error: --coords requires --north, --south, --east, --west")
            return
        north, south, east, west = args.north, args.south, args.east, args.west
        area_name = "custom area"
        
    # Validation
    if north is None:
        print("Error: Could not determine coordinates")
        return
    
    # Warning for large areas
    if args.region in ['north_america', 'usa', 'canada']:
        print("⚠️  WARNING: Large region selected!")
        print(f"This will generate a LOT of tiles. Estimated storage for zoom {args.min_zoom}-{args.max_zoom}:")
        
        # Rough estimate with corrected calculation
        total_tiles = 0
        for zoom in range(args.min_zoom, args.max_zoom + 1):
            x_min, y_max = generator.deg2num(south, west, zoom)  # Bottom-left
            x_max, y_min = generator.deg2num(north, east, zoom)  # Top-right
            
            # Ensure proper ordering
            if x_min > x_max:
                x_min, x_max = x_max, x_min
            if y_min > y_max:
                y_min, y_max = y_max, y_min
                
            tiles_this_zoom = (x_max - x_min + 1) * (y_max - y_min + 1)
            total_tiles += tiles_this_zoom
        
        estimated_mb = total_tiles * 15 / 1024  # ~15KB per tile average
        print(f"  - Estimated tiles: {total_tiles:,}")
        print(f"  - Estimated size: {estimated_mb:.1f} MB")
        print("  - Consider starting with lower zoom levels or smaller regions")
        
        confirm = input("Continue? (y/N): ")
        if confirm.lower() != 'y':
            print("Cancelled.")
            return
    
    print(f"Generating tiles for: {args.region if args.region else 'custom area'}")
    
    # Generate tiles
    generator.generate_tiles(
        north=north,
        south=south,
        east=east,
        west=west,
        min_zoom=args.min_zoom,
        max_zoom=args.max_zoom,
        source=args.source,
        max_workers=args.max_workers
    )

if __name__ == "__main__":
    main()
