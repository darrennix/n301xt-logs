"""Build the static low-bandwidth logbook viewer.

The viewer normalizes the source PDFs into three entities:
  - Airframe
  - Engine DG0188
  - Engine DG0189

It emits one searchable PDF and one compact JSON record per page so the
browser only fetches the currently selected page.

Usage:
    .venv/bin/python src/build_viewer.py
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

import fitz


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
OCR_DIR = ROOT / "ocr"
SEARCHABLE_DIR = OCR_DIR / "searchable"
TEXT_DIR = OCR_DIR / "text"
DB_PATH = OCR_DIR / "events.db"
VIEWER_DIR = PROJECT_ROOT / "dist"
DATA_DIR = VIEWER_DIR / "data"
PAGE_DIR = VIEWER_DIR / "pages"
VIEWER_TEXT_DIR = VIEWER_DIR / "text"


ENTITIES = [
    {
        "id": "airframe",
        "label": "Airframe",
        "sources": [
            "D-CHIC - Jan 2013 to Feb 2014.pdf",
            "D-CHIC - Mar 2014 to Aug 2014.pdf",
            "D-CHIC - Aug 2014 to Feb 2015.pdf",
            "D-CHIC - Mar 2015 to Oct 2015.pdf",
            "D-CHIC - Nov 2015 to May 2016.pdf",
            "D-CHIC - June 2016 to Sept 2016.pdf",
            "D-CHIC - Oct 2016 to Mar 2017.pdf",
            "D-CHIC - May 2017.pdf",
            "D CHIC - L-AKTE - 1-11.pdf",
            "D-CHIC - October 2018-July 2019.pdf",
            "D-CHIC - July 2019 to Dec 2019.pdf",
            "D-CHIC - Oct 2021 to July 2022.pdf",
            # The source name appears malformed, but the contents include
            # DCHIC-2210 records, so keep it with the late-2022 material.
            "D-CHIC - Oct 2022 - Mar 2022.pdf",
            "D-CHIC - Oct 2022 to Mar 2023.pdf",
            "D-CHIC - Apr 2023 - Nov 2023.pdf",
            "D-CHIC - November 2023.pdf",
            "N301XT scans.pdf",
        ],
    },
    {
        "id": "engine-dg0188",
        "label": "Engine DG0188",
        "sources": [
            "Engine log DG0188.pdf",
            "Engine log DG0188 2.pdf",
        ],
    },
    {
        "id": "engine-dg0189",
        "label": "Engine DG0189",
        "sources": [
            "Engine Log - PCE-DG0189.pdf",
        ],
    },
]


def slugify(name: str) -> str:
    stem = name.removesuffix(".pdf")
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower())
    return slug.strip("-")


def read_jsonl_pages(path: Path) -> dict[int, dict]:
    pages: dict[int, dict] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            payload = json.loads(line)
            page = int(payload["page"])
            pages[page] = {
                "pdf": payload.get("pdf"),
                "page": page,
                "sourceLang": payload.get("lang", []),
                "tokenLang": payload.get("lang_token_level", []),
                "textOriginal": payload.get("text", ""),
                "textEnglish": payload.get("text_en", ""),
            }
    return pages


def load_events() -> dict[tuple[str, int], list[dict]]:
    events: dict[tuple[str, int], list[dict]] = defaultdict(list)
    if not DB_PATH.exists():
        return events

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                pdf, page, date, type, category, summary, details,
                aircraft_total_time_h, aircraft_cycles,
                engine_total_time_h, engine_cycles,
                shop, location, is_damage, damage_severity
            FROM events
            ORDER BY COALESCE(date, ''), id
            """
        )
        for row in rows:
            events[(row["pdf"], int(row["page"]))].append(
                {
                    "date": row["date"],
                    "type": row["type"],
                    "category": row["category"],
                    "summary": row["summary"],
                    "details": row["details"],
                    "aircraftTotalTimeH": row["aircraft_total_time_h"],
                    "aircraftCycles": row["aircraft_cycles"],
                    "engineTotalTimeH": row["engine_total_time_h"],
                    "engineCycles": row["engine_cycles"],
                    "shop": row["shop"],
                    "location": row["location"],
                    "isDamage": bool(row["is_damage"]),
                    "damageSeverity": row["damage_severity"],
                }
            )
    finally:
        conn.close()
    return events


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_js_assignment(path: Path, name: str, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    body = body.replace("</", "<\\/")
    path.write_text(f"window.{name} = {body};\n", encoding="utf-8")


def write_page_js(path: Path, key: str, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    key_json = json.dumps(key, ensure_ascii=True)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    body = body.replace("</", "<\\/")
    path.write_text(
        "window.__N301XT_PAGE_DATA__ = window.__N301XT_PAGE_DATA__ || {};\n"
        f"window.__N301XT_PAGE_DATA__[{key_json}] = {body};\n"
        "if (window.__N301XT_PAGE_READY__) "
        f"window.__N301XT_PAGE_READY__({key_json});\n",
        encoding="utf-8",
    )


def split_pdf(source_pdf: Path, source_id: str, force: bool) -> int:
    out_dir = PAGE_DIR / source_id
    out_dir.mkdir(parents=True, exist_ok=True)
    with fitz.open(source_pdf) as doc:
        page_count = doc.page_count
        for idx in range(page_count):
            page_num = idx + 1
            out_path = out_dir / f"{page_num:04d}.pdf"
            if out_path.exists() and not force:
                continue
            one_page = fitz.open()
            one_page.insert_pdf(doc, from_page=idx, to_page=idx)
            one_page.save(out_path, garbage=4, deflate=True)
            one_page.close()
    return page_count


def build_text_pages(
    source_pdf: str,
    source_id: str,
    source_label: str,
    entity_id: str,
    entity_label: str,
    page_count: int,
    events: dict[tuple[str, int], list[dict]],
    force: bool,
) -> int:
    text_path = TEXT_DIR / f"{source_pdf.removesuffix('.pdf')}.jsonl"
    if not text_path.exists():
        raise FileNotFoundError(f"Missing text JSONL: {text_path}")

    jsonl_pages = read_jsonl_pages(text_path)
    out_dir = VIEWER_TEXT_DIR / source_id
    out_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for page_num in range(1, page_count + 1):
        out_path = out_dir / f"{page_num:04d}.json"
        js_path = out_dir / f"{page_num:04d}.js"
        if out_path.exists() and js_path.exists() and not force:
            continue

        page_payload = jsonl_pages.get(page_num, {})
        payload = {
            "entityId": entity_id,
            "entityLabel": entity_label,
            "sourceId": source_id,
            "sourcePdf": source_pdf,
            "sourceLabel": source_label,
            "page": page_num,
            "sourceLang": page_payload.get("sourceLang", []),
            "tokenLang": page_payload.get("tokenLang", []),
            "textOriginal": page_payload.get("textOriginal", ""),
            "textEnglish": page_payload.get("textEnglish", ""),
            "events": events.get((source_pdf, page_num), []),
        }
        write_json(out_path, payload)
        write_page_js(js_path, f"{source_id}/{page_num:04d}", payload)
        written += 1

    if len(jsonl_pages) != page_count:
        print(
            f"warning: {source_pdf}: pdf pages={page_count}, "
            f"jsonl pages={len(jsonl_pages)}"
        )
    return written


def source_label(pdf_name: str) -> str:
    return pdf_name.removesuffix(".pdf")


def build(force: bool) -> dict:
    events = load_events()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PAGE_DIR.mkdir(parents=True, exist_ok=True)
    VIEWER_TEXT_DIR.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schemaVersion": 1,
        "generatedBy": "build_viewer.py",
        "entities": [],
        "pagePdfTemplate": "pages/{sourceId}/{pagePadded}.pdf",
        "textTemplate": "text/{sourceId}/{pagePadded}.json",
        "textScriptTemplate": "text/{sourceId}/{pagePadded}.js",
    }

    total_pages = 0
    for entity in ENTITIES:
        entity_page_count = 0
        source_docs = []
        for source_pdf in entity["sources"]:
            source_id = slugify(source_pdf)
            pdf_path = SEARCHABLE_DIR / source_pdf
            if not pdf_path.exists():
                raise FileNotFoundError(f"Missing searchable PDF: {pdf_path}")

            page_count = split_pdf(pdf_path, source_id, force)
            build_text_pages(
                source_pdf=source_pdf,
                source_id=source_id,
                source_label=source_label(source_pdf),
                entity_id=entity["id"],
                entity_label=entity["label"],
                page_count=page_count,
                events=events,
                force=force,
            )

            source_docs.append(
                {
                    "id": source_id,
                    "label": source_label(source_pdf),
                    "pdf": source_pdf,
                    "pageCount": page_count,
                    "entityPageStart": entity_page_count + 1,
                    "pagePdfTemplate": f"pages/{source_id}/{{pagePadded}}.pdf",
                    "textTemplate": f"text/{source_id}/{{pagePadded}}.json",
                    "textScriptTemplate": f"text/{source_id}/{{pagePadded}}.js",
                }
            )
            entity_page_count += page_count
            total_pages += page_count
            print(f"{entity['label']}: {source_pdf}: {page_count} pages")

        manifest["entities"].append(
            {
                "id": entity["id"],
                "label": entity["label"],
                "pageCount": entity_page_count,
                "sources": source_docs,
            }
        )

    manifest["pageCount"] = total_pages
    write_json(DATA_DIR / "manifest.json", manifest)
    write_js_assignment(DATA_DIR / "manifest.js", "__N301XT_MANIFEST__", manifest)
    print(f"viewer manifest: {DATA_DIR / 'manifest.json'}")
    print(f"viewer manifest script: {DATA_DIR / 'manifest.js'}")
    print(f"total pages: {total_pages}")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force",
        action="store_true",
        help="regenerate existing single-page PDFs and text JSON files",
    )
    args = parser.parse_args()
    build(force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
