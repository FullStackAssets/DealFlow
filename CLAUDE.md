# CLAUDE.md — DealFlow

## What this is
Wholesale real-estate pipeline for Bristol County MA (+ Brockton). Finds distressed
single-family leads from public assessor data, underwrites ARV/MAO, preps offers and
negotiation math, generates assignment paperwork, matches buyers. Owner: Nic — solo
founder, no local dev environment; everything runs via GitHub Actions and cloud pipelines.

**Current state: WORKING.** 854 real leads underwritten across 4 cities against 1,293
real comps, zero blocked, as of 2026-07-10. Neighborhood-level ARV (0.75mi comp radius
via parcel centroids) and direct-mail export shipped; lat/lon populates on the next
nightly fetch — until then everything prices citywide, by design. Don't refactor what
runs; extend it.

**Data hold (2026-07-10):** the repo is PUBLIC, so `dealflow.db`, `leads_real.csv`,
`comps_real.csv`, and `out/dashboard.html` (real owner names + mailing addresses with
distress annotations) are NOT committed yet, and the nightly cron is commented out in
the workflow. Once Nic makes the repo private (or explicitly okays public data): commit
those four files, uncomment the cron, and the committed-db state-store design in the
file map below applies as written. Until then, regenerate locally via the smoke test.

## File map
- `dealflow.py` — entire engine. Single file, **stdlib only** (deliberate; keep it that way).
  SQLite state machine: LEAD → UNDERWRITTEN → OFFER_SENT → UNDER_CONTRACT → ASSIGNED → CLOSED, DEAD from any stage.
  All tunables in `CFG` dict at top: MAO formula = ARV×0.70 − repairs − $15K fee − 2% friction.
  Repair tiers $/sqft: light 25 / medium 55 / heavy 95 / gut 150. Offer ladder 88/94/100% of MAO.
- `fetch_massgis.py` — pulls real data from MassGIS statewide parcel layer
  (public ArcGIS REST, no key). Outputs dealflow-ready CSVs. Stdlib only.
- `dashboard_template.html` — static dashboard, hand-written CSS. `dealflow.py dashboard`
  injects JSON at the `/*DATA*/` marker → `out/dashboard.html`.
- `.github/workflows/dealflow-nightly.yml` — nightly cron 9:00 UTC + workflow_dispatch:
  fetch → ingest → sweep `inbox/` → underwrite → commit db + dashboard.
- `inbox/` — manual CSV drops (`leads_*.csv`, `buyers_*.csv`), swept by the workflow.
- `dealflow.db` — SQLite, committed to the repo on purpose (it IS the state store;
  ingest dedupes on address so pipeline states survive re-runs).

## Hard-won MassGIS facts (verified live 2026-07-08 — do not "simplify" these away)
Endpoint: `https://services1.arcgis.com/hGdibHYSPO59RG1h/arcgis/rest/services/Massachusetts_Property_Tax_Parcels/FeatureServer/0/query`
- Query by `TOWN_ID`, not `CITY` (CITY is unreliable/null in some towns):
  Brockton 44, Fall River 95, New Bedford 201, Taunton 293.
- `USE_CODE` format varies per town: Fall River/Brockton/New Bedford use `101`,
  Taunton uses `1010`/`1014`. Always `USE_CODE LIKE '101%'`.
- Sqft: Brockton has `BLD_AREA=0`, real value in `RES_AREA`. Use the `sqft()` helper.
  Any field you filter/read MUST be in the `FIELDS` request string or the API silently omits it
  (this bug already bit once — RES_AREA missing from outFields returned sqft 0 everywhere).
- Data vintage lags per town: Brockton newest sale is Dec 2024 (FY2025 roll). Hence
  comp fetch `--days=730` and `CFG["stale_comp_days"]=730`. Fall River is current to Nov 2025.
- `LS_DATE` is a YYYYMMDD **string**. Max 2000 records/page; fetcher paginates via
  `exceededTransferLimit` + `resultOffset`.
- Parcel coords: `returnCentroid=true&outSR=4326` gives a WGS84 centroid per feature
  (verified live 2026-07-10) — no need to pull polygon rings via `returnGeometry`.
- Lead signal proxies from assessor data: absentee (owner city/state mismatch),
  high_equity (held 15+ yrs), probate (nominal ≤$100 transfer — really estate/trust deed,
  verify at registry), vacant (out-of-state owner — verify by drive-by).

## Known gaps / backlog (in priority order)
1. **Lis pendens ingestion** — MassLandRecords (Bristol Fall River/Taunton/North districts)
   has no clean API (ASP.NET postback site). Current path: weekly manual export →
   `inbox/leads_*.csv`. A scraper is possible but fragile; if attempted, make failure
   non-fatal to the nightly run.
2. **Brockton buyer coverage** — buyers table has Brockton entries only in the sample CSV;
   real buyer list is Nic's, arrives via `inbox/buyers_*.csv`.
3. **GitHub Pages** on `/out` for a phone-viewable dashboard URL.

Done 2026-07-10: neighborhood-level ARV (`arv_for()` prefers renovated comps within
`CFG["arv_radius_mi"]`=0.75mi via haversine on parcel centroids, city-wide fallback
when <3 in radius, mode logged per lead, <3-comps refusal intact in both modes;
lat/lon on leads+comps via additive migration) and direct-mail export
(`dealflow.py mailer` → `out/mailer_YYYYMMDD.csv`, score ≥ `CFG["mailer_min_score"]`=50,
deduped on owner_mail). Buyers now upsert on name so the nightly sweep is idempotent.

## Non-negotiables
- **No AI slop**: never fabricate data. Underwriting refuses to value a lead with <3
  usable comps and logs the block instead. Keep that behavior.
- **MA legal guardrails**: equitable-interest disclosure is baked into the LOI and
  assignment templates — never remove it. MA is an attorney-close state; assignment
  routes through a closing attorney. Negotiation/signing are human steps by design;
  do not automate sending offers or executing contracts.
- Owner mailing data is public assessor record; use for direct outreach only, don't
  enrich with anything scraped from non-public sources.
- CSS custom properties must use literal `--` double-hyphens (an em-dash substitution
  once broke every click handler). No Tailwind CDN in HTML artifacts — hand-written CSS.
- Stdlib only in both Python files. If a dependency feels necessary, it probably isn't.
- Nic's style: terse instructions ("Go", "Continue") mean make the architectural call
  and ship complete work; don't ask clarifying questions for reversible decisions.

## Commands
```
python3 fetch_massgis.py comps --days=730 "FALL RIVER" "BROCKTON" "TAUNTON" "NEW BEDFORD" > comps_real.csv
python3 fetch_massgis.py leads "FALL RIVER" "BROCKTON" "TAUNTON" "NEW BEDFORD" > leads_real.csv
python3 dealflow.py ingest leads_real.csv | comps <csv> | buyers <csv>
python3 dealflow.py underwrite | offer <id> | counter <id> <amt> | contract <id> <price> | assign <id>
python3 dealflow.py dashboard | mailer | status
```
Smoke test after any change: `rm -f dealflow.db && python3 dealflow.py ingest leads_real.csv
&& python3 dealflow.py comps comps_real.csv && python3 dealflow.py underwrite && python3 dealflow.py status`
— expect 854 UNDERWRITTEN, 0 LEAD remaining.
