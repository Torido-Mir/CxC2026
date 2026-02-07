#!/usr/bin/env python3
"""
Enrich building footprints with grant-relevant scores.
Outputs buildings_enriched.json with centroids + size_eligible, storey_category, svr_proxy.
"""
import geopandas as gpd
import numpy as np

INPUT_GEOJSON = "Building_Footprints.geojson"
OUTPUT_JSON = "buildings_enriched.json"
SIZE_CAP_SQFT = 6458  # 600 m²


def main():
    print("Loading building footprints...")
    buildings = gpd.read_file(INPUT_GEOJSON)
    buildings = buildings.to_crs("EPSG:4326")

    # Handle nulls
    buildings["Storeys"] = buildings["Storeys"].fillna(1)
    buildings["TotalSqft"] = buildings["TotalSqft"].fillna(
        buildings["FootprintSqft"] * buildings["Storeys"]
    )
    buildings["FootprintSqft"] = buildings["FootprintSqft"].fillna(0)

    # Size eligibility (600 m² cap)
    buildings["size_eligible"] = buildings["TotalSqft"] <= SIZE_CAP_SQFT

    # Storey category
    def storey_cat(s):
        if s is None or np.isnan(s):
            return "low"
        s = int(s)
        if s <= 2:
            return "low"
        if s <= 6:
            return "mid"
        return "high"

    buildings["storey_category"] = buildings["Storeys"].apply(storey_cat)

    # SVR proxy (box model): SA/Volume, height_ft = Storeys * 10
    def svr_proxy(row):
        floor = max(1, row["FootprintSqft"])
        storeys = max(0.5, row["Storeys"])
        height_ft = storeys * 10
        sa = 2 * floor + 4 * (floor ** 0.5) * height_ft
        vol = floor * height_ft
        if vol <= 0:
            return 0
        return round(sa / vol, 4)

    buildings["svr_proxy"] = buildings.apply(svr_proxy, axis=1)

    # Centroids for point layer
    buildings["geometry"] = buildings.geometry.centroid

    # Keep only needed columns for web
    out = buildings[
        [
            "OBJECTID",
            "Municipality",
            "Settlement",
            "FootprintSqft",
            "Storeys",
            "TotalSqft",
            "BuildingType",
            "size_eligible",
            "storey_category",
            "svr_proxy",
            "geometry",
        ]
    ].copy()

    import json

    out_geojson = json.loads(out.to_json())
    for f in out_geojson.get("features", []):
        p = f.get("properties", {})
        if "svr_proxy" in p and p["svr_proxy"] is not None:
            p["svr_proxy"] = round(float(p["svr_proxy"]), 4)
        coords = f.get("geometry", {}).get("coordinates", [])
        if len(coords) >= 2:
            f["geometry"] = {"type": "Point", "coordinates": [round(coords[0], 6), round(coords[1], 6)]}

    with open(OUTPUT_JSON, "w") as f:
        json.dump(out_geojson, f, separators=(",", ":"))

    print(f"Wrote {OUTPUT_JSON}: {len(out)} buildings")

    # 10% sample for web map (lighter)
    sample = out.iloc[::10].copy()
    sample_geojson = json.loads(sample.to_json())
    for f in sample_geojson.get("features", []):
        if "svr_proxy" in f.get("properties", {}):
            f["properties"]["svr_proxy"] = round(float(f["properties"]["svr_proxy"]), 4)
    with open("buildings_enriched_sample.json", "w") as f:
        json.dump(sample_geojson, f, separators=(",", ":"))
    print(f"Wrote buildings_enriched_sample.json: {len(sample)} buildings (10% sample)")

    print(f"  size_eligible: {out['size_eligible'].sum()}")
    print(f"  storey low/mid/high: {out['storey_category'].value_counts().to_dict()}")


if __name__ == "__main__":
    main()
