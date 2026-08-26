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

Stage 1 collector logic has been implemented and tested successfully.

The collector was tested using a real Overpass data export for London. Since the live Overpass API was returning HTTP 406/502 errors during development, the `--input-json` offline fallback was used to continue testing with real Overpass data.

Latest test results:

- 300 raw businesses loaded
- 134 businesses had usable websites
- 110 leads remained after domain deduplication
- 0 fuzzy-name duplicates
- 0 missing required fields
- 110 leads written to `data/01_leads.jsonl`
- 75 leads contained extracted `site_text`
- 35 leads had empty `site_text` due to website/extraction failures

The generated `data/01_leads.jsonl` file contains the final Stage 1 output.

## Test Command

Latest successful test:

```bash
python stages/01_collect.py --category amenity=restaurant --bbox "51.28,-0.51,51.70,0.33" --city London --limit 400 --input-json data/overpass_export_london_400.json
