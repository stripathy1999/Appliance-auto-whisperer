"""
Appliance / Auto Whisperer — Orchestrator Agent

Receives ChatMessage from ASI:One via Agentverse mailbox, runs the full
pipeline (Vision → Parts → Tutorial) and sends a Markdown hero response.

Run order (3 separate terminals):
  1. python workers/parts_agent.py
  2. python workers/tutorial_agent.py
  3. python diagnostic_bureau.py          ← this file
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

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

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("orchestrator")


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _network() -> str:
    return "mainnet" if os.getenv("AGENT_NETWORK", "testnet").lower() == "mainnet" else "testnet"


def _agent_port() -> int:
    return int(os.getenv("ORCHESTRATOR_AGENT_PORT", "8001"))


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


# ──────────────────────────────────────────────────────────────────────────────
# Chat Protocol
# ──────────────────────────────────────────────────────────────────────────────

chat_protocol = Protocol(spec=chat_protocol_spec)


def _chat_reply(text: str, *, end_session: bool = True) -> ChatMessage:
    content: list = [TextContent(type="text", text=text)]
    if end_session:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=None,
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
      5. Parts scraping + YouTube tutorial (parallel, direct service calls)
      6. Format hero Markdown → send reply
    """
    # 1 ── ACK
    await ctx.send(
        sender,
        ChatAcknowledgement(acknowledged_msg_id=msg.msg_id, timestamp=msg.timestamp),
    )

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

    # 3 ── Progress message (prevents ASI:One timeout)
    await ctx.send(sender, _chat_reply(
        f"Analysing your photo against **{context_text}** — "
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

    # 5 ── Parts + Tutorial in parallel (direct service calls)
    search_query = (
        f"{context_text} {vision['part_name']} {vision['part_number']} replacement repair tutorial"
    ).strip()

    log.info("[orch] Fetching parts + tutorial in parallel...")
    parts_result, (vurl, title, dur) = await asyncio.gather(
        fetch_parts_deterministic(
            str(vision["part_name"]),
            str(vision["part_number"]),
            context_text,
        ),
        find_best_tutorial_video(search_query),
    )

    parts_dict = dict(parts_result)
    tut_dict   = {"video_url": vurl, "video_title": title, "duration_seconds": dur}

    log.info("[orch] Parts: $%.2f at %s", parts_dict["price_usd"], parts_dict["source_site"])
    log.info("[orch] Tutorial: '%s'", title)

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

    port           = _agent_port()
    av_key         = os.getenv("AGENTVERSE_API_KEY", "").strip()
    seed           = os.getenv("ORCHESTRATOR_AGENT_SEED", "repair orchestrator gateway seed phrase four")
    public_endpoint = os.getenv("AGENT_ENDPOINT", "").strip()

    _free_port(port)

    # Derive worker addresses for logging (workers run in separate terminals)
    parts_seed = os.getenv("PARTS_AGENT_SEED", "parts sourcing worker agent seed phrase one")
    tut_seed   = os.getenv("TUTORIAL_AGENT_SEED", "tutorial youtube worker agent seed two")
    parts_addr = _address_from_seed("parts-sourcing-agent", parts_seed)
    tut_addr   = _address_from_seed("tutorial-agent", tut_seed)

    # mailbox=True  → messages from ASI:One arrive via Agentverse mailbox polling
    # endpoint=None → never set a local endpoint when using mailbox; doing so
    #                 disables the mailbox and breaks ASI:One delivery
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
    log.info("Inspector / reconnect mailbox if needed:")
    log.info("  %s", inspector_url)
    log.info("=" * 60)

    orchestrator.run()


if __name__ == "__main__":
    main()
