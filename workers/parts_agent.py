"""
Parts-Sourcing Worker Agent — run in its own terminal.

Terminal 1:  python workers/parts_agent.py
Terminal 2:  python workers/tutorial_agent.py
Terminal 3:  python diagnostic_bureau.py        ← orchestrator
"""
import logging
import os
import sys
from pathlib import Path

# Make sure the project root is on sys.path so `app.*` imports work.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from uagents import Agent, Context, Protocol
from uagents.registration import AlmanacApiRegistrationPolicy

from app.services.brightdata.part_price_service import fetch_parts_deterministic, save_parts_excel
from app.uagents_protocol.schemas import PartSource, PartsSourcingRequest, PartsSourcingResponse

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("parts-agent")

# ── Agent ─────────────────────────────────────────────────────────────────────

PORT = int(os.getenv("PARTS_AGENT_PORT", "8002"))
SEED = os.getenv("PARTS_AGENT_SEED", "parts sourcing worker agent seed phrase one")

parts_agent = Agent(
    name="parts-sourcing-agent",
    seed=SEED,
    port=PORT,
    endpoint=[f"http://127.0.0.1:{PORT}/submit"],
    mailbox=False,
    registration_policy=AlmanacApiRegistrationPolicy(),
)

# ── Protocol ──────────────────────────────────────────────────────────────────

parts_protocol = Protocol(name="PartsSourcingProtocol", version="0.3.0")


@parts_protocol.on_message(model=PartsSourcingRequest, replies=PartsSourcingResponse)
async def handle_parts_request(ctx: Context, sender: str, msg: PartsSourcingRequest):
    log.info("[parts] Request: part=%s (%s) context=%r", msg.part_name, msg.part_number, msg.context_text)
    d = await fetch_parts_deterministic(msg.part_name, msg.part_number, msg.context_text)

    excel_path = save_parts_excel(d.get("all_sources", []), msg.part_name, msg.part_number) or ""
    if excel_path:
        log.info("[parts] Excel saved: %s", excel_path)

    all_sources = [
        PartSource(
            source_site=str(s.get("source_site", "")),
            price_usd=float(s.get("price_usd", 0)),
            purchase_url=str(s.get("purchase_url", "")),
            stock_status=str(s.get("stock_status", "")),
        )
        for s in d.get("all_sources", [])
    ]

    await ctx.send(
        sender,
        PartsSourcingResponse(
            price_usd=float(d["price_usd"]),
            purchase_url=str(d["purchase_url"]),
            stock_status=str(d["stock_status"]),
            source_site=str(d["source_site"]),
            all_sources=all_sources,
            excel_path=excel_path,
            session_id=msg.session_id,  # echo back for orchestrator correlation
        ),
    )
    log.info("[parts] Done — $%.2f at %s (%d sources)", d["price_usd"], d["purchase_url"], len(all_sources))


parts_agent.include(parts_protocol, publish_manifest=False)

# ── Entry point ───────────────────────────────────────────────────────────────

@parts_agent.on_event("startup")
async def startup(ctx: Context):
    log.info("Parts-Sourcing Agent ready")
    log.info("  Address : %s", ctx.agent.address)
    log.info("  Endpoint: http://127.0.0.1:%d/submit", PORT)


if __name__ == "__main__":
    log.info("Starting parts-sourcing-agent on port %d ...", PORT)
    parts_agent.run()
