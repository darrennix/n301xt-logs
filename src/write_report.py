"""Generate the Markdown buyer's intelligence report from the events DB.

Reads ocr/events.db and ocr/keyword_flags.json, emits ocr/buyers_report.md.

Most sections are deterministic SQL + Markdown templating. The narrative
"executive summary" and the per-concern (hail / lightning / corrosion)
narratives are produced by an LLM call, given the aggregated facts.

Each event line ends with a [source: PDF p.N] link to the searchable PDF
so a buyer's mechanic can verify any claim.

Usage:
    .venv/bin/python write_report.py
    .venv/bin/python write_report.py --no-llm    # skeleton only
    .venv/bin/python write_report.py --model gpt-5.5
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
DB_PATH = ROOT / "ocr" / "events.db"
FLAGS_PATH = ROOT / "ocr" / "keyword_flags.json"
SEARCHABLE_DIR = ROOT / "ocr" / "searchable"
TEXT_DIR = ROOT / "ocr" / "text"
OUT_PATH = ROOT / "ocr" / "buyers_report.md"

if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith("OPENAI_API_KEY=") and "OPENAI_API_KEY" not in os.environ:
            os.environ["OPENAI_API_KEY"] = line.split("=", 1)[1]


# ---------- helpers ----------

def src_link(pdf: str, page: int) -> str:
    """Return a markdown link to the searchable PDF page."""
    pdf_stem = pdf.rsplit(".pdf", 1)[0]
    target = SEARCHABLE_DIR / f"{pdf_stem}.pdf"
    return f"[{pdf_stem} p.{page}](file://{quote(str(target))}#page={page})"


def fmt_date(d: str | None) -> str:
    return d if d else "—"


def fmt_hours(h: float | None) -> str:
    return f"{h:,.1f}" if h is not None else "—"


def fmt_cycles(c: int | None) -> str:
    return f"{c:,}" if c is not None else "—"


def conn_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------- section: summary card ----------

def section_summary(conn: sqlite3.Connection) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM events")
    n_events = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) FROM events WHERE is_damage = 1")
    n_damage = cur.fetchone()[0]

    plausible = "date >= '2008-01-01' AND date <= '2030-01-01'"
    cur.execute(f"""
        SELECT date, aircraft_total_time_h, aircraft_cycles
        FROM events
        WHERE {plausible} AND aircraft_total_time_h IS NOT NULL
        ORDER BY date ASC LIMIT 1
    """)
    earliest = cur.fetchone()
    cur.execute(f"""
        SELECT date, aircraft_total_time_h, aircraft_cycles
        FROM events
        WHERE {plausible} AND aircraft_total_time_h IS NOT NULL
        ORDER BY aircraft_total_time_h DESC LIMIT 1
    """)
    latest = cur.fetchone()

    cur.execute(f"SELECT MIN(date), MAX(date) FROM events WHERE {plausible}")
    earliest_date, latest_date = cur.fetchone()

    return {
        "events": n_events,
        "damage_events": n_damage,
        "earliest_date": earliest_date,
        "latest_date": latest_date,
        "earliest_tt": dict(earliest) if earliest else None,
        "latest_tt": dict(latest) if latest else None,
    }


# ---------- section: damage history ----------

def section_damage(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM events
        WHERE is_damage = 1
        ORDER BY COALESCE(date, '9999-99-99') ASC, pdf, page
    """)
    return cur.fetchall()


# ---------- section: focus concerns ----------

CONCERN_KEYWORDS = {
    "hail":       ["hail"],
    "lightning":  ["lightning"],
    "corrosion":  ["corrosion"],
}


def damage_events_by_keyword(conn: sqlite3.Connection,
                             concern: str) -> list[sqlite3.Row]:
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM events
        WHERE damage_keywords_json LIKE ?
        ORDER BY COALESCE(date, '9999-99-99') ASC, pdf, page
    """, (f'%"{concern}"%',))
    return cur.fetchall()


def near_misses(conn: sqlite3.Connection,
                concern: str, flags: list[dict]) -> list[dict]:
    """Pages flagged by regex for a concern but with no damage event extracted."""
    extracted_damage_pages = set()
    cur = conn.cursor()
    cur.execute("""
        SELECT pdf, page FROM events
        WHERE is_damage = 1 AND damage_keywords_json LIKE ?
    """, (f'%"{concern}"%',))
    for row in cur.fetchall():
        extracted_damage_pages.add((row["pdf"], row["page"]))

    out = []
    for f in flags:
        if concern not in f["hits"]:
            continue
        if (f["pdf"], f["page"]) in extracted_damage_pages:
            continue
        out.append(f)
    return out


# ---------- section: shops ----------

def section_shops(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.cursor()
    cur.execute("""
        SELECT TRIM(shop) AS shop_norm,
               COUNT(*) AS n,
               MIN(date) AS first_seen,
               MAX(date) AS last_seen,
               GROUP_CONCAT(DISTINCT location) AS locations
        FROM events
        WHERE shop IS NOT NULL AND TRIM(shop) != ''
        GROUP BY UPPER(TRIM(shop))
        ORDER BY n DESC
    """)
    return [dict(r) for r in cur.fetchall()]


# ---------- section: recurring components ----------

def section_recurring(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.cursor()
    cur.execute("""
        SELECT name, COUNT(*) AS n
        FROM components
        GROUP BY UPPER(TRIM(name))
        HAVING n >= 3
        ORDER BY n DESC, name
        LIMIT 80
    """)
    return [dict(r) for r in cur.fetchall()]


# ---------- section: ADs / SBs ----------

def section_ad_sb(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.cursor()
    cur.execute("""
        SELECT id, pdf, page, date, summary, ad_sb_refs_json
        FROM events
        WHERE ad_sb_refs_json != '[]'
        ORDER BY date
    """)
    out = []
    for r in cur.fetchall():
        try:
            refs = json.loads(r["ad_sb_refs_json"])
        except Exception:
            refs = []
        if not refs:
            continue
        out.append({**dict(r), "refs": refs})
    return out


# ---------- section: utilization timeline ----------

def section_timeline(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """One row per year-month, taking the latest TAT/TAC seen that month.

    Filters out implausible dates (before 2008 — aircraft type didn't exist)
    that come from OCR misreads or extracted reference dates."""
    cur = conn.cursor()
    cur.execute("""
        WITH ranked AS (
          SELECT substr(date, 1, 7) AS ym,
                 date, aircraft_total_time_h, aircraft_cycles,
                 engine_total_time_h, engine_cycles, pdf, page, summary,
                 ROW_NUMBER() OVER (
                     PARTITION BY substr(date, 1, 7)
                     ORDER BY aircraft_total_time_h DESC NULLS LAST,
                              date DESC
                 ) AS rn
          FROM events
          WHERE date IS NOT NULL
            AND date >= '2008-01-01'
            AND date <= '2030-01-01'
            AND (aircraft_total_time_h IS NOT NULL
                 OR aircraft_cycles IS NOT NULL)
        )
        SELECT ym, date, aircraft_total_time_h, aircraft_cycles,
               engine_total_time_h, engine_cycles, pdf, page, summary
        FROM ranked WHERE rn = 1 ORDER BY ym
    """)
    return cur.fetchall()


# ---------- section: integrity ----------

def section_integrity(conn: sqlite3.Connection) -> dict:
    cur = conn.cursor()
    cur.execute("""
        SELECT pdf, page, review_reason FROM page_meta
        WHERE needs_review = 1
        ORDER BY pdf, page
    """)
    review = [dict(r) for r in cur.fetchall()]
    cur.execute("""
        SELECT page_quality, COUNT(*) AS n FROM page_meta
        GROUP BY page_quality
    """)
    quality = {r["page_quality"]: r["n"] for r in cur.fetchall()}
    return {"review": review, "quality": quality}


# ---------- LLM narrative ----------

def llm_narrative(model: str, prompt: str, system: str) -> str:
    from openai import OpenAI
    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    )
    return resp.choices[0].message.content or ""


def narrative_for_concern(model: str, concern: str,
                          events: list[sqlite3.Row],
                          near: list[dict]) -> str:
    if not events and not near:
        return f"No {concern} events or keyword matches found in the corpus."
    bullet_evt = []
    for e in events[:40]:
        bullet_evt.append(
            f"- date={fmt_date(e['date'])} pdf={e['pdf']!r} page={e['page']} "
            f"sev={e['damage_severity']!r} summary={e['summary']!r} "
            f"shop={e['shop']!r} excerpt={e['raw_excerpt'][:200]!r}"
        )
    bullet_near = [
        f"- pdf={f['pdf']!r} page={f['page']} hits={f['hits'].get(concern)}"
        for f in near[:40]
    ]
    user = f"""\
Concern: {concern.upper()}

EXTRACTED EVENTS WITH THIS DAMAGE KEYWORD ({len(events)}):
{chr(10).join(bullet_evt) or '(none)'}

REGEX-FLAGGED PAGES WITH NO MATCHING DAMAGE EVENT ({len(near)}):
{chr(10).join(bullet_near) or '(none)'}

Write a tight 2–4 paragraph narrative for a prospective buyer:
- Summarize what we found about {concern} on this aircraft.
- Be specific about dates, hours, locations, shops if known.
- Distinguish CONFIRMED damage events from POSSIBLE/AMBIGUOUS keyword matches.
- Highlight the most consequential 2–3 items first; the rest can be summarized.
- Cite source pages inline using the format (Apr 2023 p.42).
- Be measured, factual, no exaggeration. The reader is a serious buyer."""
    sys_prompt = (
        "You are an aviation maintenance analyst summarizing logbook findings "
        "for a prospective aircraft buyer. Be concrete and source-grounded. "
        "Avoid hedging, but distinguish 'confirmed in records' from 'possible "
        "based on keyword in unrelated context'."
    )
    return llm_narrative(model, user, sys_prompt)


def narrative_executive(model: str, summary: dict, n_damage_total: int,
                        shops_top: list[dict]) -> str:
    s = (f"events_total={summary['events']} damage_events_total={n_damage_total} "
         f"date_range={summary['earliest_date']}..{summary['latest_date']}")
    shops = ", ".join(f"{s['shop_norm']} ({s['n']})" for s in shops_top[:8])
    user = f"""\
Aggregated facts:
{s}
top_shops: {shops}

Write a 3–5 sentence executive summary for a prospective buyer of this
Embraer Phenom 300 (D-CHIC -> N301XT, PW535E engines). State total time and
cycles if known, registration history, the period covered by these
records, the dominant shops, and a single-sentence verdict on whether the
records appear comprehensive. No bullets. No hedging. Plain prose."""
    return llm_narrative(model, user, "You are a concise aviation analyst.")


# ---------- markdown writers ----------

def render_event_line(e: sqlite3.Row | dict) -> str:
    e = dict(e)
    parts = []
    parts.append(f"**{fmt_date(e.get('date'))}**")
    if e.get("category"): parts.append(f"_{e['category']}_")
    parts.append(f"— {e.get('summary', '')}")
    if e.get("shop"):
        parts.append(f" · shop: **{e['shop']}**")
    if e.get("location"):
        parts.append(f" · loc: {e['location']}")
    tt = []
    if e.get("aircraft_total_time_h") is not None:
        tt.append(f"TAT {fmt_hours(e['aircraft_total_time_h'])}")
    if e.get("aircraft_cycles") is not None:
        tt.append(f"TAC {fmt_cycles(e['aircraft_cycles'])}")
    if tt:
        parts.append(f" · {' / '.join(tt)}")
    if e.get("damage_severity"):
        parts.append(f" · sev: **{e['damage_severity']}**")
    parts.append(f" · {src_link(e['pdf'], e['page'])}")
    return "".join(parts)


def render_report(conn: sqlite3.Connection, flags: list[dict],
                  use_llm: bool, model: str) -> str:
    out: list[str] = []
    summary = section_summary(conn)
    damage = section_damage(conn)
    shops = section_shops(conn)
    recurring = section_recurring(conn)
    ad_sb = section_ad_sb(conn)
    timeline = section_timeline(conn)
    integrity = section_integrity(conn)

    out.append("# Pre-purchase intelligence report")
    out.append("")
    out.append("**Aircraft:** Embraer Phenom 300 (EMB-505), engines PW535E. "
               "Registration: D-CHIC (Germany) → N301XT (US, 2025).")
    out.append("")
    out.append(
        f"_Generated from {summary['events']} extracted events across "
        f"~9,500 logbook pages. Source link follows every claim — verify "
        f"by clicking through to the linked page._"
    )
    out.append("")

    # 1. Executive summary
    out.append("## Executive summary")
    out.append("")
    if use_llm:
        out.append(narrative_executive(model, summary, len(damage), shops))
    else:
        out.append(f"- Events extracted: **{summary['events']}**")
        out.append(f"- Damage-flagged events: **{len(damage)}**")
        out.append(f"- Date range covered: **{summary['earliest_date']} "
                   f"– {summary['latest_date']}**")
    out.append("")

    # 2. Damage history (major only; minor + unknown summarized)
    out.append("## 🚨 Damage history")
    out.append("")
    major = [e for e in damage if e["damage_severity"] == "major"]
    minor = [e for e in damage if e["damage_severity"] == "minor"]
    unknown = [e for e in damage if e["damage_severity"] not in ("major", "minor")]
    out.append(
        f"_{len(damage)} total damage event(s): "
        f"**{len(major)} major**, {len(minor)} minor, {len(unknown)} unknown. "
        f"Listing major events below; minor/unknown rolled up after._"
    )
    out.append("")
    if major:
        out.append("### Major damage events (chronological)")
        out.append("")
        for e in major:
            out.append(f"- {render_event_line(e)}")
            if e["details"]:
                out.append(f"  - {e['details']}")
            kw = json.loads(e["damage_keywords_json"] or "[]")
            if kw:
                out.append(f"  - keywords: `{', '.join(kw)}`")
        out.append("")
    if minor or unknown:
        out.append("### Minor & unknown-severity damage (rolled up by month)")
        out.append("")
        cur = conn.cursor()
        cur.execute("""
            SELECT substr(date, 1, 7) AS ym, COUNT(*) AS n,
                   GROUP_CONCAT(DISTINCT category) AS cats,
                   GROUP_CONCAT(DISTINCT shop) AS shops
            FROM events
            WHERE is_damage = 1
              AND (damage_severity != 'major' OR damage_severity IS NULL)
              AND date IS NOT NULL
            GROUP BY ym
            ORDER BY ym
        """)
        for r in cur.fetchall():
            out.append(f"- **{r['ym']}** — {r['n']} event(s) · "
                       f"categories: {r['cats']} · shops: {r['shops']}")
        out.append("")

    # 3. Focus findings
    out.append("## 🔍 Focus findings")
    out.append("")
    out.append("Per the user's stated concerns. Each subsection lists "
               "extracted damage events with the keyword, plus near-miss "
               "pages where the keyword appears in OCR text but no damage "
               "event was extracted (worth a manual check).")
    out.append("")
    for concern in ("hail", "lightning", "corrosion"):
        ev = damage_events_by_keyword(conn, concern)
        nm = near_misses(conn, concern, flags)
        out.append(f"### {concern.title()}")
        out.append("")
        if use_llm:
            out.append(narrative_for_concern(model, concern, ev, nm))
        else:
            out.append(f"_{len(ev)} extracted damage event(s); "
                       f"{len(nm)} near-miss page(s)._")
        out.append("")
        if ev:
            out.append("**Extracted damage events:**")
            for e in ev:
                out.append(f"- {render_event_line(e)}")
                if e["details"]:
                    out.append(f"  - {e['details']}")
            out.append("")
        if nm:
            out.append("**Near-miss pages (regex hit, no damage event):**")
            by_pdf: dict[str, list[dict]] = defaultdict(list)
            for f in nm:
                by_pdf[f["pdf"]].append(f)
            for pdf, items in sorted(by_pdf.items()):
                pages = ", ".join(
                    f"[p.{f['page']}](file://{quote(str(SEARCHABLE_DIR / (pdf.rsplit('.pdf',1)[0] + '.pdf')))}#page={f['page']})"
                    for f in sorted(items, key=lambda x: x["page"])
                )
                out.append(f"- _{pdf.rsplit('.pdf',1)[0]}_: {pages}")
            out.append("")

    # 4. Major maintenance milestones — keyword-scoped
    out.append("## 🛠 Major maintenance milestones")
    out.append("")
    cur = conn.cursor()
    rows: list = []
    seen: set[int] = set()
    for term in ("overhaul", " hsi ", " mpi ", "hot section",
                 "major inspection", "paint", "repaint",
                 "engine change", "engine removal", "engine installation",
                 "gear overhaul", "landing gear overhaul",
                 "interior refresh", "airworthiness review",
                 "arc issuance"):
        cur.execute("""
            SELECT * FROM events
            WHERE LOWER(summary || ' ' || details) LIKE ?
            ORDER BY COALESCE(date, '9999-99-99') ASC
        """, (f'%{term}%',))
        for r in cur.fetchall():
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            rows.append(r)
    rows = sorted(rows, key=lambda r: r["date"] or '9999-99-99')[:150]
    for e in rows:
        out.append(f"- {render_event_line(e)}")
    out.append("")

    # 5. Shops & locations
    out.append("## 🏪 Shop & location history")
    out.append("")
    out.append("| Shop | Events | First | Last | Locations |")
    out.append("|------|-------:|-------|------|-----------|")
    for s in shops[:60]:
        loc = s.get("locations") or "—"
        out.append(f"| {s['shop_norm']} | {s['n']} | "
                   f"{fmt_date(s['first_seen'])} | "
                   f"{fmt_date(s['last_seen'])} | {loc} |")
    out.append("")

    # 6. Recurring components
    out.append("## 🔁 Recurring component changes")
    out.append("")
    if not recurring:
        out.append("_No components appear changed twice or more._")
    else:
        out.append("| Component | Times changed |")
        out.append("|-----------|--------------:|")
        for r in recurring:
            out.append(f"| {r['name']} | {r['n']} |")
    out.append("")

    # 7. AD / SB references
    out.append("## 📋 AD / SB references")
    out.append("")
    if not ad_sb:
        out.append("_No AD/SB references extracted._")
    else:
        ref_count: Counter[str] = Counter()
        for e in ad_sb:
            for r in e["refs"]:
                ref_count[r] += 1
        out.append(f"_{len(ref_count)} unique reference(s) across {len(ad_sb)} event(s)._")
        out.append("")
        out.append("| Reference | Mentions |")
        out.append("|-----------|---------:|")
        for ref, n in ref_count.most_common(80):
            out.append(f"| `{ref}` | {n} |")
    out.append("")

    # 8. Utilization timeline — one row per month
    out.append("## 📈 Utilization timeline (one row per month)")
    out.append("")
    out.append("| Month | TAT (h) | TAC | Engine TT (h) | Engine CYC | Source |")
    out.append("|-------|--------:|----:|--------------:|-----------:|--------|")
    for r in timeline:
        out.append(f"| {r['ym']} | {fmt_hours(r['aircraft_total_time_h'])} | "
                   f"{fmt_cycles(r['aircraft_cycles'])} | "
                   f"{fmt_hours(r['engine_total_time_h'])} | "
                   f"{fmt_cycles(r['engine_cycles'])} | "
                   f"{src_link(r['pdf'], r['page'])} |")
    out.append("")

    # 9. Integrity
    out.append("## 🧾 Logbook integrity")
    out.append("")
    out.append("Page-quality distribution from extraction:")
    out.append("")
    for q, n in sorted(integrity["quality"].items()):
        out.append(f"- `{q}`: {n}")
    out.append("")
    if integrity["review"]:
        out.append(f"**Pages flagged `needs_review` ({len(integrity['review'])}):**")
        out.append("")
        for r in integrity["review"][:80]:
            reason = r.get("review_reason") or "—"
            out.append(f"- {src_link(r['pdf'], r['page'])} — {reason}")
        out.append("")

    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gpt-5.5")
    ap.add_argument("--no-llm", action="store_true",
                    help="Skip LLM narratives (fast skeleton)")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"missing {DB_PATH}; run build_events_db.py first", file=sys.stderr)
        return 1
    flags = json.loads(FLAGS_PATH.read_text()) if FLAGS_PATH.exists() else []

    conn = conn_db()
    md = render_report(conn, flags, use_llm=not args.no_llm, model=args.model)
    OUT_PATH.write_text(md)
    print(f"wrote {OUT_PATH}  ({len(md):,} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
