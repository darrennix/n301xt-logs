"""Generate two focused 2-page reports: airframe.md and engines.md.

Each report is a tight executive summary aimed at a prospective buyer:
- Summary stats (hours, cycles, age, registration history)
- Damage history highlights
- Major maintenance milestones
- Recurring/notable issues
- Buyer-focused verdict

Source citations use the format (PDF stem p.N) — the comprehensive
buyers_report.md is the place to click through to the original page.

Usage:
    .venv/bin/python write_short_reports.py
    .venv/bin/python write_short_reports.py --model gpt-5.5
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
DB_PATH = ROOT / "ocr" / "events.db"
OUT_AIRFRAME = ROOT / "ocr" / "airframe_report.md"
OUT_ENGINES = ROOT / "ocr" / "engines_report.md"

if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith("OPENAI_API_KEY=") and "OPENAI_API_KEY" not in os.environ:
            os.environ["OPENAI_API_KEY"] = line.split("=", 1)[1]


AIRFRAME_CATEGORIES = (
    "airframe", "paint_exterior", "landing_gear", "interior",
    "hydraulics", "avionics", "fuel_system", "general", "other",
)
ENGINE_CATEGORIES = ("engine", "propeller")


def conn_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def shop_label(s: str | None) -> str:
    if not s:
        return ""
    return s.strip()


def fmt_evt(r: sqlite3.Row) -> str:
    pdf_stem = r["pdf"].rsplit(".pdf", 1)[0]
    parts = []
    parts.append(r["date"] or "----")
    parts.append(f"[{r['category']}]")
    if r["damage_severity"] == "major":
        parts.append("MAJOR")
    elif r["damage_severity"] == "minor":
        parts.append("minor")
    parts.append(r["summary"])
    if r["shop"]:
        parts.append(f"shop={shop_label(r['shop'])}")
    if r["location"]:
        parts.append(f"loc={r['location']}")
    if r["aircraft_total_time_h"] is not None:
        parts.append(f"TAT={r['aircraft_total_time_h']}")
    if r["aircraft_cycles"] is not None:
        parts.append(f"TAC={r['aircraft_cycles']}")
    if r["engine_total_time_h"] is not None:
        parts.append(f"ETT={r['engine_total_time_h']}")
    if r["engine_cycles"] is not None:
        parts.append(f"ECYC={r['engine_cycles']}")
    parts.append(f"src=({pdf_stem} p.{r['page']})")
    return " | ".join(parts)


def gather_airframe(conn: sqlite3.Connection) -> dict:
    cur = conn.cursor()
    cats_in = ",".join("?" * len(AIRFRAME_CATEGORIES))

    cur.execute(f"""
        SELECT * FROM events
        WHERE is_damage = 1
          AND category IN ({cats_in})
        ORDER BY COALESCE(date, '9999') ASC
    """, AIRFRAME_CATEGORIES)
    damage = cur.fetchall()
    major_damage = [r for r in damage if r["damage_severity"] == "major"]
    minor_damage = [r for r in damage if r["damage_severity"] == "minor"]

    # Always inject every hail / lightning / corrosion damage event
    # (any severity) so the user's stated concerns are never truncated.
    cur.execute("""
        SELECT * FROM events
        WHERE is_damage = 1
          AND (damage_keywords_json LIKE '%"hail"%'
               OR damage_keywords_json LIKE '%"lightning"%'
               OR damage_keywords_json LIKE '%"corrosion"%')
        ORDER BY COALESCE(date, '9999') ASC
    """)
    concern_events = cur.fetchall()

    # Major maintenance milestones — keyword-bound on these categories
    rows: dict[int, sqlite3.Row] = {}
    for term in ("overhaul", "repaint", " paint ", "interior refresh",
                 "gear overhaul", "landing gear overhaul",
                 "airworthiness review", " arc ",
                 "annual inspection", "phase inspection",
                 "hard landing", "prop strike", "lightning strike",
                 "hail", "corrosion", "lightning"):
        cur.execute(f"""
            SELECT * FROM events
            WHERE LOWER(summary || ' ' || details) LIKE ?
              AND category IN ({cats_in})
            ORDER BY COALESCE(date, '9999') ASC
        """, (f"%{term}%",) + AIRFRAME_CATEGORIES)
        for r in cur.fetchall():
            rows[r["id"]] = r
    milestones = sorted(rows.values(),
                        key=lambda r: r["date"] or "9999")

    # Shops + counts
    cur.execute(f"""
        SELECT TRIM(shop) AS s, COUNT(*) AS n
        FROM events
        WHERE shop IS NOT NULL AND TRIM(shop) != ''
          AND category IN ({cats_in})
        GROUP BY UPPER(TRIM(shop))
        ORDER BY n DESC LIMIT 12
    """, AIRFRAME_CATEGORIES)
    shops = cur.fetchall()

    # Times — filter implausible values (Phenom 300 entered service 2009;
    # serial numbers misread as TAT yield 8-digit values; >15k h is implausible
    # over <20 years of operation).
    cur.execute("""
        SELECT date, aircraft_total_time_h, aircraft_cycles, pdf, page
        FROM events
        WHERE date >= '2008-01-01' AND date <= '2030-01-01'
          AND aircraft_total_time_h BETWEEN 10 AND 15000
          AND (aircraft_cycles IS NULL OR aircraft_cycles BETWEEN 0 AND 12000)
        ORDER BY date DESC, aircraft_total_time_h DESC LIMIT 1
    """)
    latest_tt = cur.fetchone()

    cur.execute("""
        SELECT MIN(date), MAX(date) FROM events
        WHERE date >= '2008-01-01' AND date <= '2030-01-01'
    """)
    earliest_date, latest_date = cur.fetchone()

    # AD / SB count
    cur.execute(f"""
        SELECT ad_sb_refs_json FROM events
        WHERE category IN ({cats_in})
          AND ad_sb_refs_json != '[]'
    """, AIRFRAME_CATEGORIES)
    refs: Counter[str] = Counter()
    for r in cur.fetchall():
        try:
            for ref in json.loads(r["ad_sb_refs_json"]):
                refs[ref] += 1
        except Exception:
            pass

    # Make sure all concern (hail/lightning/corrosion) events are in the
    # digest even if the major_damage[:120] cap would have dropped them.
    by_id = {r["id"]: r for r in major_damage[:200]}
    for r in concern_events:
        by_id.setdefault(r["id"], r)
    digest_damage = sorted(by_id.values(), key=lambda r: r["date"] or "9999")

    return {
        "n_total_damage": len(damage),
        "n_major_damage": len(major_damage),
        "n_minor_damage": len(minor_damage),
        "major_damage": [fmt_evt(r) for r in digest_damage],
        "milestones": [fmt_evt(r) for r in milestones[:80]],
        "shops": [(r["s"], r["n"]) for r in shops],
        "latest_tt": dict(latest_tt) if latest_tt else None,
        "earliest_date": earliest_date,
        "latest_date": latest_date,
        "ad_sb_top": refs.most_common(15),
    }


def gather_engines(conn: sqlite3.Connection) -> dict:
    cur = conn.cursor()
    cats_in = ",".join("?" * len(ENGINE_CATEGORIES))

    cur.execute(f"""
        SELECT * FROM events
        WHERE category IN ({cats_in}) OR pdf LIKE 'Engine%'
        ORDER BY COALESCE(date, '9999') ASC
    """, ENGINE_CATEGORIES)
    all_engine = cur.fetchall()

    damage = [r for r in all_engine if r["is_damage"]]
    major_damage = [r for r in damage if r["damage_severity"] == "major"]
    minor_damage = [r for r in damage if r["damage_severity"] == "minor"]

    # Major maintenance milestones for engines
    keywords = (
        "overhaul", "hot section", " hsi ", " mpi ",
        "engine change", "engine removal", "engine installation",
        "engine replacement", "borescope", "trend",
        "fan blade", "compressor", "combustor", "turbine",
        "starter generator", "fuel control", "fcu", "fadec",
        "air inlet", "engine air inlet",
    )
    rows: dict[int, sqlite3.Row] = {}
    for term in keywords:
        cur.execute(f"""
            SELECT * FROM events
            WHERE LOWER(summary || ' ' || details) LIKE ?
              AND (category IN ({cats_in}) OR pdf LIKE 'Engine%')
            ORDER BY COALESCE(date, '9999') ASC
        """, (f"%{term}%",) + ENGINE_CATEGORIES)
        for r in cur.fetchall():
            rows[r["id"]] = r
    milestones = sorted(rows.values(), key=lambda r: r["date"] or "9999")

    # Engine times — filter implausible values (PW535E in this airframe will
    # be at most ~12k h after ~14 years; >15k is OCR misread).
    cur.execute("""
        SELECT * FROM events
        WHERE engine_total_time_h BETWEEN 10 AND 15000
          AND date >= '2008-01-01' AND date <= '2030-01-01'
        ORDER BY date DESC, engine_total_time_h DESC LIMIT 1
    """)
    latest_ett = cur.fetchone()

    cur.execute(f"""
        SELECT TRIM(shop) AS s, COUNT(*) AS n
        FROM events
        WHERE shop IS NOT NULL AND TRIM(shop) != ''
          AND (category IN ({cats_in}) OR pdf LIKE 'Engine%')
        GROUP BY UPPER(TRIM(shop))
        ORDER BY n DESC LIMIT 12
    """, ENGINE_CATEGORIES)
    shops = cur.fetchall()

    return {
        "n_total_engine_events": len(all_engine),
        "n_total_damage": len(damage),
        "n_major_damage": len(major_damage),
        "n_minor_damage": len(minor_damage),
        "major_damage": [fmt_evt(r) for r in major_damage[:80]],
        "milestones": [fmt_evt(r) for r in milestones[:60]],
        "shops": [(r["s"], r["n"]) for r in shops],
        "latest_ett": dict(latest_ett) if latest_ett else None,
    }


SYS_PROMPT = """\
You are an aviation maintenance analyst writing a focused executive summary
for a prospective buyer of a used Embraer Phenom 300 (EMB-505, S/N 50500096,
PW535E engines). Registration history: D-CHIC (Germany) → N301XT (US, 2025).

Write tight, factual, decision-grade prose. Plain markdown.

HARD CONSTRAINTS:
- Maximum 2 printed pages (~900 words). The reader will print this.
- Cite source pages inline using the format (PDF stem p.N) exactly as given
  in the raw event lines. Do not invent dates, hours, shops, or events.
  If a fact isn't in the digest, don't claim it.
- Use markdown sections with ## headers; keep each section focused.
- Lead with the headline issues a buyer must know. Damage findings before
  routine maintenance.
- Distinguish CONFIRMED damage events from minor/routine items.
- No emojis. No filler. No hedging adverbs ("perhaps", "possibly may have").
- Last section: a short "Buyer's questions to escalate" bullet list with
  open items worth a pre-buy mechanic's attention."""


def call_llm(model: str, system: str, user: str) -> str:
    from openai import OpenAI
    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content or ""


def airframe_user_prompt(d: dict) -> str:
    lt = d["latest_tt"] or {}
    return f"""\
Write the AIRFRAME report.

CORE STATS:
- Date range covered by extracted events: {d['earliest_date']} to {d['latest_date']}
- Latest known airframe time/cycles: TAT={lt.get('aircraft_total_time_h')}h \
TAC={lt.get('aircraft_cycles')}  (recorded {lt.get('date')}, \
src=({(lt.get('pdf') or '').rsplit('.pdf',1)[0]} p.{lt.get('page')}))
- Damage events on airframe-side categories: {d['n_total_damage']} total \
({d['n_major_damage']} major, {d['n_minor_damage']} minor)

TOP SHOPS (events on airframe-side categories):
{chr(10).join(f'  - {s} ({n})' for s, n in d['shops'])}

TOP AD/SB REFERENCES:
{chr(10).join(f'  - {ref}: {n}' for ref, n in d['ad_sb_top'])}

MAJOR DAMAGE EVENTS (chronological, airframe categories only) — \
each line is "date | category | severity | summary | shop | loc | TAT | TAC | src":
{chr(10).join(d['major_damage'])}

MAJOR MAINTENANCE MILESTONES & DAMAGE-RELATED EVENTS:
{chr(10).join(d['milestones'])}

REPORT STRUCTURE:
1. ## Aircraft (1 short paragraph): identity, registration history, dates covered, latest TAT/TAC.
2. ## Damage history: focus on confirmed major events. Group by incident, not by line item. The Zadar 2014 hailstorm and the 2018 AMS lightning strike are the most consequential — name them, dates, locations, shops that did repair, scope of damage, follow-up disposition.
3. ## Maintenance & inspection program: what shops did the recurring maintenance, ARC issuances, paint/interior work if any.
4. ## Buyer's questions to escalate: 4–6 specific bullets a pre-buy inspector should chase.

Stay under ~900 words total."""


def engines_user_prompt(d: dict) -> str:
    le = d["latest_ett"] or {}
    return f"""\
Write the ENGINES report. The aircraft has TWO PW535E engines. Try to
distinguish entries between them when the source identifies a specific
engine (e.g. ESN, "Engine 1" vs "Engine 2", LH/RH, the engine logbook
filenames "Engine log DG0188", "Engine log DG0188 2", "Engine Log -
PCE-DG0189"). DG0188 / DG0188 2 / PCE-DG0189 are engine serial numbers.

CORE STATS:
- Total engine-related events: {d['n_total_engine_events']}
- Engine damage events: {d['n_total_damage']} total \
({d['n_major_damage']} major, {d['n_minor_damage']} minor)
- Latest known engine time: ETT={le.get('engine_total_time_h')}h \
ECYC={le.get('engine_cycles')}  (recorded {le.get('date')}, \
src=({(le.get('pdf') or '').rsplit('.pdf',1)[0]} p.{le.get('page')}))

TOP SHOPS (engine work):
{chr(10).join(f'  - {s} ({n})' for s, n in d['shops'])}

MAJOR ENGINE DAMAGE EVENTS:
{chr(10).join(d['major_damage'])}

MAJOR ENGINE MAINTENANCE MILESTONES (overhauls, HSI, removals/installations,
borescope, fuel/FCU/FADEC, hot section, fan/compressor/turbine, etc.):
{chr(10).join(d['milestones'])}

REPORT STRUCTURE:
1. ## Engines (1 short paragraph): two PW535E engines, ESN(s) if you can
   identify them from the digest, latest known ETT/ECYC, basis of records.
2. ## Engine damage & notable findings: hail-induced engine air inlet
   damage (LH inlet from Zadar 2014), corrosion findings on bypass duct /
   exhaust nozzle (2019), any cracking, leaks, hot section condition. Be
   specific.
3. ## Removals, installations, overhauls, life-limited parts: what the
   logbook says about life-cycle posture.
4. ## Buyer's questions to escalate: 4–6 bullets focused on engine
   condition assessment, trend monitoring, on-condition status, ESN-level
   times, recent borescope, oil analysis, AD compliance.

Stay under ~900 words total."""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gpt-5.5")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"missing {DB_PATH}", file=sys.stderr)
        return 1

    conn = conn_db()

    print("gathering airframe digest...")
    af = gather_airframe(conn)
    print("calling LLM for airframe report...")
    af_md = call_llm(args.model, SYS_PROMPT, airframe_user_prompt(af))
    OUT_AIRFRAME.write_text(af_md)
    print(f"wrote {OUT_AIRFRAME}  ({len(af_md):,} chars, ~{len(af_md.split())} words)")

    print()
    print("gathering engines digest...")
    en = gather_engines(conn)
    print("calling LLM for engines report...")
    en_md = call_llm(args.model, SYS_PROMPT, engines_user_prompt(en))
    OUT_ENGINES.write_text(en_md)
    print(f"wrote {OUT_ENGINES}  ({len(en_md):,} chars, ~{len(en_md.split())} words)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
