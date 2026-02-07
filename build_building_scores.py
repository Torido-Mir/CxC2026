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

    # ── SVR proxy using real polygon geometry ──────────────
    # Project to a metre-based CRS for accurate area/perimeter,
    # then convert back to WGS 84 for the final output.
    buildings_m = buildings.to_crs("EPSG:32617")  # UTM 17N (Waterloo Region)

    # Real perimeter (ft) and footprint area (sq ft) from the polygon
    M_TO_FT = 3.28084
    SQM_TO_SQFT = 10.7639
    buildings["perimeter_ft"] = buildings_m.geometry.length * M_TO_FT
    buildings["footprint_sqft_geo"] = buildings_m.geometry.area * SQM_TO_SQFT

    def svr_proxy(row):
        floor = max(1, row["footprint_sqft_geo"])   # real footprint area
        perimeter = max(4, row["perimeter_ft"])      # real perimeter
        storeys = max(0.5, row["Storeys"])
        height_ft = storeys * 10  # assume 10 ft per storey

        # Surface area = roof + ground + walls (perimeter × height)
        sa = 2 * floor + perimeter * height_ft
        vol = floor * height_ft
        if vol <= 0:
            return 0
        return round(sa / vol, 4)

    buildings["svr_proxy"] = buildings.apply(svr_proxy, axis=1)

    # Also add compactness ratio: 4π × area / perimeter²
    # Perfect circle = 1.0; more irregular / elongated shapes → lower values
    buildings["compactness"] = buildings.apply(
        lambda r: round(
            4 * np.pi * max(1, r["footprint_sqft_geo"]) / max(1, r["perimeter_ft"]) ** 2, 4
        ),
        axis=1,
    )

    # Centroids for point layer (use projected CRS for accuracy, then back to WGS 84)
    buildings_m = buildings.to_crs("EPSG:32617")
    buildings["geometry"] = buildings_m.geometry.centroid
    buildings = buildings.set_crs("EPSG:32617", allow_override=True).to_crs("EPSG:4326")

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
            "compactness",
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
