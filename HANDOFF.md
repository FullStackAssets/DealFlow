# HANDOFF — first Claude Code session

Read CLAUDE.md first. The system works end-to-end; your job is the backlog, not a rewrite.

## Session 1 tasks
1. Run the smoke test from CLAUDE.md and confirm 854 UNDERWRITTEN / 0 blocked.
2. Backlog item 2: neighborhood-level ARV.
   - Extend fetch_massgis.py to request geometry centroids (returnGeometry=true,
     geometryPrecision low, or use the layer's centroid via outStatistics if cheaper).
   - Store lat/lon on comps and leads (schema migration: ALTER TABLE, keep it additive —
     the db is committed state, don't drop it).
   - In arv_for(): prefer comps within ~0.75 mi of the lead (haversine, stdlib math);
     fall back to city-wide median when <3 within radius, and log which mode was used.
   - Keep the <3-comps refusal behavior in both modes.
3. Backlog item 5: `python3 dealflow.py mailer` — CSV of owner name + mailing address
   for all UNDERWRITTEN leads with distress_score >= 50, dedup by owner_mail,
   written to out/mailer_YYYYMMDD.csv.
4. Re-run smoke test, regenerate dashboard, update CLAUDE.md's "Current state" line.

## Constraints reminder
Stdlib only. No fabricated data. Don't touch the legal disclosure text.
Commit in small units; the nightly workflow must stay green throughout.

## Not this session
MassLandRecords scraper (fragile, needs its own session with careful failure isolation).
