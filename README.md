# CxC2026 – Urban Heat Island Map (Waterloo Region)

Uses building footprint density as a proxy for urban heat intensity. Higher coverage = more impervious surface = warmer microclimate.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

1. **Generate all data** (run in order):

   ```bash
   .venv/bin/python compute_uhi.py          # uhi_grid.geojson
   .venv/bin/python build_building_scores.py  # buildings_enriched.json, buildings_enriched_sample.json
   .venv/bin/python build_neighborhood_stats.py  # neighborhood_stats.json
   .venv/bin/python enrich_uhi_grid.py      # adds Settlement to uhi_grid
   ```

2. **Serve and view the map** (needed for CORS when loading GeoJSON):

   ```bash
   python -m http.server 8000
   ```

   Open http://localhost:8000

## Interpretation

- **Blue** = Low building coverage (cooler, more vegetation/open space)
- **Red** = High building coverage (hotter, urban heat island)
- Use for: green infrastructure planning, tree planting, heat warning outreach
