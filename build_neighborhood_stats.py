#!/usr/bin/env python3
"""
Aggregate building and grid data by Settlement (neighborhood).
Outputs neighborhood_stats.json with avg_coverage, priority_score, etc.
"""
import json
import geopandas as gpd
import numpy as np

BUILDINGS_GEOJSON = "Building_Footprints.geojson"
GRID_GEOJSON = "uhi_grid.geojson"
OUTPUT_JSON = "neighborhood_stats.json"


def main():
    print("Loading buildings...")
    buildings = gpd.read_file(BUILDINGS_GEOJSON)
    buildings = buildings.to_crs("EPSG:4326")

    print("Loading grid...")
    grid = gpd.read_file(GRID_GEOJSON)
    grid = grid.to_crs("EPSG:4326")

    # Assign dominant Settlement to each grid cell: which buildings intersect each cell?
    # Drop pre-existing settlement column (added by enrich_uhi_grid.py) to avoid merge conflicts
    if "settlement" in grid.columns:
        grid = grid.drop(columns=["settlement"])
    if "Settlement" in grid.columns:
        grid = grid.drop(columns=["Settlement"])

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

    # Aggregate grid stats by settlement
    grid_by_settlement = (
        grid.groupby("settlement")
        .agg(
            avg_coverage=("coverage_pct", "mean"),
            max_coverage=("coverage_pct", "max"),
            cell_count=("grid_id", "count"),
            total_building_count=("building_count", "sum"),
        )
        .reset_index()
    )

    # Building stats by Settlement
    buildings["TotalSqft"] = buildings["TotalSqft"].fillna(
        buildings["FootprintSqft"] * buildings["Storeys"].fillna(1)
    )
    building_stats = buildings.groupby("Settlement").agg(
        building_count=("OBJECTID", "count"),
        total_sqft=("TotalSqft", "sum"),
        residential_count=("BuildingType", lambda x: (x == "Residential").sum()),
    ).reset_index()
    building_stats["residential_pct"] = (
        building_stats["residential_count"] / building_stats["building_count"]
    ).round(4)

    # Size eligible count
    buildings["size_eligible"] = buildings["TotalSqft"].fillna(99999) <= 6458
    size_eligible = buildings.groupby("Settlement")["size_eligible"].sum().reset_index()
    size_eligible = size_eligible.rename(columns={"size_eligible": "size_eligible_count"})

    # Merge all
    stats = grid_by_settlement.merge(
        building_stats,
        left_on="settlement",
        right_on="Settlement",
        how="outer",
    ).merge(
        size_eligible,
        left_on="settlement",
        right_on="Settlement",
        how="left",
        suffixes=("", "_y"),
    )
    stats = stats[[c for c in stats.columns if not c.endswith("_y") and c != "Settlement"]].copy()
    if "size_eligible_count" not in stats.columns:
        stats["size_eligible_count"] = 0
    stats["size_eligible_count"] = stats["size_eligible_count"].fillna(0).astype(int)

    for col in ["avg_coverage", "max_coverage", "total_building_count", "residential_pct"]:
        if col in stats.columns:
            stats[col] = stats[col].fillna(0)

    stats["building_count"] = stats["building_count"].fillna(0).astype(int)
    stats["total_sqft"] = stats["total_sqft"].fillna(0).astype(int)
    stats["cell_count"] = stats["cell_count"].fillna(1)
    stats["building_density"] = (
        stats["total_building_count"] / stats["cell_count"].replace(0, 1)
    ).round(2)

    # Normalize for priority score (0-1)
    max_cov = stats["avg_coverage"].max() or 1
    max_den = stats["building_density"].max() or 1
    stats["norm_coverage"] = (stats["avg_coverage"] / max_cov).round(4)
    stats["norm_density"] = (stats["building_density"] / max_den).round(4)

    stats["priority_score"] = (
        0.4 * stats["norm_coverage"]
        + 0.3 * stats["norm_density"]
        + 0.3 * stats["residential_pct"]
    ).round(4)

    # Compute settlement centroids from building locations
    centroids = buildings.groupby("Settlement").agg(
        centroid_lat=("geometry", lambda g: g.centroid.y.mean()),
        centroid_lng=("geometry", lambda g: g.centroid.x.mean()),
    ).reset_index()
    stats = stats.merge(
        centroids,
        left_on="settlement",
        right_on="Settlement",
        how="left",
        suffixes=("", "_centroid"),
    )
    stats = stats[[c for c in stats.columns if c != "Settlement_centroid" and c != "Settlement"]].copy()
    stats["centroid_lat"] = stats["centroid_lat"].round(6)
    stats["centroid_lng"] = stats["centroid_lng"].round(6)

    # Clean columns for output
    out = stats[
        [
            "settlement",
            "avg_coverage",
            "max_coverage",
            "building_count",
            "total_sqft",
            "residential_count",
            "residential_pct",
            "size_eligible_count",
            "building_density",
            "priority_score",
            "centroid_lat",
            "centroid_lng",
        ]
    ].copy()
    out = out.rename(columns={"settlement": "Settlement"})

    # Drop invalid / empty settlements
    out["Settlement"] = out["Settlement"].astype(str).str.strip()
    out = out[
        (out["Settlement"] != "")
        & (out["Settlement"] != "0")
        & (out["Settlement"].str.lower() != "unknown")
        & (out["Settlement"].str.lower() != "nan")
        & (out["building_count"] > 0)
    ].copy()

    out = out.fillna(0)
    for col in ["avg_coverage", "max_coverage", "building_density", "priority_score", "residential_pct"]:
        out[col] = out[col].round(4)
    out["total_sqft"] = out["total_sqft"].astype(int)
    out["building_count"] = out["building_count"].astype(int)
    out["residential_count"] = out["residential_count"].astype(int)

    result = out.to_dict(orient="records")
    with open(OUTPUT_JSON, "w") as f:
        json.dump(result, f, indent=2)

    print(f"Wrote {OUTPUT_JSON}: {len(result)} settlements")
    print("Top 3 by priority_score:", sorted(result, key=lambda x: x["priority_score"], reverse=True)[:3])


if __name__ == "__main__":
    main()
