"""Per-page LLM extraction of structured logbook events.

Reads ocr/text/<stem>.jsonl, sends each page to OpenAI with a strict JSON
schema, and writes ocr/extracted/<stem>/NNNN.events.json.

Resumable: pages whose .events.json already exists are skipped. Errors are
surfaced loudly per-page; one bad page does not halt the run.

Usage:
    .venv/bin/python extract_events.py                      # all pdfs
    .venv/bin/python extract_events.py --pdf "<name>.pdf"
    .venv/bin/python extract_events.py --pages 1-10
    .venv/bin/python extract_events.py --model gpt-5.5      # for re-extraction
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
TEXT_DIR = ROOT / "ocr" / "text"
OUT_DIR = ROOT / "ocr" / "extracted"
DEFAULT_MODEL = "gpt-5.4-mini"

# Load OPENAI_API_KEY from .env without an extra dependency.
if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line.startswith("OPENAI_API_KEY=") and "OPENAI_API_KEY" not in os.environ:
            os.environ["OPENAI_API_KEY"] = line.split("=", 1)[1]

from openai import OpenAI  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from tqdm import tqdm  # noqa: E402


# ---------- structured-output schema ----------

EventType = Literal[
    "damage", "scheduled_maintenance", "unscheduled_maintenance",
    "inspection", "component_change", "ad_sb_compliance",
    "modification", "registration", "delivery", "other",
]
Category = Literal[
    "engine", "airframe", "avionics", "interior",
    "landing_gear", "propeller", "fuel_system",
    "hydraulics", "paint_exterior", "general", "other",
]
Severity = Literal["minor", "major", "unknown"]
PageQuality = Literal["clean", "noisy", "blank", "regulatory_boilerplate"]


class Component(BaseModel):
    name: str
    pn_off: str | None = None
    sn_off: str | None = None
    pn_on: str | None = None
    sn_on: str | None = None


class Event(BaseModel):
    date: str | None = Field(None, description="ISO YYYY-MM-DD if present")
    type: EventType
    category: Category
    summary: str = Field(description="<= 120 chars one-line English summary")
    details: str = Field(description="fuller English description")
    aircraft_total_time_h: float | None = None
    aircraft_cycles: int | None = None
    engine_total_time_h: float | None = None
    engine_cycles: int | None = None
    shop: str | None = None
    location: str | None = Field(None, description="City, country, or ICAO")
    components: list[Component] = []
    ad_sb_refs: list[str] = []
    is_damage: bool = False
    damage_severity: Severity | None = None
    damage_keywords: list[str] = []
    signoff_name: str | None = None
    signoff_license: str | None = None
    raw_excerpt: str = Field(
        description="<= 240 chars verbatim quote justifying this event"
    )


class PageExtraction(BaseModel):
    events: list[Event]
    page_quality: PageQuality
    needs_review: bool
    review_reason: str | None = None


# ---------- prompt ----------

SYSTEM_PROMPT = """\
You are extracting structured maintenance events from one page of an airplane
maintenance/airworthiness logbook. The aircraft is an Embraer Phenom 300 (model
EMB-505, engines Pratt & Whitney PW535E). It was originally registered in
Germany as D-CHIC and was imported to the US as N301XT in 2025.

The corpus contains maintenance work orders, task cards, AD/SB compliance
records, component change records, inspection signoffs, and regulatory
paperwork. There are NO pilot flight log entries — do not invent flight events.

The page text below has already been OCR'd. You are given both the original
text (mixed German / English / Portuguese / French) and a machine English
translation. Use whichever helps you understand. Do not translate proper nouns
(shop names, mechanic names, place names, registrations, P/N, S/N, ICAO
codes) — keep them verbatim.

For each distinct event on the page, emit one Event object. If the page is
blank, a TOC, or pure regulatory boilerplate with no maintenance content,
return an empty events list and set page_quality accordingly. Do not
fabricate. If a field is not stated, leave it null/empty.

You MUST flag damage events with is_damage=true. Be especially attentive to:
- HAIL DAMAGE: hail, hailstorm; Hagel, Hagelschaden; granizo; grêle. Watch
  for composite/paint repair, leading-edge work, dimpling, exterior
  re-skinning, paint re-touch on upper surfaces.
- LIGHTNING STRIKE: lightning, lightning strike; Blitzschlag, Blitzschaden;
  raio (queda de raio); foudre. Watch for antenna/radome work, NDT after
  strike, static wick replacement, wingtip/elevator/rudder strike points,
  bonding tests.
- CORROSION: corrosion, rust, pitting; Korrosion, Rost; corrosão, ferrugem;
  corrosion. Watch for blistering paint, structural NDT findings, fastener
  crevice corrosion, intergranular corrosion, repeated corrosion findings
  in the same area.

Set damage_keywords to a list of which of {hail, lightning, corrosion, smoke,
fire, leak, crack, dent, scratch, bird_strike, hard_landing, prop_strike,
tire_blowout, ground_handling, other_damage} apply. Use damage_severity to
flag minor vs major (major = structural repair, replacement of major
component, NDT-required action, anything that would matter to a buyer).

raw_excerpt MUST be a verbatim chunk (≤240 chars) from the original-language
text that justifies the event — this lets a human verify against the source.
"""


def build_user_message(text_orig: str, text_en: str, lang: list[str],
                       pdf: str, page: int) -> str:
    parts = [
        f"PDF: {pdf}",
        f"Page: {page}",
        f"Detected source language(s): {', '.join(lang) if lang else 'unknown'}",
        "",
        "--- ORIGINAL TEXT ---",
        text_orig.strip() or "(empty)",
        "",
        "--- ENGLISH TRANSLATION ---",
        text_en.strip() or "(empty)",
    ]
    return "\n".join(parts)


# ---------- IO helpers ----------

def read_jsonl(path: Path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def page_out_path(stem: str, page: int) -> Path:
    return OUT_DIR / stem / f"{page:04d}.events.json"


def parse_pages(spec: str | None) -> set[int] | None:
    if not spec:
        return None
    out: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(chunk))
    return out


# ---------- extraction call ----------

def extract_one(client: OpenAI, model: str, rec: dict) -> PageExtraction:
    user_msg = build_user_message(
        rec.get("text", ""), rec.get("text_en", ""),
        rec.get("lang", []), rec.get("pdf", ""), rec.get("page", 0),
    )
    resp = client.chat.completions.parse(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        response_format=PageExtraction,
    )
    return resp.choices[0].message.parsed


def process_page(client: OpenAI, model: str, rec: dict, out_path: Path,
                 force: bool) -> str:
    if out_path.exists() and not force:
        return "skip"
    parsed = extract_one(client, model, rec)
    payload = {
        "pdf": rec.get("pdf"),
        "page": rec.get("page"),
        "lang": rec.get("lang", []),
        "model": model,
        "extraction": parsed.model_dump(),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return "ok"


# ---------- driver ----------

def run(pdf_filter: str | None, pages_filter: set[int] | None,
        model: str, workers: int, force: bool, limit: int | None) -> int:
    client = OpenAI()
    jsonls = sorted(TEXT_DIR.glob("*.jsonl"))
    if pdf_filter:
        wanted_stem = Path(pdf_filter).stem
        jsonls = [p for p in jsonls if p.stem == wanted_stem]
        if not jsonls:
            print(f"no jsonl found for {pdf_filter!r}", file=sys.stderr)
            return 1
    print(f"extracting from {len(jsonls)} PDF(s) using {model}")

    rc_failures = 0
    for jp in jsonls:
        stem = jp.stem
        records = list(read_jsonl(jp))
        if pages_filter is not None:
            records = [r for r in records if r["page"] in pages_filter]
        if limit:
            records = records[:limit]

        todo = []
        for r in records:
            out = page_out_path(stem, r["page"])
            if out.exists() and not force:
                continue
            todo.append((r, out))
        print(f"\n=== {stem} ===  total={len(records)} todo={len(todo)}")
        if not todo:
            continue

        failures = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            jobs = {
                pool.submit(process_page, client, model, r, out, force):
                    r["page"]
                for r, out in todo
            }
            for fut in tqdm(as_completed(jobs), total=len(jobs), desc="extract"):
                try:
                    fut.result()
                except Exception as e:
                    n = jobs[fut]
                    failures += 1
                    tqdm.write(f"  page {n} FAILED: {type(e).__name__}: {e}")
        if failures:
            print(f"  WARNING: {failures} page(s) failed in {stem}")
            rc_failures += failures
    return 0 if rc_failures == 0 else 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default=None,
                    help="restrict to one PDF filename (matches jsonl stem)")
    ap.add_argument("--pages", default=None, help='e.g. "1-10" or "5,7,9"')
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--force", action="store_true",
                    help="re-extract pages even if output exists")
    ap.add_argument("--limit", type=int, default=None,
                    help="max pages per PDF (debug)")
    args = ap.parse_args()
    return run(args.pdf, parse_pages(args.pages), args.model,
               args.workers, args.force, args.limit)


if __name__ == "__main__":
    sys.exit(main())
