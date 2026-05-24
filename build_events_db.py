"""Aggregate per-page event extractions into a single SQLite database.

Reads ocr/extracted/<stem>/NNNN.events.json files and writes ocr/events.db
with three tables (events, components, page_meta) plus an FTS5 virtual
table for full-text search over summary/details/raw_excerpt.

Re-running rebuilds the DB from scratch (cheap; ~9,500 rows).

Usage:
    .venv/bin/python build_events_db.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXTRACT_DIR = ROOT / "ocr" / "extracted"
DB_PATH = ROOT / "ocr" / "events.db"


SCHEMA = """
DROP TABLE IF EXISTS events;
DROP TABLE IF EXISTS components;
DROP TABLE IF EXISTS page_meta;
DROP TABLE IF EXISTS events_fts;

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pdf TEXT NOT NULL,
    page INTEGER NOT NULL,
    date TEXT,                     -- ISO YYYY-MM-DD
    type TEXT NOT NULL,
    category TEXT NOT NULL,
    summary TEXT NOT NULL,
    details TEXT NOT NULL,
    aircraft_total_time_h REAL,
    aircraft_cycles INTEGER,
    engine_total_time_h REAL,
    engine_cycles INTEGER,
    shop TEXT,
    location TEXT,
    ad_sb_refs_json TEXT,          -- JSON array
    is_damage INTEGER NOT NULL,
    damage_severity TEXT,
    damage_keywords_json TEXT,     -- JSON array
    signoff_name TEXT,
    signoff_license TEXT,
    raw_excerpt TEXT NOT NULL,
    extraction_model TEXT NOT NULL
);
CREATE INDEX idx_events_pdf_page ON events(pdf, page);
CREATE INDEX idx_events_date ON events(date);
CREATE INDEX idx_events_type ON events(type);
CREATE INDEX idx_events_is_damage ON events(is_damage);
CREATE INDEX idx_events_shop ON events(shop);

CREATE TABLE components (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    pn_off TEXT,
    sn_off TEXT,
    pn_on TEXT,
    sn_on TEXT,
    FOREIGN KEY (event_id) REFERENCES events(id)
);
CREATE INDEX idx_components_event ON components(event_id);
CREATE INDEX idx_components_name ON components(name);

CREATE TABLE page_meta (
    pdf TEXT NOT NULL,
    page INTEGER NOT NULL,
    page_quality TEXT NOT NULL,
    needs_review INTEGER NOT NULL,
    review_reason TEXT,
    extraction_model TEXT NOT NULL,
    PRIMARY KEY (pdf, page)
);

CREATE VIRTUAL TABLE events_fts USING fts5(
    summary, details, raw_excerpt,
    content='events', content_rowid='id'
);
CREATE TRIGGER events_ai AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, summary, details, raw_excerpt)
    VALUES (new.id, new.summary, new.details, new.raw_excerpt);
END;
"""


def insert_page(cur: sqlite3.Cursor, payload: dict) -> int:
    pdf = payload["pdf"]
    page = payload["page"]
    model = payload.get("model", "unknown")
    extraction = payload["extraction"]

    cur.execute(
        "INSERT INTO page_meta(pdf,page,page_quality,needs_review,"
        "review_reason,extraction_model) VALUES (?,?,?,?,?,?)",
        (pdf, page, extraction["page_quality"],
         1 if extraction["needs_review"] else 0,
         extraction.get("review_reason"), model),
    )

    n_inserted = 0
    for ev in extraction.get("events", []):
        cur.execute(
            """INSERT INTO events (
                pdf, page, date, type, category, summary, details,
                aircraft_total_time_h, aircraft_cycles,
                engine_total_time_h, engine_cycles,
                shop, location, ad_sb_refs_json,
                is_damage, damage_severity, damage_keywords_json,
                signoff_name, signoff_license, raw_excerpt,
                extraction_model
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pdf, page, ev.get("date"),
                ev["type"], ev["category"],
                ev["summary"], ev["details"],
                ev.get("aircraft_total_time_h"),
                ev.get("aircraft_cycles"),
                ev.get("engine_total_time_h"),
                ev.get("engine_cycles"),
                ev.get("shop"), ev.get("location"),
                json.dumps(ev.get("ad_sb_refs", [])),
                1 if ev.get("is_damage") else 0,
                ev.get("damage_severity"),
                json.dumps(ev.get("damage_keywords", [])),
                ev.get("signoff_name"), ev.get("signoff_license"),
                ev["raw_excerpt"], model,
            ),
        )
        event_id = cur.lastrowid
        for c in ev.get("components", []):
            cur.execute(
                """INSERT INTO components (
                    event_id, name, pn_off, sn_off, pn_on, sn_on
                ) VALUES (?,?,?,?,?,?)""",
                (event_id, c["name"], c.get("pn_off"), c.get("sn_off"),
                 c.get("pn_on"), c.get("sn_on")),
            )
        n_inserted += 1
    return n_inserted


def main() -> int:
    if DB_PATH.exists():
        DB_PATH.unlink()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    cur = conn.cursor()

    pdfs = sorted([d for d in EXTRACT_DIR.iterdir() if d.is_dir()])
    total_events = 0
    total_pages = 0
    for d in pdfs:
        files = sorted(d.glob("*.events.json"))
        ev_count = 0
        for f in files:
            try:
                payload = json.loads(f.read_text())
            except Exception as e:
                print(f"  skip {f}: {e}", file=sys.stderr)
                continue
            ev_count += insert_page(cur, payload)
        total_events += ev_count
        total_pages += len(files)
        print(f"  {d.name:55s} pages={len(files):5d} events={ev_count:5d}")

    conn.commit()
    conn.close()
    print()
    print(f"DB ready at {DB_PATH}")
    print(f"  pages indexed: {total_pages}")
    print(f"  events stored: {total_events}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
