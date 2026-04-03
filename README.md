# Appliance / Auto Whisperer — Right-to-Repair Agent

> **"That is the defrost thermostat. A repairman will charge $250.  
> You can buy the part for $18 [here](https://www.repairclinic.com) and fix it yourself in 5 minutes [▶ Watch](https://youtube.com)."**

A Fetch.ai multi-agent system that turns a photo of a broken part into a complete DIY repair plan — identifying the part with Gemini Vision, sourcing the cheapest price via Bright Data, and finding the best YouTube tutorial, all in parallel.

---

## Architecture

```
ASI:One / Agentverse
       │  ChatMessage (photo + model string)
       ▼
┌─────────────────────────────────────┐
│   repair-orchestrator  (PUBLIC)     │  Chat Protocol  ← only agent visible to ASI:One
│                                     │
│  1. Extract context + image         │
│  2. Call OpenAI gpt-4o Vision       │  → structured JSON: part_name, part_number,
│     (strict JSON mode)              │    estimated_labor_cost, confidence, issue_summary
│                                     │
│  3. Fan out (asyncio.gather):       │
│     ┌───────────────────────────┐   │
│     │ parts-sourcing-agent      │   │  PartsSourcingRequest  →  PartsSourcingResponse
│     │ (Bright Data Web Unlocker)│   │  price_usd, purchase_url, stock_status
│     └───────────────────────────┘   │
│     ┌───────────────────────────┐   │
│     │ tutorial-agent            │   │  TutorialSearchRequest  →  TutorialSearchResponse
│     │ (YouTube Data API v3)     │   │  video_url, video_title, duration_seconds
│     └───────────────────────────┘   │
│                                     │
│  4. Calculate savings               │  labor_cost − part_price = total_saved
│  5. Format hero Markdown            │
│  6. Return final ChatMessage        │  → EndSessionContent closes ASI:One session
└─────────────────────────────────────┘
         ↑ internal (publish_manifest=False)
```

**Key design choices:**
- ASI:One talks to **exactly one agent** (`repair-orchestrator`).
- **OpenAI Python SDK** is used as the unified LLM client — Gemini vision is accessed via Gemini's OpenAI-compatible endpoint (`generativelanguage.googleapis.com/v1beta/openai/`), not the Google ADK. No Anthropic/Claude SDK is used.
- Bright Data and YouTube calls are **deterministic** — no AI in the scraping/API layer.
- The orchestrator sends `PartsSourcingRequest` / `TutorialSearchRequest` to the worker agents via **uAgents scatter-gather** (asyncio.Future keyed by session_id). If workers don't respond within `WORKER_TIMEOUT_S` seconds it falls back to direct service calls automatically, so the agent is always available.

---

## Project Structure

```
appliance-auto-whisperer/
├── diagnostic_bureau.py          ← Orchestrator entry point (Chat Protocol + scatter-gather)
├── workers/
│   ├── parts_agent.py            ← parts-sourcing-agent (Bright Data → price results)
│   └── tutorial_agent.py         ← tutorial-agent (YouTube Data API v3)
├── app/
│   ├── config/
│   │   └── settings.py           ← Pydantic Settings (reads .env)
│   ├── services/
│   │   ├── openai/
│   │   │   └── vision_part_extractor.py   ← Vision LLM (Gemini/GPT-4o via OpenAI SDK)
│   │   ├── brightdata/
│   │   │   └── part_price_service.py      ← Bright Data Web Unlocker proxy
│   │   └── youtube/
│   │       └── instructor_service.py      ← YouTube Data API v3 ranking
│   └── uagents_protocol/
│       ├── schemas.py            ← PartsSourcingRequest/Response, TutorialSearch*
│       ├── chat_inbound.py       ← Parse ChatMessage → context_text + image_b64
│       └── final_markdown.py     ← Build hero Markdown response
├── docker-compose.yml            ← Multi-container local deployment (bureau + rest profiles)
├── docker-entrypoint.sh          ← Single-container startup (all 3 agents in one dyno)
├── Dockerfile.bureau             ← Docker image for all 3 bureau services
├── Dockerfile                    ← Docker image for REST API only
├── render.yaml                   ← Render.com service definition (mailbox mode)
├── .env.example                  ← Copy to .env and fill in API keys
└── requirements.txt
```

---

## Worker Communication (Scatter-Gather)

When you run **all three processes** (parts-agent, tutorial-agent, orchestrator), the orchestrator communicates with them via the **uAgents message protocol**:

1. Orchestrator sends `PartsSourcingRequest` to `parts-sourcing-agent` and `TutorialSearchRequest` to `tutorial-agent` simultaneously.
2. Each worker processes the request and sends back `PartsSourcingResponse` / `TutorialSearchResponse`.
3. The orchestrator awaits both via `asyncio.Future` keyed by `session_id` (up to `WORKER_TIMEOUT_S` seconds).
4. If workers don't respond in time (e.g. not running), the orchestrator **falls back automatically** to calling the service functions directly — the agent stays available regardless.

> You will see `← parts-agent:` and `← tutorial-agent:` log lines on the orchestrator when communication is working correctly.

---

## Quick Start (Local)

### 1. Prerequisites

```powershell
# Python 3.11+ from python.org (not MSYS/Anaconda — needed for prebuilt wheels on Windows)
python --version   # should print 3.11.x or 3.12.x
```

### 2. Virtual environment + dependencies

```powershell
cd "c:\Users\strip\Documents\Cursor\Fetch.ai - Agent Project\appliance-auto-whisperer"

# Activate the shared venv
& "c:\Users\strip\Documents\Cursor\Fetch.ai - Agent Project\.venv-py313\Scripts\Activate.ps1"

pip install -r requirements.txt
pip install -e ".[dev]"
```

### 3. Configure `.env`

```powershell
cp .env.example .env
```

Open `.env` and fill in at minimum:

| Variable | Where to get it |
|---|---|
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) (free tier) |
| `GEMINI_MODEL` | `models/gemini-2.5-flash` (default) |
| `YOUTUBE_API_KEY` | Google Cloud Console → Enable *YouTube Data API v3* → Create API key |
| `BRIGHTDATA_CUSTOMER_ID` | [brightdata.com/cp/zones](https://brightdata.com/cp/zones) |
| `BRIGHTDATA_API_TOKEN` | Same Bright Data dashboard |
| `BRIGHTDATA_ZONE` | Zone name (e.g. `web_unlocker1`) |
| `AGENTVERSE_API_KEY` | [agentverse.ai](https://agentverse.ai) → Account → API Keys |
| `ORCHESTRATOR_AGENT_SEED` | Run `python -c "import secrets; print(secrets.token_hex(32))"` |
| `PARTS_AGENT_SEED` | Same |
| `TUTORIAL_AGENT_SEED` | Same |

### 4. Run — 3 separate terminals

Each agent is a standalone process. Open **3 PowerShell tabs** in the project folder:

**Terminal 1 — Parts-Sourcing Worker (port 8002)**
```powershell
python workers/parts_agent.py
```

**Terminal 2 — Tutorial Worker (port 8003)**
```powershell
python workers/tutorial_agent.py
```

**Terminal 3 — Orchestrator / ASI:One gateway (port 8001, mailbox)**
```powershell
python diagnostic_bureau.py
```

> Start workers **before** the orchestrator. They need ~5 seconds to register on the Almanac before the orchestrator begins routing to them.

Expected orchestrator startup output:

```
============================================================
Appliance / Auto Whisperer  —  Orchestrator
============================================================
Network    : testnet
Mode       : mailbox (Agentverse)
Agentverse : configured

PUBLIC  repair-orchestrator   → agent1q...
WORKER  parts-sourcing-agent  → agent1q...
WORKER  tutorial-agent        → agent1q...

Copy the repair-orchestrator address above and register it in Agentverse
so ASI:One can discover it.
============================================================
```

---

## Register with Agentverse (so ASI:One can find it)

1. Go to [agentverse.ai](https://agentverse.ai) → **My Agents** → **Register External Agent**.
2. Paste the `repair-orchestrator` address printed at startup.
3. Give it a name, description, and the Chat Protocol manifest (auto-published when `publish_manifest=True`).
4. In ASI:One, search for the agent name — you should be able to chat with it directly.

---

## Send a test message via ASI:One

1. Open ASI:One and start a chat with **Appliance Auto Whisperer**.
2. Attach a photo (e.g. a cracked fridge bin, a weird dashboard light).
3. Type the model string: `Whirlpool WRF535SWHZ00`.
4. Hit send — you'll get:
   - An immediate acknowledgement
   - A "Analysing your photo..." progress message
   - The final hero response with diagnosis, cost breakdown, buy link, and tutorial link

### Example output

```markdown
### 🔍 Diagnostic Complete

**Identified Part:** Evaporator Fan Motor
**Part Number:** `W10312696`

> Fan motor has seized — fridge not cooling properly.

*Confidence: 🟢 91% confident*

---

### 💰 Cost Breakdown

|  |  |
| :--- | ---: |
| Standard repairman estimate | **$250.00** |
| DIY part cost (repairclinic.com) | **$34.99** |
| **Total savings** | **+$215.01** |

---

### 🛒 Buy the Part
**[Order Replacement Part — Evaporator Fan Motor](https://www.repairclinic.com/...)**
*Stock: in_stock*

---

### 🎬 How to Fix It Yourself
**[Whirlpool Refrigerator Evaporator Fan Motor Replacement](https://youtube.com/watch?v=...)**  · 6m 12s

---
*You've got this! 🛠️*
```

---

## Deploy with Docker (local)

Copy `.env.example` to `.env` and fill in required keys, then choose a mode:

**Mode A — Full 3-agent bureau (recommended, proper inter-agent communication):**
```bash
docker-compose --profile bureau up --build
# or: make docker-up
```
Starts `parts-agent` (port 8002) + `tutorial-agent` (port 8003) + `orchestrator` (port 8001).
Workers come up first; orchestrator waits for both to be healthy before starting.

**Mode B — REST API only (no uAgents, simpler):**
```bash
docker-compose --profile rest up --build
# or: make docker-up-rest
```
REST endpoint at `http://localhost:8000/health` and `POST /v1/chat`.

**Mode C — Single container (all 3 in one, Render-style):**
```bash
docker build -f Dockerfile.bureau -t whisperer-bureau .
docker run --env-file .env -e PORT=8001 -p 8001:8001 \
  --entrypoint /app/docker-entrypoint.sh whisperer-bureau
```

---

## Deploy to Render (mailbox mode)

1. Push this folder to a GitHub repo.
2. In Render dashboard: **New → Web Service → Docker** → point at the repo, set `Dockerfile Path` to `./Dockerfile.bureau`.
3. Add environment variables from `.env` (Render encrypts secrets). Leave `AGENT_ENDPOINT` blank.
4. Deploy — Render injects `PORT`; the bureau reads it automatically.
5. Note the orchestrator address from Render logs and register it in Agentverse (see above).

---

## Run tests

```powershell
python -m pytest tests/ -v
```

---

## Troubleshooting

### Port already in use (`[Errno 10048]`)

```powershell
# Find and kill the process on port 8000
$p = (Get-NetTCPConnection -LocalPort 8000 -State Listen -EA SilentlyContinue).OwningProcess
if ($p) { Stop-Process -Id $p -Force; "Killed PID $p" } else { "Port 8000 is free" }
```

### `jiter` build failure on Windows (OpenAI dependency)

Use Python from **python.org** (not MSYS/Conda) so prebuilt wheels are available, or run inside Docker (`docker build -f Dockerfile.bureau -t whisperer . && docker run --env-file .env -e PORT=8001 -p 8001:8001 whisperer`).

### Bright Data returns no price

Verify `BRIGHTDATA_CUSTOMER_ID`, `BRIGHTDATA_API_TOKEN`, and `BRIGHTDATA_ZONE` are set. Check that the zone type is **Web Unlocker**. The proxy URL format is:  
`brd-customer-{CUSTOMER_ID}-zone-{ZONE}:{TOKEN}@brd.superproxy.io:22225`

### YouTube returns a placeholder link

Set `YOUTUBE_API_KEY`. Enable the **YouTube Data API v3** in Google Cloud Console for the project that owns the key.
