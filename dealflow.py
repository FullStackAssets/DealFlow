#!/usr/bin/env python3
"""
DealFlow — Bristol County wholesale pipeline engine.
Single file, stdlib only. SQLite state machine:

  LEAD -> UNDERWRITTEN -> OFFER_SENT -> UNDER_CONTRACT -> ASSIGNED -> CLOSED
                                     -> DEAD (any stage)

Automated:  sourcing ingest, distress scoring, ARV/MAO underwriting,
            offer ladder + counteroffer math, LOI + assignment doc generation,
            buyer matching, dashboard export.
Human:      sending offers, negotiating live, signing. (MA: disclose
            equitable interest; attorney-close state. This tool preps,
            you execute.)

Usage:
  python3 dealflow.py ingest leads.csv          # import leads (registry/assessor/driving-for-dollars)
  python3 dealflow.py comps comps.csv           # load comp sales
  python3 dealflow.py underwrite                # score + value every LEAD
  python3 dealflow.py offer <lead_id>           # generate offer ladder + LOI
  python3 dealflow.py counter <lead_id> <their_number>   # counteroffer guidance
  python3 dealflow.py contract <lead_id> <agreed_price>  # mark under contract, gen P&S rider notes
  python3 dealflow.py buyers buyers.csv         # load buyer box list
  python3 dealflow.py assign <lead_id>          # match buyers, gen assignment package
  python3 dealflow.py dashboard                 # write dashboard.html
  python3 dealflow.py status                    # pipeline snapshot
"""
import csv, json, math, re, sqlite3, sys, textwrap
from datetime import date
from pathlib import Path

DB = Path(__file__).parent / "dealflow.db"
OUT = Path(__file__).parent / "out"

# ---------------- config (edit here, nowhere else) ----------------
CFG = {
    "assignment_fee_target": 15000,
    "assignment_fee_floor": 7500,
    "mao_pct": 0.70,                 # classic 70% rule
    "closing_cost_pct": 0.02,        # buyer-side friction baked into MAO
    "repair_psf": {"light": 25, "medium": 55, "heavy": 95, "gut": 150},
    "offer_ladder": [0.88, 0.94, 1.00],   # fractions of MAO: anchor, mid, walk-away
    "county": "Bristol County, MA",
    "stale_comp_days": 730,   # matches fetch window; Brockton assessor vintage lags to Dec 2024
    "arv_radius_mi": 0.75,    # neighborhood comp radius; citywide fallback when <3 inside
    "mailer_min_score": 50,   # distress cutoff for direct-mail export
}

DISTRESS_WEIGHTS = {          # signal -> points
    "lis_pendens": 40, "tax_lien": 30, "probate": 25, "absentee": 15,
    "vacant": 20, "code_violation": 20, "pre_foreclosure": 35,
    "divorce": 20, "expired_listing": 10, "high_equity": 15,
}

# ---------------- db ----------------
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS leads(
      id INTEGER PRIMARY KEY, address TEXT UNIQUE, city TEXT, sqft INT, beds INT, baths REAL,
      owner TEXT, owner_mail TEXT, signals TEXT, condition TEXT DEFAULT 'medium',
      asking REAL, notes TEXT, state TEXT DEFAULT 'LEAD',
      distress_score INT, arv REAL, repairs REAL, mao REAL, spread REAL,
      contract_price REAL, assigned_to TEXT, created TEXT DEFAULT CURRENT_DATE);
    CREATE TABLE IF NOT EXISTS comps(
      id INTEGER PRIMARY KEY, city TEXT, address TEXT, sale_price REAL,
      sqft INT, sale_date TEXT, condition TEXT DEFAULT 'renovated');
    CREATE TABLE IF NOT EXISTS buyers(
      id INTEGER PRIMARY KEY, name TEXT, contact TEXT, cities TEXT,
      max_price REAL, min_spread REAL, strategy TEXT, proof_of_funds INT DEFAULT 0);
    CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY, lead_id INT, what TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP);
    """)
    # additive migrations only — the db is committed state, never drop/rebuild
    for table in ("leads", "comps"):
        for col in ("lat", "lon"):
            try:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} REAL")
            except sqlite3.OperationalError:
                pass  # column already exists
    return con

def log(con, lead_id, what):
    con.execute("INSERT INTO events(lead_id,what) VALUES(?,?)", (lead_id, what))

# ---------------- ingest ----------------
def ingest(path):
    """CSV columns: address,city,sqft,beds,baths,owner,owner_mail,signals,condition,asking,notes[,lat,lon]
       signals = semicolon list, e.g. 'lis_pendens;absentee;vacant'"""
    con = db(); n = 0
    for r in csv.DictReader(open(path)):
        sig = ";".join(s.strip() for s in (r.get("signals") or "").split(";") if s.strip())
        try:
            con.execute("""INSERT INTO leads(address,city,sqft,beds,baths,owner,owner_mail,
                        signals,condition,asking,notes,lat,lon) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (r["address"].strip(), r["city"].strip(), int(r.get("sqft") or 0),
                         int(r.get("beds") or 0), float(r.get("baths") or 0),
                         r.get("owner",""), r.get("owner_mail",""), sig,
                         (r.get("condition") or "medium").lower(),
                         float(r["asking"]) if r.get("asking") else None, r.get("notes",""),
                         float(r["lat"]) if r.get("lat") else None,
                         float(r["lon"]) if r.get("lon") else None))
            n += 1
        except sqlite3.IntegrityError:
            # dedupe on address; backfill coords onto known leads when the re-fetch has them
            if r.get("lat") and r.get("lon"):
                con.execute("UPDATE leads SET lat=?, lon=? WHERE address=? AND lat IS NULL",
                            (float(r["lat"]), float(r["lon"]), r["address"].strip()))
    con.commit()
    print(f"ingested {n} new leads")

def load_comps(path):
    """CSV: city,address,sale_price,sqft,sale_date(YYYY-MM-DD),condition[,lat,lon]"""
    con = db(); n = 0
    for r in csv.DictReader(open(path)):
        con.execute("INSERT INTO comps(city,address,sale_price,sqft,sale_date,condition,lat,lon) VALUES(?,?,?,?,?,?,?,?)",
                    (r["city"].strip(), r["address"], float(r["sale_price"]),
                     int(r["sqft"]), r["sale_date"], (r.get("condition") or "renovated").lower(),
                     float(r["lat"]) if r.get("lat") else None,
                     float(r["lon"]) if r.get("lon") else None))
        n += 1
    con.commit(); print(f"loaded {n} comps")

def load_buyers(path):
    """CSV: name,contact,cities(semicolon),max_price,min_spread,strategy,proof_of_funds(0/1)"""
    con = db(); n = 0
    for r in csv.DictReader(open(path)):
        con.execute("INSERT INTO buyers(name,contact,cities,max_price,min_spread,strategy,proof_of_funds) VALUES(?,?,?,?,?,?,?)",
                    (r["name"], r["contact"], r["cities"], float(r["max_price"]),
                     float(r.get("min_spread") or 0), r.get("strategy","flip"),
                     int(r.get("proof_of_funds") or 0)))
        n += 1
    con.commit(); print(f"loaded {n} buyers")

# ---------------- underwriting ----------------
def haversine_mi(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 3958.8 * 2 * math.asin(math.sqrt(a))

def arv_for(con, lead):
    """Median $/sqft of renovated comps, last N days. Prefers comps within
    CFG['arv_radius_mi'] of the lead when both sides have coordinates; falls back
    to city-wide when <3 in radius. Returns (arv, comp_count, mode) — arv None
    when even city-wide has <3 usable comps (never value on thin data)."""
    cutoff = date.today().toordinal() - CFG["stale_comp_days"]
    rows = con.execute("SELECT sale_price, sqft, sale_date, lat, lon FROM comps WHERE city=? AND condition='renovated' AND sqft>0",
                       (lead["city"],)).fetchall()
    citywide, nearby = [], []
    for c in rows:
        try:
            y, m, d = map(int, c["sale_date"].split("-"))
            if date(y, m, d).toordinal() < cutoff:
                continue
            ppsf = c["sale_price"] / c["sqft"]
        except (ValueError, ZeroDivisionError):
            continue
        citywide.append(ppsf)
        if (lead["lat"] is not None and lead["lon"] is not None
                and c["lat"] is not None and c["lon"] is not None
                and haversine_mi(lead["lat"], lead["lon"], c["lat"], c["lon"]) <= CFG["arv_radius_mi"]):
            nearby.append(ppsf)
    if len(nearby) >= 3:
        ppsf, mode = nearby, f"radius<={CFG['arv_radius_mi']}mi"
    else:
        ppsf, mode = citywide, "citywide"
    if len(ppsf) < 3 or not lead["sqft"]:
        return None, len(ppsf), mode
    ppsf.sort()
    med = ppsf[len(ppsf)//2] if len(ppsf) % 2 else (ppsf[len(ppsf)//2 - 1] + ppsf[len(ppsf)//2]) / 2
    return round(med * lead["sqft"], -3), len(ppsf), mode

def underwrite():
    con = db()
    leads = con.execute("SELECT * FROM leads WHERE state='LEAD'").fetchall()
    for L in leads:
        score = sum(DISTRESS_WEIGHTS.get(s, 0) for s in (L["signals"] or "").split(";"))
        arv, ncomps, mode = arv_for(con, L)
        if arv is None:
            con.execute("UPDATE leads SET distress_score=? WHERE id=?", (score, L["id"]))
            log(con, L["id"], f"underwrite blocked: only {ncomps} usable comps ({mode}) in {L['city']} — load more")
            continue
        repairs = CFG["repair_psf"].get(L["condition"], 55) * (L["sqft"] or 0)
        mao = arv * CFG["mao_pct"] - repairs - CFG["assignment_fee_target"] - arv * CFG["closing_cost_pct"]
        mao = max(round(mao, -3), 0)
        spread = (mao - L["asking"]) if L["asking"] else None
        con.execute("""UPDATE leads SET state='UNDERWRITTEN', distress_score=?, arv=?,
                    repairs=?, mao=?, spread=? WHERE id=?""",
                    (score, arv, repairs, mao, spread, L["id"]))
        log(con, L["id"], f"underwritten: ARV {arv:,.0f} ({ncomps} comps, {mode}), repairs {repairs:,.0f}, MAO {mao:,.0f}")
    con.commit()
    ranked = con.execute("""SELECT id,address,city,distress_score,arv,mao,asking,spread FROM leads
                         WHERE state='UNDERWRITTEN' ORDER BY distress_score DESC, spread DESC""").fetchall()
    print(f"{'ID':>3} {'SCORE':>5} {'ARV':>9} {'MAO':>9} {'ASK':>9} {'SPREAD':>8}  ADDRESS")
    for r in ranked:
        print(f"{r['id']:>3} {r['distress_score']:>5} {r['arv']:>9,.0f} {r['mao']:>9,.0f} "
              f"{(r['asking'] or 0):>9,.0f} {(r['spread'] or 0):>8,.0f}  {r['address']}, {r['city']}")

# ---------------- negotiation prep ----------------
def offer(lead_id):
    con = db()
    L = con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not L or not L["mao"]:
        sys.exit("lead not found or not underwritten")
    ladder = [round(L["mao"] * f, -2) for f in CFG["offer_ladder"]]
    OUT.mkdir(exist_ok=True)
    loi = OUT / f"loi_{lead_id}_{re.sub(r'[^A-Za-z0-9]+','_',L['address'])}.txt"
    loi.write_text(textwrap.dedent(f"""\
        LETTER OF INTENT — {L['address']}, {L['city']}, MA
        Date: {date.today()}

        Buyer: [YOUR ENTITY LLC], and/or assigns
        Seller: {L['owner'] or '[OWNER]'}

        Purchase price: ${ladder[0]:,.0f}
        Deposit: $500 held in escrow with buyer's attorney
        Contingencies: 10-day inspection; marketable title
        Closing: 30 days or sooner, buyer pays customary buyer closing costs
        Sale: AS-IS, seller removes only what they wish to keep

        DISCLOSURE: Buyer intends to assign its equitable interest in the
        purchase contract to a third party prior to closing, and may earn
        an assignment fee. Seller acknowledges and consents.

        This LOI is non-binding until a Purchase & Sale agreement is executed.
        """))
    log(con, lead_id, f"offer ladder generated: {ladder}")
    con.execute("UPDATE leads SET state='OFFER_SENT' WHERE id=?", (lead_id,)); con.commit()
    print(f"OFFER LADDER for #{lead_id} {L['address']} (MAO ${L['mao']:,.0f})")
    print(f"  Anchor:    ${ladder[0]:,.0f}   <- open here")
    print(f"  Mid:       ${ladder[1]:,.0f}   <- concede in 1-2 steps")
    print(f"  Walk-away: ${ladder[2]:,.0f}   <- never exceed")
    print(f"\nSeller call script:")
    print(f'  "Based on {L["city"]} sales and roughly ${L["repairs"]:,.0f} of work the place')
    print(f'  needs, the number I can make work is ${ladder[0]:,.0f} — cash, as-is,')
    print(f'  close in 30 days, you don\'t fix or clean anything."')
    print(f"\nLOI written -> {loi}")

def counter(lead_id, their):
    con = db()
    L = con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    their = float(their); mao = L["mao"]
    log(con, lead_id, f"seller countered {their:,.0f}"); con.commit()
    if their <= mao * CFG["offer_ladder"][1]:
        print(f"ACCEPT. ${their:,.0f} is under mid-ladder (${mao*CFG['offer_ladder'][1]:,.0f}). Lock it today.")
    elif their <= mao:
        gap = mao - their
        meet = round(their + gap * 0.4, -2)
        print(f"COUNTER ${meet:,.0f} (splits gap, stays ${mao-meet:,.0f} under MAO).")
        print(f'Script: "I can stretch to ${meet:,.0f} if we can sign this week — that\'s me at my ceiling."')
    else:
        over = their - mao
        print(f"WALK unless terms change. ${their:,.0f} is ${over:,.0f} over MAO ${mao:,.0f}.")
        print(f"Only paths: seller finances ${over:,.0f}+, condition better than '{L['condition']}',")
        print(f"or cut assignment fee to floor (adds ${CFG['assignment_fee_target']-CFG['assignment_fee_floor']:,.0f} of room -> ${mao + CFG['assignment_fee_target']-CFG['assignment_fee_floor']:,.0f} absolute max).")

def contract(lead_id, price):
    con = db(); price = float(price)
    L = con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    con.execute("UPDATE leads SET state='UNDER_CONTRACT', contract_price=? WHERE id=?", (price, lead_id))
    log(con, lead_id, f"under contract at {price:,.0f}"); con.commit()
    fee_room = (L["mao"] or 0) + CFG["assignment_fee_target"] - price
    print(f"#{lead_id} UNDER CONTRACT at ${price:,.0f}. Assignment fee room: ${fee_room:,.0f}")
    print("P&S must include (have your attorney add):")
    print("  1. 'Buyer: [Entity], and/or assigns' — assignability clause")
    print("  2. Equitable-interest disclosure signed by seller")
    print("  3. Inspection contingency window (your escape hatch)")
    print("  4. Deposit held by YOUR attorney, not seller's")

# ---------------- disposition ----------------
def assign(lead_id):
    con = db()
    L = con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if L["state"] != "UNDER_CONTRACT":
        sys.exit("lead must be UNDER_CONTRACT first")
    ask = L["contract_price"] + CFG["assignment_fee_target"]
    matches = []
    for b in con.execute("SELECT * FROM buyers").fetchall():
        cities = [c.strip().lower() for c in b["cities"].split(";")]
        spread = (L["arv"] or 0) - ask - (L["repairs"] or 0)
        if L["city"].lower() in cities and b["max_price"] >= ask and spread >= b["min_spread"]:
            matches.append((b, spread))
    matches.sort(key=lambda x: (-x[0]["proof_of_funds"], -x[1]))
    print(f"Dispo price: ${ask:,.0f} (contract ${L['contract_price']:,.0f} + fee ${CFG['assignment_fee_target']:,.0f})")
    if not matches:
        print("No buyer-box matches. Options: cut fee toward floor, widen buyer list, or exercise inspection out.")
        return
    for b, s in matches:
        pof = "POF✓" if b["proof_of_funds"] else "no-POF"
        print(f"  {b['name']} ({b['contact']}) [{pof}, {b['strategy']}] — their spread ${s:,.0f}")
    top = matches[0][0]
    OUT.mkdir(exist_ok=True)
    pkg = OUT / f"assignment_{lead_id}.txt"
    pkg.write_text(textwrap.dedent(f"""\
        ASSIGNMENT OF PURCHASE & SALE AGREEMENT — DRAFT FOR ATTORNEY REVIEW
        Date: {date.today()}

        Assignor: [YOUR ENTITY LLC]
        Assignee: {top['name']}
        Property: {L['address']}, {L['city']}, {CFG['county']}
        Underlying P&S price: ${L['contract_price']:,.0f}
        Assignment fee: ${CFG['assignment_fee_target']:,.0f}, non-refundable upon signing,
          balance of fee paid at closing via settlement statement.
        Assignee deposit: $2,500 to assignor's attorney escrow within 48 hours.
        Assignee assumes all buyer obligations under the P&S, including closing date.
        Assignor makes no representations regarding property condition; assignee
          relies solely on its own inspection.

        MA NOTE: route through your closing attorney; fee appears on the
        settlement statement. Do not close without written seller disclosure
        acknowledgment on file.

        BLAST EMAIL (send to all matches):
        Subject: {L['city']} {L['beds']}/{L['baths']} off-market — ${ask:,.0f}, ARV ${L['arv']:,.0f}
        {L['sqft']} sqft, needs ~${L['repairs']:,.0f} ({L['condition']} rehab).
        Projected spread ${(L['arv'] or 0) - ask - (L['repairs'] or 0):,.0f}. Under contract,
        assignable, close in [X] days. $2,500 non-refundable to lock it. First POF wins.
        """))
    con.execute("UPDATE leads SET state='ASSIGNED', assigned_to=? WHERE id=?", (top["name"], lead_id))
    log(con, lead_id, f"assigned to {top['name']}"); con.commit()
    print(f"\nAssignment package -> {pkg}")

# ---------------- reporting ----------------
def status():
    con = db()
    for r in con.execute("SELECT state, COUNT(*) n, COALESCE(SUM(spread),0) s FROM leads GROUP BY state"):
        print(f"{r['state']:<15} {r['n']:>3}  spread ${r['s']:,.0f}")

def dashboard():
    con = db()
    leads = [dict(r) for r in con.execute("SELECT * FROM leads ORDER BY distress_score DESC")]
    events = [dict(r) for r in con.execute("SELECT * FROM events ORDER BY id DESC LIMIT 60")]
    OUT.mkdir(exist_ok=True)
    tpl = (Path(__file__).parent / "dashboard_template.html").read_text()
    html = tpl.replace("/*DATA*/", f"const LEADS={json.dumps(leads)};const EVENTS={json.dumps(events)};")
    (OUT / "dashboard.html").write_text(html)
    print(f"dashboard -> {OUT/'dashboard.html'}")

# ---------------- cli ----------------
if __name__ == "__main__":
    cmds = {"ingest": ingest, "comps": load_comps, "buyers": load_buyers,
            "underwrite": underwrite, "offer": offer, "counter": counter,
            "contract": contract, "assign": assign, "status": status, "dashboard": dashboard}
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        sys.exit(__doc__)
    cmds[sys.argv[1]](*sys.argv[2:])
