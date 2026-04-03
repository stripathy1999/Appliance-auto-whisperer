"""
Tutorial-Search Worker Agent — run in its own terminal.

Quickstart (three terminals):
  Terminal 1:  python workers/parts_agent.py
  Terminal 2:  python workers/tutorial_agent.py
  Terminal 3:  python diagnostic_bureau.py   ← orchestrator

  Or use the launcher:  python run.py

Routing modes (mutually exclusive — matching pdf-podcast-agent pattern):
  • AGENTVERSE_API_KEY set  → mailbox mode.  The Agentverse relay delivers
    messages from the orchestrator (also in mailbox mode).
  • No key (Docker / LAN)  → direct-HTTP endpoint. Set TUTORIAL_AGENT_HOST
    if the worker isn't on 127.0.0.1 (e.g. container hostname).
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

from app.services.youtube.instructor_service import find_best_tutorial_video
from app.uagents_protocol.schemas import TutorialSearchRequest, TutorialSearchResponse

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("tutorial-agent")

# ── Agent ─────────────────────────────────────────────────────────────────────

PORT   = int(os.getenv("TUTORIAL_AGENT_PORT", "8003"))
SEED   = os.getenv("TUTORIAL_AGENT_SEED", "tutorial youtube worker agent seed two")
HOST   = os.getenv("TUTORIAL_AGENT_HOST", "127.0.0.1")
AV_KEY = os.getenv("AGENTVERSE_API_KEY", "").strip()

# mailbox (key as value) OR direct endpoint — never both.
# Passing both triggers "Endpoint configuration overrides mailbox setting"
# and silently disables the mailbox, so we follow the pdf-podcast-agent pattern:
#   • AV_KEY set + no custom HOST (local dev) → mailbox via Agentverse relay
#   • TUTORIAL_AGENT_HOST set (Docker/LAN)    → direct HTTP to that hostname
#   • no AV_KEY                               → direct HTTP to localhost
_use_mailbox = bool(AV_KEY) and HOST == "127.0.0.1"

tutorial_agent = Agent(
    name="tutorial-agent",
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

instructor_protocol = Protocol(name="TutorialSearchProtocol", version="0.3.0")


@instructor_protocol.on_message(model=TutorialSearchRequest, replies=TutorialSearchResponse)
async def handle_tutorial_request(ctx: Context, sender: str, msg: TutorialSearchRequest):
    log.info("[tutorial] Request received: query=%s", msg.search_query[:80])
    try:
        vurl, title, dur = await find_best_tutorial_video(msg.search_query)
        resp = TutorialSearchResponse(
            video_url=vurl,
            video_title=title,
            duration_seconds=dur,
            session_id=msg.session_id,
        )
        log.info("[tutorial] Done — '%s' %s", title, vurl)
    except Exception as exc:  # noqa: BLE001
        log.exception("[tutorial] Service error — sending empty response: %s", exc)
        resp = TutorialSearchResponse(
            video_url="",
            video_title="Tutorial unavailable",
            duration_seconds=0,
            session_id=msg.session_id,
        )

    await ctx.send(sender, resp)
    log.info("[tutorial] Response sent back to orchestrator")


tutorial_agent.include(instructor_protocol, publish_manifest=False)

# ── Entry point ───────────────────────────────────────────────────────────────

@tutorial_agent.on_event("startup")
async def startup(ctx: Context):
    log.info("Tutorial Agent ready")
    log.info("  Address  : %s", ctx.agent.address)
    log.info("  Endpoint : http://%s:%d/submit", HOST, PORT)
    log.info("  Mailbox  : %s", "enabled" if _use_mailbox else "disabled (direct HTTP)")


if __name__ == "__main__":
    log.info("Starting tutorial-agent on port %d ...", PORT)
    tutorial_agent.run()
