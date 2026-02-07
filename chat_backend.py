#!/usr/bin/env python3
"""
Thin FastAPI backend that proxies chat messages to Backboard.io,
handles tool calls (map actions), and returns responses + actions to the frontend.

Usage:
    .venv/bin/uvicorn chat_backend:app --reload --port 8001
"""
import json
import logging
import os
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("chat_backend")

load_dotenv()

# ---------------------------------------------------------------------------
# Backboard client (lazy-initialized)
# ---------------------------------------------------------------------------
_client = None


def get_client():
    global _client
    if _client is None:
        from backboard import BackboardClient

        api_key = os.getenv("BACKBOARD_IO_API_KEY")
        if not api_key:
            raise RuntimeError("BACKBOARD_IO_API_KEY not set in .env")
        _client = BackboardClient(api_key=api_key)
    return _client


ASSISTANT_ID = os.getenv("BACKBOARD_ASSISTANT_ID", "")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Grant Chat Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class MapState(BaseModel):
    settlement: str = ""
    size_eligible_only: bool = False
    building_type: str = ""
    storey_tier: str = ""
    min_coverage: float = 0.1
    show_buildings: bool = False


class ChatRequest(BaseModel):
    message: str
    thread_id: Optional[str] = None
    map_state: MapState = MapState()


class ChatAction(BaseModel):
    type: str
    settlement: Optional[str] = None
    visible: Optional[bool] = None
    size_eligible_only: Optional[bool] = None
    building_type: Optional[str] = None
    storey_tier: Optional[str] = None
    min_coverage: Optional[float] = None


class ChatResponse(BaseModel):
    message: str
    thread_id: str
    actions: List[ChatAction] = []


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    if not ASSISTANT_ID:
        raise HTTPException(
            status_code=500,
            detail="BACKBOARD_ASSISTANT_ID not set. Run setup_assistant.py first.",
        )

    client = get_client()

    # Create or reuse thread
    thread_id = req.thread_id
    if not thread_id:
        thread = await client.create_thread(ASSISTANT_ID)
        thread_id = str(thread.thread_id)

    # Build enriched message with current map context
    ctx_parts = []
    ms = req.map_state
    if ms.settlement:
        ctx_parts.append(f"selected_settlement={ms.settlement}")
    if ms.size_eligible_only:
        ctx_parts.append("size_eligible_filter=ON")
    if ms.building_type:
        ctx_parts.append(f"building_type_filter={ms.building_type}")
    if ms.storey_tier:
        ctx_parts.append(f"storey_tier_filter={ms.storey_tier}")
    ctx_parts.append(f"min_coverage={ms.min_coverage}")
    if ms.show_buildings:
        ctx_parts.append("building_points=visible")

    enriched = req.message
    map_ctx = ""
    if ctx_parts:
        map_ctx = f"[Current map state: {', '.join(ctx_parts)}]\n"

    # Inject tool-combo hints so the LLM calls the right tools together
    tool_hints = (
        "[TOOL RULES: "
        "To show eligible/grant-qualifying buildings, you MUST call BOTH "
        "show_building_points(visible=true) AND apply_filters(size_eligible_only=true) together. "
        "To show buildings of a specific type, call show_building_points AND apply_filters with the type. "
        "You can call multiple tools in a single response. "
        "Always call apply_filters when the user asks about filtering, eligibility, or specific building criteria.]"
    )

    enriched = f"{map_ctx}{tool_hints}\n\n{enriched}"

    # Send to Backboard — use GPT-4o which has native parallel tool calling
    # and works with submit_tool_outputs (Gemini 3 Pro requires thought_signatures
    # that Backboard doesn't support yet).
    response = await client.add_message(
        thread_id=thread_id,
        content=enriched,
        llm_provider="openai",
        model_name="gpt-4o",
        memory="Auto",
        stream=False,
    )

    logger.info(
        "Backboard response: status=%s, tool_calls=%s, content_len=%s",
        response.status,
        response.tool_calls,
        len(response.content) if response.content else 0,
    )

    actions: List[ChatAction] = []

    # Handle tool calls via submit_tool_outputs (loop for multiple rounds).
    # GPT-4o can return multiple tool calls in a single response (parallel calling).
    # NOTE: add_message returns ToolCall objects, but submit_tool_outputs returns
    # plain dicts — we normalise both formats here.
    MAX_TOOL_ROUNDS = 5
    tool_round = 0
    while response.tool_calls and tool_round < MAX_TOOL_ROUNDS:
        tool_round += 1
        logger.info("Processing tool call round %d with %d call(s)", tool_round, len(response.tool_calls))
        tool_outputs = []
        for tc in response.tool_calls:
            # Normalise: ToolCall object vs plain dict (from submit_tool_outputs)
            if isinstance(tc, dict):
                tc_id = tc["id"]
                func_name = tc["function"]["name"]
                args = json.loads(tc["function"]["arguments"])
            else:
                tc_id = tc.id
                func_name = tc.function.name
                args = tc.function.parsed_arguments
            logger.info("  Tool call: %s(%s)", func_name, args)
            action = ChatAction(type=func_name, **args)
            actions.append(action)
            tool_outputs.append({
                "tool_call_id": tc_id,
                "output": json.dumps({"status": "executed", **args}),
            })

        # Submit tool outputs so the LLM can generate its final text response
        response = await client.submit_tool_outputs(
            thread_id=thread_id,
            run_id=response.run_id,
            tool_outputs=tool_outputs,
        )
        logger.info(
            "After submit_tool_outputs (round %d): status=%s, tool_calls=%s, content_len=%s",
            tool_round,
            response.status,
            getattr(response, 'tool_calls', None),
            len(response.content) if response.content else 0,
        )

    # ── Deterministic action injection ──
    # The LLM often calls show_building_points but forgets apply_filters.
    # Detect common patterns and inject the missing actions automatically.
    action_types = {a.type for a in actions}
    msg_lower = req.message.lower()

    has_show_buildings = "show_building_points" in action_types
    has_apply_filters = "apply_filters" in action_types

    # User wants to see ALL buildings (include ineligible) — turn OFF the filter
    broaden_keywords = {"ineligible", "all buildings", "show all", "include ineligible", "as well", "both eligible and", "eligible and ineligible"}
    wants_to_show_all = any(kw in msg_lower for kw in broaden_keywords)

    # User wants eligible-only — turn ON the filter (exclude "ineligible" from triggering this)
    eligibility_keywords = {"eligible", "grant", "qualify", "qualifying", "eligib", "size_eligible"}
    mentions_eligibility = any(kw in msg_lower for kw in eligibility_keywords)
    wants_eligible_only = mentions_eligibility and "ineligible" not in msg_lower

    if wants_to_show_all:
        logger.info("Auto-injecting apply_filters(size_eligible_only=false) for show-all/ineligible query")
        actions.append(ChatAction(type="apply_filters", size_eligible_only=False))
    elif has_show_buildings and not has_apply_filters and wants_eligible_only:
        logger.info("Auto-injecting apply_filters(size_eligible_only=true) for eligibility query")
        actions.append(ChatAction(type="apply_filters", size_eligible_only=True))

    # Deduplicate actions (LLM sometimes calls the same tool multiple times)
    seen = set()
    deduped: List[ChatAction] = []
    for a in actions:
        key = (a.type, a.settlement, a.visible, a.size_eligible_only, a.building_type, a.storey_tier)
        if key not in seen:
            seen.add(key)
            deduped.append(a)
    actions = deduped

    return ChatResponse(
        message=response.content or "",
        thread_id=str(thread_id),
        actions=actions,
    )


@app.post("/thread")
async def create_thread():
    """Create a new chat thread."""
    if not ASSISTANT_ID:
        raise HTTPException(status_code=500, detail="BACKBOARD_ASSISTANT_ID not set.")
    client = get_client()
    thread = await client.create_thread(ASSISTANT_ID)
    return {"thread_id": str(thread.thread_id)}
