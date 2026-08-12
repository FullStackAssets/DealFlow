# DealFlow — Bristol County Wholesale Pipeline

Single-file, stdlib-only Python engine. SQLite state machine:
`LEAD → UNDERWRITTEN → OFFER_SENT → UNDER_CONTRACT → ASSIGNED → CLOSED`

## What's automated vs what's you

| Stage | Automated | You |
|---|---|---|
| Sourcing | CSV ingest + dedupe + distress scoring (lis pendens 40, pre-foreclosure 35, tax lien 30, probate 25…) | Pull lists (MassLandRecords, tax takings, assessor absentee export) |
| Underwriting | ARV via median $/sqft of renovated comps <12mo, repair est by condition tier, MAO = 70% × ARV − repairs − fee − 2% friction | Sanity-check condition tier |
| Negotiation | Offer ladder (88/94/100% of MAO), LOI with MA equitable-interest disclosure baked in, counteroffer math + scripts | Make the call, sign |
| Contract | P&S rider checklist (and/or assigns, disclosure, inspection out, deposit control) | Your attorney executes |
| Dispo | Buyer-box matching (city + max price + min spread, POF-first ranking), assignment doc + blast email | Collect the $2,500 |

## Commands
```
python3 dealflow.py ingest leads.csv
python3 dealflow.py comps comps.csv
python3 dealflow.py buyers buyers.csv
python3 dealflow.py underwrite
python3 dealflow.py offer <id>
python3 dealflow.py counter <id> <their_number>
python3 dealflow.py contract <id> <price>
python3 dealflow.py assign <id>
python3 dealflow.py dashboard      # -> out/dashboard.html
python3 dealflow.py mailer         # -> out/mailer_YYYYMMDD.csv (score>=50, deduped by owner mail)
python3 dealflow.py status
```

## Real data source
`fetch_massgis.py` pulls live from the MassGIS statewide assessor parcel layer
(public ArcGIS REST, no key). Verified quirks handled: per-town USE_CODE formats
(101 vs 1010), sqft in BLD_AREA vs RES_AREA, TOWN_ID keys (Brockton 44,
Fall River 95, New Bedford 201, Taunton 293), and per-town data vintage —
Brockton lags to Dec 2024, hence the 730-day comp window. Lis pendens still
comes from MassLandRecords manual export -> `inbox/leads_*.csv`.

## Automation wiring (iPhone-only stack)
Ready-made workflow at `.github/workflows/dealflow-nightly.yml`: nightly cron +
manual trigger from the GitHub iOS app. Fetches MassGIS fresh, ingests anything
in `inbox/`, underwrites, commits db + dashboard back to the repo.
Drop new lead CSVs into `inbox/` from your phone (Working Copy app), dashboard commits back — view it through GitHub Pages or another independently verified static host. Lead CSVs can come from a Cloudflare Worker email-ingestion pipe (same pattern as HostGraph's invoice intake) if you subscribe to a list provider that emails exports.

## Config
All knobs at the top of `dealflow.py` in `CFG`: fee target/floor, MAO %, repair $/sqft tiers, offer ladder fractions.

## MA legal guardrails (built in, non-negotiable)
- LOI and assignment both carry the equitable-interest disclosure.
- Assignment routes through a closing attorney; MA is an attorney-close state.
- Underwriting refuses to value a lead with <3 usable comps — it logs the block instead of guessing. No fabricated ARVs.
