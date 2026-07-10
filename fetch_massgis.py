#!/usr/bin/env python3
"""
fetch_massgis.py — pull REAL parcel data from MassGIS statewide assessor layer
into dealflow-ready CSVs. Stdlib only.

  python3 fetch_massgis.py comps "FALL RIVER" "TAUNTON" "NEW BEDFORD" > comps_real.csv
  python3 fetch_massgis.py leads "FALL RIVER" > leads_real.csv

Comps  = arm's-length single-family sales (USE_CODE 101), last 12 months, price > $150K.
Leads  = single-family, absentee owner (owner city != site city or out of state),
         held 15+ years (high equity) OR nominal-price transfer (estate/family signal),
         built pre-1960 (deferred-maintenance proxy).
Source: Massachusetts_Property_Tax_Parcels FeatureServer (MassGIS AGOL, public).
"""
import csv, json, sys, urllib.parse, urllib.request
from datetime import date, timedelta

BASE = ("https://services1.arcgis.com/hGdibHYSPO59RG1h/arcgis/rest/services/"
        "Massachusetts_Property_Tax_Parcels/FeatureServer/0/query")
# Verified against live layer 2026-07-08. USE_CODE format varies by town (101 vs 1010).
TOWN_ID = {"BROCKTON": 44, "FALL RIVER": 95, "NEW BEDFORD": 201, "TAUNTON": 293}
FIELDS = ("SITE_ADDR,CITY,LS_PRICE,LS_DATE,BLD_AREA,YEAR_BUILT,OWNER1,"
          "OWN_ADDR,OWN_CITY,OWN_STATE,TOTAL_VAL,STYLE,NUM_ROOMS,USE_CODE,RES_AREA")

def query(where, offset=0):
    params = urllib.parse.urlencode({
        "where": where, "outFields": FIELDS, "returnGeometry": "false",
        "orderByFields": "LS_DATE DESC", "resultOffset": offset,
        "resultRecordCount": 2000, "f": "json"})
    with urllib.request.urlopen(f"{BASE}?{params}", timeout=90) as r:
        d = json.load(r)
    if "error" in d:
        sys.exit(f"API error: {d['error']}")
    return [f["attributes"] for f in d.get("features", [])], d.get("exceededTransferLimit", False)

def fetch_all(where):
    out, offset = [], 0
    while True:
        rows, more = query(where, offset)
        out += rows
        if not more or not rows:
            return out
        offset += len(rows)

def sqft(a):
    return a["BLD_AREA"] if (a.get("BLD_AREA") or 0) > 0 else a.get("RES_AREA") or 0

def iso(yyyymmdd):
    s = str(yyyymmdd or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else ""

def comps(cities, days=365):
    cutoff = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
    w = csv.writer(sys.stdout)
    w.writerow(["city", "address", "sale_price", "sqft", "sale_date", "condition"])
    for city in cities:
        where = (f"TOWN_ID={TOWN_ID[city]} AND USE_CODE LIKE '101%' AND LS_PRICE>150000 "
                 f"AND LS_DATE>='{cutoff}' AND (BLD_AREA>0 OR RES_AREA>0)")
        for a in fetch_all(where):
            w.writerow([city.title(), a["SITE_ADDR"], a["LS_PRICE"],
                        sqft(a), iso(a["LS_DATE"]), "renovated"])

def leads(cities):
    held_cutoff = (date.today() - timedelta(days=15 * 365)).strftime("%Y%m%d")
    w = csv.writer(sys.stdout)
    w.writerow(["address","city","sqft","beds","baths","owner","owner_mail",
                "signals","condition","asking","notes"])
    for city in cities:
        where = (f"TOWN_ID={TOWN_ID[city]} AND USE_CODE LIKE '101%' AND (BLD_AREA>0 OR RES_AREA>0) "
                 f"AND YEAR_BUILT>0 AND YEAR_BUILT<1960 "
                 f"AND (OWN_CITY<>'{city}' OR OWN_STATE<>'MA') "
                 f"AND (LS_DATE<'{held_cutoff}' OR LS_PRICE<=100)")
        for a in fetch_all(where):
            sig = ["absentee"]
            if str(a.get("LS_DATE") or "99999999") < held_cutoff:
                sig.append("high_equity")
            if (a.get("LS_PRICE") or 0) <= 100:
                sig.append("probate")   # nominal transfer: estate/family deed signal — verify at registry
            if a.get("OWN_STATE") and a["OWN_STATE"] != "MA":
                sig.append("vacant")    # out-of-state proxy — confirm by drive-by
            mail = f"{a.get('OWN_ADDR','')}, {a.get('OWN_CITY','')} {a.get('OWN_STATE','')}"
            notes = (f"assessed {a.get('TOTAL_VAL')}; built {a.get('YEAR_BUILT')}; "
                     f"{a.get('STYLE','')}; last sale {iso(a.get('LS_DATE'))} @ {a.get('LS_PRICE')}")
            w.writerow([a["SITE_ADDR"], city.title(), sqft(a), "", "",
                        a.get("OWNER1",""), mail, ";".join(sig), "medium", "", notes])

if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in ("comps", "leads"):
        sys.exit(__doc__)
    args = sys.argv[2:]
    days = 365
    if args and args[0].startswith("--days="):
        days = int(args.pop(0).split("=")[1])
    if sys.argv[1] == "comps":
        comps([c.upper() for c in args], days)
    else:
        leads([c.upper() for c in args])
