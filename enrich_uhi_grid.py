#!/usr/bin/env python3
"""
Add Settlement to each uhi_grid cell via spatial join with buildings.
Overwrites uhi_grid.geojson with enriched data.
"""
import geopandas as gpd

GRID_GEOJSON = "uhi_grid.geojson"
BUILDINGS_GEOJSON = "Building_Footprints.geojson"


def main():
    print("Loading grid...")
    grid = gpd.read_file(GRID_GEOJSON)
    grid = grid.to_crs("EPSG:4326")

    print("Loading buildings...")
    buildings = gpd.read_file(BUILDINGS_GEOJSON)
    buildings = buildings.to_crs("EPSG:4326")

    print("Spatial join to assign Settlement per cell...")
    joined = gpd.sjoin(
        grid[["grid_id", "geometry", "coverage_pct", "building_count"]],
        buildings[["geometry", "Settlement"]],
        how="left",
        predicate="intersects",
    )
    settlement_per_cell = joined.groupby("grid_id")["Settlement"].agg(
        lambda x: x.mode().iloc[0] if len(x.dropna()) > 0 and len(x.mode()) > 0 else "Unknown"
    )
    grid = grid.merge(
        settlement_per_cell.reset_index().rename(columns={"Settlement": "settlement"}),
        on="grid_id",
        how="left",
    )
    grid["settlement"] = grid["settlement"].fillna("Unknown")

    print(f"Writing {GRID_GEOJSON}...")
    grid.to_file(GRID_GEOJSON, driver="GeoJSON")
    print(f"Done. Settlements: {grid['settlement'].nunique()}")


if __name__ == "__main__":
    main()
