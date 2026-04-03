"""
Parts-Sourcing Worker Agent — run in its own terminal.

Quickstart (three terminals):
  Terminal 1:  python workers/parts_agent.py
  Terminal 2:  python workers/tutorial_agent.py
  Terminal 3:  python diagnostic_bureau.py   ← orchestrator

  Or use the launcher:  python run.py

Routing modes (mutually exclusive — matching pdf-podcast-agent pattern):
  • AGENTVERSE_API_KEY set  → mailbox mode.  The Agentverse relay delivers
    messages from the orchestrator (also in mailbox mode).
  • No key (Docker / LAN)  → direct-HTTP endpoint. Set PARTS_AGENT_HOST if
    the worker isn't on 127.0.0.1 (e.g. container hostname).
"""
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from uagents import Agent, Context, Protocol
from uagents.registration import AlmanacApiRegistrationPolicy

from app.services.brightdata.part_price_service import fetch_parts_deterministic
from app.uagents_protocol.schemas import PartSource, PartsSourcingRequest, PartsSourcingResponse

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("parts-agent")

# ── Agent ─────────────────────────────────────────────────────────────────────

PORT   = int(os.getenv("PARTS_AGENT_PORT", "8002"))
SEED   = os.getenv("PARTS_AGENT_SEED", "parts sourcing worker agent seed phrase one")
HOST   = os.getenv("PARTS_AGENT_HOST", "127.0.0.1")
AV_KEY = os.getenv("AGENTVERSE_API_KEY", "").strip()

# mailbox (key as value) OR direct endpoint — never both.
# Passing both triggers "Endpoint configuration overrides mailbox setting"
# and silently disables the mailbox, so we follow the pdf-podcast-agent pattern:
#   • AV_KEY set + no custom HOST (local dev) → mailbox via Agentverse relay
#   • PARTS_AGENT_HOST set (Docker/LAN)       → direct HTTP to that hostname
#   • no AV_KEY                               → direct HTTP to localhost
_use_mailbox = bool(AV_KEY) and HOST == "127.0.0.1"

parts_agent = Agent(
    name="parts-sourcing-agent",
    seed=SEED,
    port=PORT,
    **({
        "mailbox": AV_KEY,                              # Agentverse relay mode
    } if _use_mailbox else {
        "endpoint": [f"http://{HOST}:{PORT}/submit"],   # direct HTTP mode
    }),
    registration_policy=AlmanacApiRegistrationPolicy(),
)

# ── Protocol ──────────────────────────────────────────────────────────────────

parts_protocol = Protocol(name="PartsSourcingProtocol", version="0.3.0")


@parts_protocol.on_message(model=PartsSourcingRequest, replies=PartsSourcingResponse)
async def handle_parts_request(ctx: Context, sender: str, msg: PartsSourcingRequest):
    log.info("[parts] Request received: part=%s (%s)", msg.part_name, msg.part_number)
    try:
        d = await fetch_parts_deterministic(msg.part_name, msg.part_number, msg.context_text)
        excel_path = str(d.get("excel_path") or "")
        all_sources = [
            PartSource(
                source_site=str(s.get("source_site", "")),
                price_usd=float(s.get("price_usd", 0)),
                purchase_url=str(s.get("purchase_url", "")),
                stock_status=str(s.get("stock_status", "")),
            )
            for s in d.get("all_sources", [])
        ]
        resp = PartsSourcingResponse(
            price_usd=float(d["price_usd"]),
            purchase_url=str(d["purchase_url"]),
            stock_status=str(d["stock_status"]),
            source_site=str(d["source_site"]),
            all_sources=all_sources,
            excel_path=excel_path,
            session_id=msg.session_id,
        )
        log.info("[parts] Done — $%.2f at %s (%d sources)", d["price_usd"], d["source_site"], len(all_sources))
    except Exception as exc:  # noqa: BLE001
        log.exception("[parts] Service error — sending empty response: %s", exc)
        resp = PartsSourcingResponse(
            price_usd=0.0,
            purchase_url="",
            stock_status="error",
            source_site="error",
            all_sources=[],
            excel_path="",
            session_id=msg.session_id,
        )

    await ctx.send(sender, resp)
    log.info("[parts] Response sent back to orchestrator")


parts_agent.include(parts_protocol, publish_manifest=False)

# ── Entry point ───────────────────────────────────────────────────────────────

@parts_agent.on_event("startup")
async def startup(ctx: Context):
    log.info("Parts-Sourcing Agent ready")
    log.info("  Address  : %s", ctx.agent.address)
    log.info("  Endpoint : http://%s:%d/submit", HOST, PORT)
    log.info("  Mailbox  : %s", "enabled" if _use_mailbox else "disabled (direct HTTP)")


if __name__ == "__main__":
    log.info("Starting parts-sourcing-agent on port %d ...", PORT)
    parts_agent.run()
