"""Multilingual keyword pre-scan over the OCR JSONL for damage signals.

Scans ocr/text/*.jsonl and emits ocr/keyword_flags.json with a list of
pages where any of the user's three concerns (hail, lightning, corrosion)
appear in either the original text or the English translation, in any of
the four corpus languages (en, de, pt, fr).

The output is later cross-referenced against the LLM extraction: any page
flagged here that did NOT produce a damage event in extracted/ becomes a
"near-miss" that merits re-extraction with the stronger model and explicit
mention in the buyer's report.

Usage:
    .venv/bin/python flag_pages.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEXT_DIR = ROOT / "ocr" / "text"
OUT_PATH = ROOT / "ocr" / "keyword_flags.json"

KEYWORDS: dict[str, list[str]] = {
    "hail": [
        # English
        r"\bhail\b", r"\bhailstorm\b", r"\bhailstones?\b", r"hail\s*damage",
        # German
        r"\bhagel\b", r"\bhagelschaden\b", r"\bhagelschlag\b",
        r"\bhagelschauer\b", r"\bhagelk(o|ö)rner\b",
        # Portuguese
        r"\bgranizo\b", r"\bpedras\s+de\s+gelo\b",
        # French
        r"\bgr(ê|e)le\b", r"\bgr(ê|e)lons?\b",
    ],
    "lightning": [
        # English
        r"\blightning\b", r"lightning\s*strike", r"struck\s+by\s+lightning",
        # German
        r"\bblitz\b(?!licht)", r"\bblitzschlag\b", r"\bblitzeinschlag\b",
        r"\bblitzschaden\b",
        # Portuguese
        r"\braio\b", r"queda\s+de\s+raio", r"atingido\s+por\s+raio",
        # French
        r"\bfoudre\b", r"frapp(é|e)\s+par\s+la\s+foudre",
        r"impact\s+de\s+foudre",
    ],
    "corrosion": [
        # English
        r"\bcorros(ion|ive|ed|ing)\b", r"\brust(ed|ing)?\b",
        r"\bpitting\b", r"intergranular", r"crevice\s+corrosion",
        # German
        r"\bkorrosion(en)?\b", r"\bkorrodiert\b", r"\brost(ig)?\b",
        r"\blochfra(ß|ss)\b",
        # Portuguese
        r"\bcorros(ã|a)o\b", r"\bcorro(í|i)d[oa]\b", r"\bferrugem\b",
        # French
        r"\bcorrosion\b", r"\brouille\b", r"\bpiq(û|u)res\b",
    ],
}


COMPILED: dict[str, list[re.Pattern[str]]] = {
    cat: [re.compile(p, re.IGNORECASE) for p in patterns]
    for cat, patterns in KEYWORDS.items()
}


def scan_text(text: str) -> dict[str, list[str]]:
    """Return {category: [matched_phrase, ...]} for all hits."""
    hits: dict[str, list[str]] = {}
    for cat, regexes in COMPILED.items():
        cat_hits: list[str] = []
        for rx in regexes:
            for m in rx.finditer(text):
                phrase = m.group(0)
                # Trim duplicates per page
                if phrase.lower() not in (h.lower() for h in cat_hits):
                    cat_hits.append(phrase)
        if cat_hits:
            hits[cat] = cat_hits
    return hits


def scan_all() -> list[dict]:
    out: list[dict] = []
    for jp in sorted(TEXT_DIR.glob("*.jsonl")):
        with jp.open(encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                # Concatenate original and English so we hit either.
                blob = (rec.get("text") or "") + "\n" + (rec.get("text_en") or "")
                hits = scan_text(blob)
                if hits:
                    out.append({
                        "pdf": rec["pdf"],
                        "page": rec["page"],
                        "lang": rec.get("lang", []),
                        "hits": hits,
                    })
    return out


def main() -> int:
    flags = scan_all()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(flags, ensure_ascii=False, indent=2))
    by_cat: dict[str, int] = {}
    for f in flags:
        for cat in f["hits"]:
            by_cat[cat] = by_cat.get(cat, 0) + 1
    print(f"flagged {len(flags)} page(s) total")
    for cat, n in sorted(by_cat.items()):
        print(f"  {cat:10s}: {n}")
    print(f"wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
