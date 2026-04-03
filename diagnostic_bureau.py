"""
Appliance / Auto Whisperer — Orchestrator Agent

Receives ChatMessage from ASI:One via Agentverse mailbox, runs the full
pipeline (Vision → Parts → Tutorial) and sends a Markdown hero response.

Worker communication (scatter-gather):
  The orchestrator sends PartsSourcingRequest / TutorialSearchRequest to
  the worker agents and awaits responses via asyncio.Future keyed by
  session_id.  If workers are not reachable (timeout), it falls back to
  calling the service functions directly so the agent is always available.

Run order (3 separate terminals):
  1. python workers/parts_agent.py
  2. python workers/tutorial_agent.py
  3. python diagnostic_bureau.py          ← this file

Or single-container Docker:
  docker-compose up --build
  (docker-entrypoint.sh starts all 3 processes automatically)
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from uagents import Agent, Context, Protocol
from uagents.registration import AlmanacApiRegistrationPolicy
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    TextContent,
    chat_protocol_spec,
)

from app.services.brightdata.part_price_service import fetch_parts_deterministic
from app.services.openai.vision_part_extractor import extract_part_diagnosis, validate_diagnosis
from app.services.youtube.instructor_service import find_best_tutorial_video
from app.uagents_protocol.chat_inbound import extract_diagnostic_inputs
from app.uagents_protocol.final_markdown import format_diagnostic_markdown
from app.uagents_protocol.schemas import (
    PartsSourcingRequest,
    PartsSourcingResponse,
    TutorialSearchRequest,
    TutorialSearchResponse,
)

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("orchestrator")

# ──────────────────────────────────────────────────────────────────────────────
# Scatter-gather state
#
# Maps  "parts:<session_id>"   → asyncio.Future[PartsSourcingResponse]
#        "tut:<session_id>"    → asyncio.Future[TutorialSearchResponse]
#
# Created before ctx.send(); resolved by response handlers below.
# Cleaned up on TimeoutError so futures don't leak.
# ──────────────────────────────────────────────────────────────────────────────

_pending: dict[str, asyncio.Future] = {}

# How long to wait for a worker reply before falling back to direct calls.
# With Agentverse mailbox round-trips (~10-20s each direction) + processing time
# (~30-40s for parts scraping), 120s is a safe default.
_WORKER_TIMEOUT_S: float = float(os.getenv("WORKER_TIMEOUT_S", "120"))


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _network() -> str:
    return "mainnet" if os.getenv("AGENT_NETWORK", "testnet").lower() == "mainnet" else "testnet"


def _agent_port() -> int:
    # Render injects PORT; Docker users can set it too.
    return int(os.getenv("PORT") or os.getenv("ORCHESTRATOR_AGENT_PORT", "8001"))


def _address_from_seed(name: str, seed: str) -> str:
    tmp = Agent(name=name, seed=seed)
    return tmp.address


def _free_port(port: int) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return
    except OSError:
        return
    try:
        import psutil
        try:
            conns = psutil.net_connections(kind="inet")
        except AttributeError:
            conns = psutil.net_connections()
        for conn in conns:
            if conn.laddr.port == port and conn.pid:
                try:
                    proc = psutil.Process(conn.pid)
                    log.info("Freeing port %d — killing PID %d (%s)", port, conn.pid, proc.name())
                    proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                return
    except ImportError:
        log.warning("Port %d busy — install psutil or kill the process manually.", port)


def _resolve_worker_address(role: str, name: str, default_seed: str) -> str:
    """
    Derive a worker's deterministic address from its seed.
    Uses <ROLE>_AGENT_SEED env var, falling back to the default seed string.
    """
    seed = os.getenv(f"{role}_AGENT_SEED", default_seed)
    return _address_from_seed(name, seed)


# ──────────────────────────────────────────────────────────────────────────────
# Chat Protocol  (Chat protocol handlers + worker response handlers)
# ──────────────────────────────────────────────────────────────────────────────

chat_protocol = Protocol(spec=chat_protocol_spec)

# ── Internal protocol for worker responses ────────────────────────────────────
# AgentChatProtocol is locked — worker response types must live on a
# separate protocol that is included alongside chat_protocol on the agent.
worker_protocol = Protocol(name="OrchestratorWorkerProtocol", version="0.1.0")


@worker_protocol.on_message(model=PartsSourcingResponse)
async def on_parts_response(ctx: Context, sender: str, msg: PartsSourcingResponse):
    """Receive parts-pricing reply from parts-sourcing-agent."""
    key = f"parts:{msg.session_id}"
    fut = _pending.pop(key, None)
    if fut and not fut.done():
        log.info(
            "[orch] ← parts-agent: $%.2f at %s (%s)",
            msg.price_usd, msg.source_site, msg.stock_status,
        )
        fut.set_result(msg)
    else:
        log.warning("[orch] Received unexpected PartsSourcingResponse for session=%s", msg.session_id)


@worker_protocol.on_message(model=TutorialSearchResponse)
async def on_tutorial_response(ctx: Context, sender: str, msg: TutorialSearchResponse):
    """Receive tutorial reply from tutorial-agent."""
    key = f"tut:{msg.session_id}"
    fut = _pending.pop(key, None)
    if fut and not fut.done():
        log.info("[orch] ← tutorial-agent: '%s' (%ds)", msg.video_title, msg.duration_seconds)
        fut.set_result(msg)
    else:
        log.warning("[orch] Received unexpected TutorialSearchResponse for session=%s", msg.session_id)


# ── Main pipeline handler ─────────────────────────────────────────────────────

def _chat_reply(text: str, *, end_session: bool = True) -> ChatMessage:
    content: list = [TextContent(type="text", text=text)]
    if end_session:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=content,
    )


@chat_protocol.on_message(model=ChatMessage)
async def orchestrator_chat(ctx: Context, sender: str, msg: ChatMessage):
    """
    Pipeline:
      1. ACK immediately
      2. Parse image + context_text from ChatMessage
      3. Send progress message
      4. Vision LLM → identify part
      5a. Send PartsSourcingRequest  → parts-agent   (asyncio.Future)
      5b. Send TutorialSearchRequest → tutorial-agent (asyncio.Future)
      5c. Await both with timeout; fallback to direct service calls on timeout
      6. Format hero Markdown → send reply

    All steps after the initial ACK are wrapped in a broad try/except so that
    any unhandled error results in a graceful user-facing message rather than
    a silent hang in ASI:One.
    """
    # 1 ── ACK (outside try/except — must always be sent)
    await ctx.send(
        sender,
        ChatAcknowledgement(acknowledged_msg_id=msg.msg_id, timestamp=msg.timestamp),
    )

    try:
        await _run_pipeline(ctx, sender, msg)
    except Exception as exc:  # noqa: BLE001
        log.exception("[orch] Unhandled error in pipeline: %s", exc)
        try:
            await ctx.send(sender, _chat_reply(
                "Sorry, something went wrong on my end. "
                "Please try again in a moment — or check the agent logs for details."
            ))
        except Exception:
            pass


async def _run_pipeline(ctx: Context, sender: str, msg: ChatMessage) -> None:
    """Inner pipeline — called by orchestrator_chat inside a broad try/except."""
    # 2 ── Parse inputs
    context_text, image_b64 = await extract_diagnostic_inputs(msg)
    log.info("[orch] context=%r | has_image=%s", context_text, bool(image_b64))

    if not image_b64:
        await ctx.send(sender, _chat_reply(
            "Please **attach a photo** of the broken part (or paste an image URL) "
            "and include your appliance or vehicle model "
            "(e.g. `Whirlpool WRF535SWHZ00`).",
        ))
        return

    if not context_text:
        await ctx.send(sender, _chat_reply(
            "Got the photo — please also tell me your **appliance or vehicle model** "
            "(e.g. `Whirlpool WRF535SWHZ00` or `2018 Honda Civic`).",
        ))
        return

    # Truncate for the progress message only — protect against very long inputs
    _preview = context_text[:80] + ("…" if len(context_text) > 80 else "")

    # 3 ── Progress message (prevents ASI:One timeout)
    await ctx.send(sender, _chat_reply(
        f"Analysing your photo against **{_preview}** — "
        "identifying the part, checking prices and finding a tutorial...",
        end_session=False,
    ))

    # 4 ── Vision LLM
    log.info("[orch] Calling Vision LLM — context=%r", context_text)
    vision = await extract_part_diagnosis(image_b64, context_text)
    verr = validate_diagnosis(vision)
    if verr:
        await ctx.send(sender, _chat_reply(
            f"Could not identify the part from the photo: {verr}. "
            "Please try a clearer image."
        ))
        return

    log.info(
        "[orch] Vision: part=%s (%s) labor=$%.2f conf=%.0f%%",
        vision["part_name"], vision["part_number"],
        vision["estimated_labor_cost"], vision["confidence"] * 100,
    )

    # 5 ── Fan-out: worker agents (scatter-gather) + direct fallback
    search_query = (
        f"{context_text} {vision['part_name']} {vision['part_number']} replacement repair tutorial"
    ).strip()

    parts_addr = _resolve_worker_address(
        "PARTS", "parts-sourcing-agent", "parts sourcing worker agent seed phrase one"
    )
    tut_addr = _resolve_worker_address(
        "TUTORIAL", "tutorial-agent", "tutorial youtube worker agent seed two"
    )

    session_id = str(uuid4())
    log.info(
        "[orch] Scatter-gather → parts=%s | tutorial=%s | session=%s",
        parts_addr[:20], tut_addr[:20], session_id,
    )

    # Create futures before sending so there is no window where the response
    # arrives before the future is registered.
    loop = asyncio.get_running_loop()
    parts_fut: asyncio.Future[PartsSourcingResponse] = loop.create_future()
    tut_fut:   asyncio.Future[TutorialSearchResponse] = loop.create_future()
    _pending[f"parts:{session_id}"] = parts_fut
    _pending[f"tut:{session_id}"]   = tut_fut

    await ctx.send(
        parts_addr,
        PartsSourcingRequest(
            part_name=str(vision["part_name"]),
            part_number=str(vision["part_number"]),
            context_text=context_text,
            session_id=session_id,
        ),
    )
    await ctx.send(
        tut_addr,
        TutorialSearchRequest(
            search_query=search_query,
            session_id=session_id,
        ),
    )
    log.info("[orch] → sent requests to workers (timeout=%.0fs)", _WORKER_TIMEOUT_S)

    # Gather both responses; fallback to direct calls on timeout
    try:
        parts_resp, tut_resp = await asyncio.wait_for(
            asyncio.gather(parts_fut, tut_fut),
            timeout=_WORKER_TIMEOUT_S,
        )
        def _source_to_dict(s) -> dict:
            """Convert PartSource (uAgents Model) or plain dict to a plain dict."""
            if isinstance(s, dict):
                return s
            return {
                "source_site":  getattr(s, "source_site", ""),
                "price_usd":    float(getattr(s, "price_usd", 0)),
                "purchase_url": getattr(s, "purchase_url", ""),
                "stock_status": getattr(s, "stock_status", ""),
            }

        parts_dict = {
            "price_usd":    parts_resp.price_usd,
            "purchase_url": parts_resp.purchase_url,
            "stock_status": parts_resp.stock_status,
            "source_site":  parts_resp.source_site,
            "all_sources":  [_source_to_dict(s) for s in (parts_resp.all_sources or [])],
            "excel_path":   parts_resp.excel_path,
        }
        tut_dict = {
            "video_url":        tut_resp.video_url,
            "video_title":      tut_resp.video_title,
            "duration_seconds": tut_resp.duration_seconds,
        }
        log.info(
            "[orch] Gather complete via workers — parts=$%.2f | tutorial='%s'",
            parts_dict["price_usd"], tut_dict["video_title"],
        )

    except asyncio.TimeoutError:
        # Workers not running or too slow — clean up leaked futures and fall back
        _pending.pop(f"parts:{session_id}", None)
        _pending.pop(f"tut:{session_id}",   None)
        log.warning(
            "[orch] Workers timed out after %.0fs — falling back to direct service calls. "
            "Start workers/parts_agent.py and workers/tutorial_agent.py to enable worker mode.",
            _WORKER_TIMEOUT_S,
        )
        direct_parts, (vurl, vtitle, vdur) = await asyncio.gather(
            fetch_parts_deterministic(
                str(vision["part_name"]),
                str(vision["part_number"]),
                context_text,
            ),
            find_best_tutorial_video(search_query),
        )
        parts_dict = dict(direct_parts)
        tut_dict   = {"video_url": vurl, "video_title": vtitle, "duration_seconds": vdur}
        log.info(
            "[orch] Direct fallback — parts=$%.2f | tutorial='%s'",
            parts_dict["price_usd"], tut_dict["video_title"],
        )

    # 6 ── Hero response
    markdown = format_diagnostic_markdown(vision, parts_dict, tut_dict)
    log.info("[orch] Sending final response to %s", sender)
    await ctx.send(sender, _chat_reply(markdown))


@chat_protocol.on_message(model=ChatAcknowledgement)
async def on_chat_ack(_ctx: Context, _sender: str, _msg: ChatAcknowledgement):
    """No-op — required by Chat Protocol spec."""
    return


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    os.chdir(PROJECT_ROOT)

    port            = _agent_port()
    av_key          = os.getenv("AGENTVERSE_API_KEY", "").strip()
    seed            = os.getenv("ORCHESTRATOR_AGENT_SEED", "repair orchestrator gateway seed phrase four")
    public_endpoint = os.getenv("AGENT_ENDPOINT", "").strip()

    _free_port(port)

    # Derive worker addresses for logging
    parts_seed = os.getenv("PARTS_AGENT_SEED", "parts sourcing worker agent seed phrase one")
    tut_seed   = os.getenv("TUTORIAL_AGENT_SEED", "tutorial youtube worker agent seed two")
    parts_addr = _address_from_seed("parts-sourcing-agent", parts_seed)
    tut_addr   = _address_from_seed("tutorial-agent", tut_seed)

    use_mailbox = bool(av_key) and not public_endpoint
    orchestrator = Agent(
        name="repair-orchestrator",
        seed=seed,
        port=port,
        endpoint=[public_endpoint] if public_endpoint else None,
        mailbox=use_mailbox,
        **({"agentverse": {"api_key": av_key}} if av_key else {}),
        network=_network(),
        registration_policy=AlmanacApiRegistrationPolicy(),
    )

    orchestrator.include(chat_protocol, publish_manifest=True)
    orchestrator.include(worker_protocol, publish_manifest=False)

    @orchestrator.on_event("startup")
    async def _startup(_ctx: Context) -> None:
        log.info("[orch] Mailbox active — waiting for messages from ASI:One")

    @orchestrator.on_interval(period=30.0)
    async def _heartbeat(_ctx: Context) -> None:
        log.info("[orch] ♥ alive — mailbox polling | address=%s", orchestrator.address)

    inspector_url = (
        f"https://agentverse.ai/inspect/"
        f"?uri=http%3A//127.0.0.1%3A{port}"
        f"&address={orchestrator.address}"
    )

    log.info("=" * 60)
    log.info("Appliance / Auto Whisperer  —  Orchestrator")
    log.info("=" * 60)
    log.info("Network    : %s", _network())
    log.info("Mailbox    : %s", "enabled" if use_mailbox else "disabled")
    log.info("Address    : %s", orchestrator.address)
    log.info("")
    log.info("Workers (start BEFORE this agent):")
    log.info("  parts-agent    port=8002  address=%s", parts_addr)
    log.info("  tutorial-agent port=8003  address=%s", tut_addr)
    log.info("")
    log.info("Worker timeout : %.0fs (set WORKER_TIMEOUT_S to adjust)", _WORKER_TIMEOUT_S)
    log.info("Fallback       : direct service calls if workers don't respond in time")
    log.info("")
    log.info("Inspector / reconnect mailbox if needed:")
    log.info("  %s", inspector_url)
    log.info("=" * 60)

    orchestrator.run()


if __name__ == "__main__":
    main()
