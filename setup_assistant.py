#!/usr/bin/env python3
"""
One-time setup: create a Backboard assistant with grant-targeting tools,
system prompt, and upload grant documents.

Usage:
    .venv/bin/python setup_assistant.py

Prints the assistant_id to stdout — save it in .env as BACKBOARD_ASSISTANT_ID.
"""
import asyncio
import glob
import os

from dotenv import load_dotenv
from backboard import BackboardClient

load_dotenv()

SYSTEM_PROMPT = """\
You are **Grant Targeting Advisor** for the Waterloo Region Urban Heat Island (UHI) project.

## Your Role
Help municipal planners and residents identify which buildings and neighborhoods
qualify for green retrofit and energy-efficiency grants at the Federal (Canada),
Provincial (Ontario), and Regional (Waterloo) levels.

## Data You Know About
The map shows the Waterloo Region with the following data layers:
- **UHI Grid**: 500 m cells coloured by building footprint coverage %. Higher = hotter.
- **Building Points**: Individual buildings with attributes:
  - Settlement (neighborhood name)
  - FootprintSqft, Storeys, TotalSqft
  - size_eligible: true if TotalSqft <= 6,458 sqft (600 m²)
  - storey_category: "low" (1-2), "mid" (3-6), "high" (7+)
  - svr_proxy: surface-to-volume ratio proxy (higher = more heat loss)
  - BuildingType: Residential, Commercial, Agricultural, Industrial, etc.
- **Neighborhood Stats** (per Settlement):
  - avg_coverage, max_coverage (building footprint coverage %)
  - building_count, total_sqft, residential_count, residential_pct
  - size_eligible_count
  - building_density (buildings per grid cell)
  - priority_score (0-1, higher = stronger candidate for retrofit grants)

## Available Settlements
Ayr, Baden, Bamberg, Bloomingdale, Breslau, Cambridge, Conestogo, Elmira,
Heidelberg, Kitchener, Linwood, Maryhill, New Dundee, New Hamburg,
North Dumfries, Petersburg, Roseville, St. Clements, St. Jacobs,
Wallenstein, Waterloo, Wellesley, West Montrose, Woolwich

## Tool Usage
- When you mention a specific settlement, call **highlight_settlement** so the user can see it on the map.
- When recommending a specific area to focus on, call **zoom_to_settlement**.
- When advising on eligibility filters, call **apply_filters** to update the map.
- When building-level detail would help, call **show_building_points** to toggle the layer.

## Grant Knowledge
Use your uploaded documents to answer specific grant eligibility questions.
Always cite the program name and jurisdiction (Federal / Ontario / Waterloo).
If you do not have information about a specific grant, say so honestly.

## Response Style
- Be concise and actionable.
- Use bullet points for eligibility criteria.
- When comparing settlements, include key numbers (priority_score, building_count, avg_coverage).
- Suggest next steps (e.g., "apply filters to see only size-eligible residential buildings").
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "highlight_settlement",
            "description": "Select and highlight a settlement on the map, showing its neighborhood stats in the detail panel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "settlement": {
                        "type": "string",
                        "description": "The settlement name to highlight (e.g. 'Kitchener', 'Cambridge')"
                    }
                },
                "required": ["settlement"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "zoom_to_settlement",
            "description": "Zoom the map to center on a specific settlement.",
            "parameters": {
                "type": "object",
                "properties": {
                    "settlement": {
                        "type": "string",
                        "description": "The settlement name to zoom to"
                    }
                },
                "required": ["settlement"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "apply_filters",
            "description": "Apply filters on the map to narrow down displayed data. Any parameter not provided will be left unchanged.",
            "parameters": {
                "type": "object",
                "properties": {
                    "size_eligible_only": {
                        "type": "boolean",
                        "description": "If true, show only buildings with TotalSqft <= 6458 (600 m²)"
                    },
                    "building_type": {
                        "type": "string",
                        "enum": ["", "Residential", "Commercial", "Agricultural", "Utility and Miscellaneous"],
                        "description": "Filter by building type. Empty string for all."
                    },
                    "storey_tier": {
                        "type": "string",
                        "enum": ["", "low", "mid", "high"],
                        "description": "Filter by storey tier. Empty string for all."
                    },
                    "min_coverage": {
                        "type": "number",
                        "description": "Minimum coverage % to display on the heatmap"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "show_building_points",
            "description": "Toggle the building points layer on the map to show individual buildings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "visible": {
                        "type": "boolean",
                        "description": "True to show building points, false to hide"
                    }
                },
                "required": ["visible"]
            }
        }
    },
]


async def main():
    api_key = os.getenv("BACKBOARD_IO_API_KEY")
    if not api_key:
        print("ERROR: Set BACKBOARD_IO_API_KEY in .env")
        return

    client = BackboardClient(api_key=api_key)

    print("Creating assistant...")
    assistant = await client.create_assistant(
        name="Grant Targeting Advisor",
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
    )
    assistant_id = assistant.assistant_id
    print(f"Assistant created: {assistant_id}")

    # Upload grant documents if any exist
    grant_dir = os.path.join(os.path.dirname(__file__), "grant_docs")
    if os.path.isdir(grant_dir):
        patterns = ["*.pdf", "*.txt", "*.md", "*.docx", "*.json"]
        for pattern in patterns:
            for filepath in glob.glob(os.path.join(grant_dir, pattern)):
                print(f"  Uploading {os.path.basename(filepath)}...")
                await client.upload_document_to_assistant(
                    assistant_id=assistant_id,
                    file_path=filepath,
                )
                print(f"  Uploaded {os.path.basename(filepath)}")
    else:
        print(f"No grant_docs/ directory found. Create it and add PDFs/text, then re-run.")

    print(f"\nDone! Add this to your .env:\nBACKBOARD_ASSISTANT_ID={assistant_id}")


if __name__ == "__main__":
    asyncio.run(main())
