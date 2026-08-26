# LeadForge Sprint

A working lead-generation pipeline, built by seven interns in four days.

---

## Overview

Given a single business category and a single city, the pipeline:

1. Collects real businesses from a public API
2. Audits each business's website (screenshots + automated health checks)
3. Uses an LLM to extract specific, verified findings about each site
4. Scores every lead 0–100 on outreach potential
5. Drafts a personalized outreach email for the strongest leads
6. Puts every draft in front of a human for approval, edit, or rejection
7. Delivers approved messages to a sandbox test inbox

No real business is ever contacted — all outbound mail goes through a Mailtrap sandbox.

Each stage is a standalone script that reads the file the previous stage wrote and writes a new one. No APIs or database between stages — when something breaks, you open the file and look at it.

## Pipeline

```
OpenStreetMap Overpass API (one city + one business category)
        │
        ▼
STAGE 1 · COLLECT      → data/01_leads.jsonl
STAGE 2 · LOOK         → data/02_visual.jsonl
STAGE 3 · RESEARCH     → data/03_research.jsonl
STAGE 4 · SCORE        → data/04_scored.jsonl
STAGE 5 · WRITE        → data/05_drafts.jsonl
DASHBOARD (review)     → data/06_approved.jsonl
RUNNER + DELIVERY      → Mailtrap sandbox inbox
```

## Repository structure

```
leadforge-sprint/
├── contracts/
│   └── lead_schema.json      # Shared record contract — every field, who owns it
├── data/
│   ├── sample_10.jsonl       # 10 fake leads, fully populated — unblocks everyone from Day 0
│   ├── 01_leads.jsonl
│   ├── 02_visual.jsonl
│   ├── 03_research.jsonl
│   ├── 04_scored.jsonl
│   ├── 05_drafts.jsonl
│   └── 06_approved.jsonl
├── screenshots/
│   ├── {lead_id}_d.png       # Desktop screenshot
│   └── {lead_id}_m.png       # Mobile screenshot
├── stages/
│   ├── 01_collect.py
│   ├── 02_visual.py
│   ├── 03_research.py
│   ├── 04_score.py
│   └── 05_write.py
├── app.py                    # Streamlit review dashboard
├── run_pipeline.py           # Runs all five stages in order
├── send_approved.py          # Delivers approved drafts to the Mailtrap sandbox
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

`data/` is gitignored except `sample_10.jsonl`. `.env` (real API keys) is gitignored — only `.env.example` is committed. `stages/` holds Stages 1–5; the dashboard and runner live at the repo root since they sit outside the linear stage chain.

## Shared data format

Every record is one line of JSON. Each stage reads the record the previous stage wrote, **copies every existing field through untouched**, and adds only its own new fields.

Full contract: [`contracts/lead_schema.json`](contracts/lead_schema.json). Fully-populated example: [`data/sample_10.jsonl`](data/sample_10.jsonl) — lets any stage be built and tested independently from Day 0.

## Setup

```bash
git clone <repo-url>
cd leadforge-sprint
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in Gemini / Groq / Mailtrap credentials
```

## Running it

```bash
python stages/03_research.py --input data/sample_10.jsonl --limit 3   # one stage, sample data
python run_pipeline.py                                                # full pipeline
streamlit run app.py                                                  # review dashboard
python send_approved.py                                               # deliver to sandbox inbox
```

## Rules that don't bend

- No real business is ever contacted — Mailtrap sandbox only
- Only public business info (name, address, phone, website) — no personal data
- Respect `robots.txt`, one request/second per site, identify the crawler in the user agent
- Never invent a fact — enforced by Stage 3's quote verification

## Git workflow

- One branch per person, named after their stage (`stage-1-collect`, `dashboard`, `runner`, ...)
- Merge into `main` at least once a day
- Never commit `data/` (except `sample_10.jsonl`) or `.env`
- No formal review this week — merge, then post what you merged in the team chat
