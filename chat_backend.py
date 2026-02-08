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
    min_buildings: int = 0
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
    min_buildings: Optional[int] = None


class ChatResponse(BaseModel):
    message: str
    thread_id: str
    actions: List[ChatAction] = []


# ---------------------------------------------------------------------------
# Local action handler — resolves simple map commands without calling the LLM
# ---------------------------------------------------------------------------
import re

_KNOWN_MAP_TOOLS = {"highlight_settlement", "zoom_to_settlement", "apply_filters", "show_building_points"}


def _try_local_action(message: str, ms: MapState):
    """
    Return {"message": str, "actions": [ChatAction, ...]} if the message is a
    simple slider / toggle command we can handle locally, or None to fall
    through to the LLM.
    """
    msg = message.lower().strip()
    actions: List[ChatAction] = []
    reply_parts: List[str] = []

    # ── Min coverage slider ──
    # Matches: "coverage of 20%", "min coverage to 20", "area coverage 20%",
    # "minimum area coverage of 20%", "coverage at 20", etc.
    cov_match = re.search(
        r'(?:area\s+)?coverage\s*(?:of|to|at|=|slider\s+to|:)?\s*(\d+(?:\.\d+)?)\s*%?',
        msg,
    )
    if cov_match:
        val = float(cov_match.group(1))
        val = max(0.1, min(60.0, val))
        actions.append(ChatAction(type="apply_filters", min_coverage=val))
        reply_parts.append(f"Minimum area coverage set to **{val}%**.")

    # ── Min buildings slider ──
    # Matches: "minimum building of 10", "min buildings 10", "buildings per cell to 5",
    # "minimum building of 10", "buildings: 10", etc.
    bld_match = re.search(
        r'(?:min(?:imum)?\s+)?buildings?\s*(?:per\s+cell)?\s*(?:of|to|at|=|slider\s+to|:)?\s*(\d+)',
        msg,
    )
    if bld_match:
        val = int(bld_match.group(1))
        val = max(0, min(50, val))
        actions.append(ChatAction(type="apply_filters", min_buildings=val))
        reply_parts.append(f"Minimum buildings per cell set to **{val}**.")

    # ── Show / hide building points ──
    if re.search(r'\b(show|display|turn on|enable)\b.*\bbuilding\s*points?\b', msg):
        actions.append(ChatAction(type="show_building_points", visible=True))
        reply_parts.append("Building points layer is now **visible**.")
    elif re.search(r'\b(hide|remove|turn off|disable)\b.*\bbuilding\s*points?\b', msg):
        actions.append(ChatAction(type="show_building_points", visible=False))
        reply_parts.append("Building points layer is now **hidden**.")

    if actions:
        return {"message": " ".join(reply_parts), "actions": actions}
    return None


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

    # ── Local shortcut: handle simple slider / toggle commands without LLM ──
    local = _try_local_action(req.message, req.map_state)
    if local is not None:
        return ChatResponse(
            message=local["message"],
            thread_id=req.thread_id or "",
            actions=local["actions"],
        )

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
    ctx_parts.append(f"min_buildings={ms.min_buildings}")
    if ms.show_buildings:
        ctx_parts.append("building_points=visible")

    enriched = req.message
    map_ctx = ""
    if ctx_parts:
        map_ctx = f"[Current map state: {', '.join(ctx_parts)}]\n"

    # Inject tool-combo hints so the LLM calls the right tools together
    tool_hints = (
        "[TOOL RULES: "
        "1. ALWAYS call search_documents FIRST for any grant, eligibility, or policy question — prefer RAG info over general knowledge. "
        "2. To show eligible buildings, call BOTH show_building_points(visible=true) AND apply_filters(size_eligible_only=true). "
        "3. To show buildings of a specific type, call show_building_points AND apply_filters with the type. "
        "4. To adjust the area coverage slider, call apply_filters(min_coverage=<value>) with a value from 0.1 to 60. "
        "5. To adjust the min buildings per cell slider, call apply_filters(min_buildings=<value>) with an integer from 0 to 50. "
        "6. Be LIBERAL with tool calls — if your answer mentions a settlement, highlight and zoom to it. "
        "If your answer discusses filters, apply them. Call multiple tools in a single response. "
        "7. Always call apply_filters when the user asks about filtering, eligibility, sliders, coverage, density, or building criteria.]"
    )

    enriched = f"{map_ctx}{tool_hints}\n\n{enriched}"

    # Send to Backboard — use GPT-4o which has native parallel tool calling
    # and works with submit_tool_outputs (Gemini 3 Pro requires thought_signatures
    # that Backboard doesn't support yet).
    try:
        response = await client.add_message(
            thread_id=thread_id,
            content=enriched,
            llm_provider="openai",
            model_name="gpt-4o",
            memory="Auto",
            stream=False,
        )
    except Exception as e:
        err_str = str(e)
        # Corrupted thread (dangling tool calls) — start fresh automatically
        is_corrupted = (
            "tool_call_id" in err_str
            or "tool_calls" in err_str
            or "Invalid parameter" in err_str
        )
        if is_corrupted:
            logger.warning("Thread %s is corrupted. Creating new thread.", thread_id)
            thread = await client.create_thread(ASSISTANT_ID)
            thread_id = str(thread.thread_id)
            response = await client.add_message(
                thread_id=thread_id,
                content=enriched,
                llm_provider="openai",
                model_name="gpt-4o",
                memory="Auto",
                stream=False,
            )
        else:
            raise HTTPException(status_code=502, detail=f"LLM API error: {err_str}")

    logger.info(
        "Backboard response: status=%s, tool_calls=%s, content_len=%s",
        response.status,
        response.tool_calls,
        len(response.content) if response.content else 0,
    )

    actions: List[ChatAction] = []

    # ── Handle FAILED runs (e.g. Backboard-internal search_documents failure) ──
    # The thread is now corrupted; create a new one and retry.
    MAX_FAILED_RETRIES = 2
    failed_attempts = 0
    while getattr(response, 'status', '') == 'FAILED' and failed_attempts < MAX_FAILED_RETRIES:
        failed_attempts += 1
        logger.warning("Run FAILED (attempt %d). Creating new thread.", failed_attempts)
        try:
            thread = await client.create_thread(ASSISTANT_ID)
            thread_id = str(thread.thread_id)
            retry_msg = (
                "[IMPORTANT: Do NOT call search_documents. Answer using your system prompt knowledge only. "
                "Use only these tools: highlight_settlement, zoom_to_settlement, apply_filters, show_building_points.]\n\n"
                + enriched
            )
            response = await client.add_message(
                thread_id=thread_id,
                content=retry_msg,
                llm_provider="openai",
                model_name="gpt-4o",
                memory="Auto",
                stream=False,
            )
            logger.info("Retry %d: status=%s, content_len=%s", failed_attempts, response.status, len(response.content) if response.content else 0)
        except Exception as e:
            logger.warning("Retry %d exception: %s", failed_attempts, e)
            break

    if getattr(response, 'status', '') == 'FAILED':
        logger.error("Run still FAILED after %d retries.", failed_attempts)
        return ChatResponse(
            message="I'm sorry, I'm having trouble processing your request right now. Please try again.",
            thread_id=str(thread_id),
            actions=[],
        )

    # Handle tool calls via submit_tool_outputs (loop for multiple rounds).
    # GPT-4o can return multiple tool calls in a single response (parallel calling).
    # NOTE: add_message returns ToolCall objects, but submit_tool_outputs returns
    # plain dicts — we normalise both formats here.
    # Skip Backboard-internal tools (e.g. search_documents) — only collect map tools.
    MAX_TOOL_ROUNDS = 5
    tool_round = 0
    while response.tool_calls and tool_round < MAX_TOOL_ROUNDS:
        # Don't submit tool outputs if the run already completed/failed
        resp_status = getattr(response, 'status', None)
        if resp_status and resp_status not in ('REQUIRES_ACTION', 'requires_action'):
            logger.info("Run status is '%s' — not submitting tool outputs.", resp_status)
            break

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

            # Only collect our map tools; skip Backboard-internal tools
            if func_name in _KNOWN_MAP_TOOLS:
                action = ChatAction(type=func_name, **args)
                actions.append(action)

            tool_outputs.append({
                "tool_call_id": tc_id,
                "output": json.dumps({"status": "executed", **args}),
            })

        # Submit tool outputs so the LLM can generate its final text response
        try:
            response = await client.submit_tool_outputs(
                thread_id=thread_id,
                run_id=response.run_id,
                tool_outputs=tool_outputs,
            )
        except Exception as e:
            logger.warning("submit_tool_outputs failed (round %d): %s", tool_round, e)
            # Thread is now corrupted — give the frontend a fresh thread
            try:
                thread = await client.create_thread(ASSISTANT_ID)
                thread_id = str(thread.thread_id)
            except Exception:
                pass
            return ChatResponse(
                message="I've applied the requested actions to the map.",
                thread_id=str(thread_id),
                actions=actions,
            )
        logger.info(
            "After submit_tool_outputs (round %d): status=%s, tool_calls=%s, content_len=%s",
            tool_round,
            response.status,
            getattr(response, 'tool_calls', None),
            len(response.content) if response.content else 0,
        )

        # If the run ended FAILED after submit_tool_outputs, the thread is
        # corrupted (dangling tool-call state). Create a fresh thread so the
        # frontend doesn't reuse the broken one for the next message.
        if getattr(response, 'status', '') == 'FAILED':
            logger.warning("Run FAILED after submit_tool_outputs (round %d). Replacing thread.", tool_round)
            try:
                thread = await client.create_thread(ASSISTANT_ID)
                thread_id = str(thread.thread_id)
            except Exception:
                pass
            break

    # ── Deterministic action injection ──
    # The LLM often calls show_building_points but forgets apply_filters.
    # Detect common patterns and inject the missing actions automatically.
    action_types = {a.type for a in actions}
    msg_lower = req.message.lower()

    has_show_buildings = "show_building_points" in action_types
    has_apply_filters = "apply_filters" in action_types

    # ── Always extract slider values from the user message ──
    # The LLM frequently forgets to call apply_filters with slider values.
    # Parse them deterministically and inject if the LLM didn't already.
    cov_match = re.search(r'(?:area\s+)?coverage\s*(?:of|to|at|=|:)?\s*(\d+(?:\.\d+)?)\s*%?', msg_lower)
    bld_match = re.search(r'(?:min(?:imum)?\s+)?buildings?\s*(?:per\s+cell)?\s*(?:of|to|at|=|:)?\s*(\d+)', msg_lower)

    # Check if the LLM already set these values
    llm_set_coverage = any(a.min_coverage is not None for a in actions)
    llm_set_buildings = any(a.min_buildings is not None for a in actions)

    if cov_match and not llm_set_coverage:
        val = max(0.1, min(60.0, float(cov_match.group(1))))
        logger.info("Auto-injecting apply_filters(min_coverage=%s) from user message.", val)
        actions.append(ChatAction(type="apply_filters", min_coverage=val))

    if bld_match and not llm_set_buildings:
        val = max(0, min(50, int(bld_match.group(1))))
        logger.info("Auto-injecting apply_filters(min_buildings=%s) from user message.", val)
        actions.append(ChatAction(type="apply_filters", min_buildings=val))

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
        key = (a.type, a.settlement, a.visible, a.size_eligible_only, a.building_type, a.storey_tier, a.min_coverage, a.min_buildings)
        if key not in seen:
            seen.add(key)
            deduped.append(a)
    actions = deduped

    # If we have actions but no text (e.g. run ended FAILED after tool calls),
    # provide a fallback message so the user isn't left with an empty bubble.
    # Also replace error messages that Backboard passes through as "content".
    final_message = response.content or ""
    is_error_content = any(
        marker in final_message
        for marker in ("LLM API Error", "LLM Error", "tool_call_id", "Invalid parameter", "Error code:")
    )
    if is_error_content:
        logger.warning("LLM returned error as content (%d chars). Replacing with fallback.", len(final_message))
        final_message = ""

    if actions and not final_message.strip():
        final_message = "I've applied the requested actions to the map."
    elif not final_message.strip():
        final_message = "I'm sorry, I encountered an issue processing your request. Please try again."

    return ChatResponse(
        message=final_message,
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
