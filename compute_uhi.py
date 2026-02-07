#!/usr/bin/env python3
"""
Compute Urban Heat Island proxy from building footprints.
Outputs uhi_grid.geojson with coverage_pct per grid cell.
"""
import json
import geopandas as gpd
from shapely.geometry import box
import numpy as np

INPUT_GEOJSON = "Building_Footprints.geojson"
OUTPUT_GEOJSON = "uhi_grid.geojson"
CELL_SIZE_DEG = 0.006  # ~500m at this latitude (balance detail vs speed)


def main():
    print("Loading building footprints...")
    buildings = gpd.read_file(INPUT_GEOJSON)
    buildings = buildings.to_crs("EPSG:4326")

    # Create grid over bounding box
    minx, miny, maxx, maxy = buildings.total_bounds
    rows = int(np.ceil((maxy - miny) / CELL_SIZE_DEG))
    cols = int(np.ceil((maxx - minx) / CELL_SIZE_DEG))
    print(f"Creating {rows}x{cols} grid ({rows * cols} cells)...")

    grid_cells = []
    for r in range(rows):
        for c in range(cols):
            x1 = minx + c * CELL_SIZE_DEG
            y1 = miny + r * CELL_SIZE_DEG
            x2 = min(x1 + CELL_SIZE_DEG, maxx)
            y2 = min(y1 + CELL_SIZE_DEG, maxy)
            grid_cells.append(box(x1, y1, x2, y2))

    grid = gpd.GeoDataFrame(
        {"grid_id": range(len(grid_cells))},
        geometry=grid_cells,
        crs="EPSG:4326",
    )
    grid["cell_area"] = grid.geometry.area

    # Overlay: intersect buildings with grid
    print("Computing building coverage per cell...")
    intersected = gpd.overlay(grid[["grid_id", "geometry", "cell_area"]], buildings[["geometry"]], how="intersection")
    intersected["overlap_area"] = intersected.geometry.area

    # Sum overlap area per grid cell
    coverage = intersected.groupby("grid_id").agg({"overlap_area": "sum", "cell_area": "first"}).reset_index()
    coverage["coverage_pct"] = (coverage["overlap_area"] / coverage["cell_area"] * 100).round(2)
    counts = intersected.groupby("grid_id").size().reset_index(name="building_count")
    coverage = coverage.merge(counts, on="grid_id", how="left")

    # Merge back to grid
    grid = grid.merge(coverage[["grid_id", "coverage_pct", "building_count"]], on="grid_id", how="left")
    grid["coverage_pct"] = grid["coverage_pct"].fillna(0)
    grid["building_count"] = grid["building_count"].fillna(0).astype(int)

    # Drop temp columns for output
    grid = grid[["grid_id", "coverage_pct", "building_count", "geometry"]]

    print(f"Writing {OUTPUT_GEOJSON}...")
    grid.to_file(OUTPUT_GEOJSON, driver="GeoJSON")
    print(f"Done. Coverage range: {grid['coverage_pct'].min():.1f}% - {grid['coverage_pct'].max():.1f}%")


if __name__ == "__main__":
    main()
