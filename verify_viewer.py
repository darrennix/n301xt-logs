"""Verify generated static viewer assets against source OCR outputs.

Checks every generated text/PDF asset exists and that every compact page JSON
matches the source JSONL. It also spot-checks one page out of every ten by
rendering the generated single-page PDF and the corresponding source PDF page
and comparing their image hashes.

Usage:
    .venv/bin/python verify_viewer.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import fitz


ROOT = Path(__file__).resolve().parent
OCR_DIR = ROOT / "ocr"
SEARCHABLE_DIR = OCR_DIR / "searchable"
TEXT_DIR = OCR_DIR / "text"
VIEWER_DIR = ROOT / "viewer"
MANIFEST_PATH = VIEWER_DIR / "data" / "manifest.json"


class VerifyError(Exception):
    pass


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl_pages(pdf_name: str) -> dict[int, dict]:
    path = TEXT_DIR / f"{pdf_name.removesuffix('.pdf')}.jsonl"
    if not path.exists():
        raise VerifyError(f"missing source JSONL: {path}")

    pages: dict[int, dict] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            payload = json.loads(line)
            page = int(payload["page"])
            pages[page] = payload
    return pages


def resolve_template(source: dict, key: str, page: int) -> Path:
    rel = source[key].replace("{pagePadded}", f"{page:04d}")
    return VIEWER_DIR / rel


def render_hash(doc: fitz.Document, page_index: int) -> str:
    page = doc.load_page(page_index)
    # Low-resolution grayscale is enough for order verification and keeps the
    # 1-in-10 audit fast across 950+ sampled pages.
    pix = page.get_pixmap(
        matrix=fitz.Matrix(0.18, 0.18),
        colorspace=fitz.csGRAY,
        alpha=False,
    )
    h = hashlib.sha256()
    h.update(str(pix.width).encode("ascii"))
    h.update(b"x")
    h.update(str(pix.height).encode("ascii"))
    h.update(b":")
    h.update(pix.samples)
    return h.hexdigest()


def compare_page_visual(source_doc: fitz.Document, source: dict, page: int) -> None:
    generated_pdf = resolve_template(source, "pagePdfTemplate", page)
    if not generated_pdf.exists():
        raise VerifyError(f"missing generated page PDF: {generated_pdf}")

    with fitz.open(generated_pdf) as generated_doc:
        if generated_doc.page_count != 1:
            raise VerifyError(
                f"generated page PDF is not single-page: {generated_pdf}"
            )
        source_hash = render_hash(source_doc, page - 1)
        generated_hash = render_hash(generated_doc, 0)
        if source_hash != generated_hash:
            raise VerifyError(
                "visual order check failed: "
                f"{source['pdf']} p.{page} != {generated_pdf}"
            )


def verify_page_json(
    entity: dict,
    source: dict,
    page: int,
    source_payload: dict,
) -> None:
    generated_json = resolve_template(source, "textTemplate", page)
    if not generated_json.exists():
        raise VerifyError(f"missing generated text JSON: {generated_json}")

    payload = load_json(generated_json)
    expected_pairs = {
        "entityId": entity["id"],
        "entityLabel": entity["label"],
        "sourceId": source["id"],
        "sourcePdf": source["pdf"],
        "sourceLabel": source["label"],
        "page": page,
        "textOriginal": source_payload.get("text", ""),
        "textEnglish": source_payload.get("text_en", ""),
    }
    for key, expected in expected_pairs.items():
        actual = payload.get(key)
        if actual != expected:
            raise VerifyError(
                f"text JSON mismatch for {source['pdf']} p.{page}: "
                f"{key} expected {expected!r}, got {actual!r}"
            )

    if not isinstance(payload.get("events"), list):
        raise VerifyError(f"events is not a list: {generated_json}")


def verify_source(entity: dict, source: dict) -> tuple[int, int]:
    source_pdf = SEARCHABLE_DIR / source["pdf"]
    if not source_pdf.exists():
        raise VerifyError(f"missing source PDF: {source_pdf}")

    source_pages = load_jsonl_pages(source["pdf"])
    checked_pages = 0
    sampled_pages = 0

    with fitz.open(source_pdf) as source_doc:
        if source_doc.page_count != source["pageCount"]:
            raise VerifyError(
                f"manifest page count mismatch for {source['pdf']}: "
                f"manifest={source['pageCount']} source={source_doc.page_count}"
            )
        if len(source_pages) != source["pageCount"]:
            raise VerifyError(
                f"source JSONL page count mismatch for {source['pdf']}: "
                f"manifest={source['pageCount']} jsonl={len(source_pages)}"
            )

        for page in range(1, source["pageCount"] + 1):
            source_payload = source_pages.get(page)
            if not source_payload:
                raise VerifyError(f"missing source JSONL page: {source['pdf']} p.{page}")
            pdf_path = resolve_template(source, "pagePdfTemplate", page)
            if not pdf_path.exists():
                raise VerifyError(f"missing generated page PDF: {pdf_path}")
            verify_page_json(entity, source, page, source_payload)
            checked_pages += 1

        for page in range(1, source["pageCount"] + 1, 10):
            compare_page_visual(source_doc, source, page)
            sampled_pages += 1

    return checked_pages, sampled_pages


def verify() -> None:
    if not MANIFEST_PATH.exists():
        raise VerifyError(f"missing manifest: {MANIFEST_PATH}")

    manifest = load_json(MANIFEST_PATH)
    entity_ids = [entity["id"] for entity in manifest["entities"]]
    if entity_ids != ["airframe", "engine-dg0188", "engine-dg0189"]:
        raise VerifyError(f"unexpected entity order/ids: {entity_ids}")
    entity_labels = [entity["label"] for entity in manifest["entities"]]
    if entity_labels != ["Airframe", "Engine DG0188", "Engine DG0189"]:
        raise VerifyError(f"unexpected entity labels: {entity_labels}")

    total_pages = 0
    total_samples = 0
    for entity in manifest["entities"]:
        entity_pages = 0
        for source in entity["sources"]:
            pages, samples = verify_source(entity, source)
            entity_pages += pages
            total_samples += samples
            print(
                f"{entity['label']}: {source['label']}: "
                f"pages={pages} spot_checks={samples}"
            )
        if entity_pages != entity["pageCount"]:
            raise VerifyError(
                f"entity page count mismatch for {entity['label']}: "
                f"manifest={entity['pageCount']} checked={entity_pages}"
            )
        total_pages += entity_pages

    if total_pages != manifest["pageCount"]:
        raise VerifyError(
            f"manifest total mismatch: manifest={manifest['pageCount']} "
            f"checked={total_pages}"
        )

    print()
    print(f"verified pages: {total_pages}")
    print(f"visual order spot checks: {total_samples}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    try:
        verify()
    except VerifyError as exc:
        print(f"VERIFY FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
