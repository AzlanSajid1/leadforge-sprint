# LEADFORGE — Stage 1: Collect the Leads

## Overview

Stage 1 collects business leads using the OpenStreetMap Overpass API.

The pipeline:

Overpass API
→ Business information
→ Website URL
→ Website text extraction
→ Deduplication
→ Lead IDs
→ JSONL output

## Main File

`stages/01_collect.py`

## Output

The collector generates:

`data/01_leads.jsonl`

Each lead contains:

- lead_id
- name
- domain
- city
- category
- phone
- site_text

## Technologies

- Python 3.11
- Requests
- Trafilatura
- RapidFuzz
- Overpass API

## Current Status

Stage 1 collector logic has been implemented.

During testing, the Overpass API endpoint returned HTTP 406.
The issue is currently being investigated and will be resolved separately.

The file `stages/debug_test.py` contains the current Overpass API debugging test.

## Test Command

```bash
python stages/01_collect.py --category amenity=car_repair --city Lahore --limit 5