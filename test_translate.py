"""End-to-end OCR + translate test on one logbook page.

Pulls a page from rotated-pdf, runs Document AI OCR, then sends the resulting
text through Google Cloud Translation v3 with autodetect source language.

Usage:
    .venv/bin/python test_translate.py [--page N]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KEY_PATH = ROOT / "secrets" / "gcp-service-account.json"
PROJECT_ID = "gen-lang-client-0551697674"
PROCESSOR_NAME = (
    "projects/722351201105/locations/eu/processors/75cf48cdcd867839"
)
PDF_PATH = ROOT / "rotated-pdf" / "D CHIC - L-AKTE - 1-11.pdf"
DPI = 300

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(KEY_PATH)
os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID

import fitz
from google.api_core.client_options import ClientOptions
from google.cloud import documentai, translate_v3 as translate


def render_page_png(pdf_path: Path, page_idx: int, dpi: int) -> bytes:
    with fitz.open(pdf_path) as doc:
        return doc.load_page(page_idx).get_pixmap(dpi=dpi).tobytes("png")


def ocr_png(png: bytes) -> str:
    opts = ClientOptions(api_endpoint="eu-documentai.googleapis.com")
    client = documentai.DocumentProcessorServiceClient(client_options=opts)
    raw_doc = documentai.RawDocument(content=png, mime_type="image/png")
    request = documentai.ProcessRequest(name=PROCESSOR_NAME, raw_document=raw_doc)
    return client.process_document(request=request).document.text or ""


def translate_to_en(text: str) -> tuple[str, str]:
    """Returns (english_text, detected_source_language)."""
    client = translate.TranslationServiceClient()
    parent = f"projects/{PROJECT_ID}/locations/global"
    resp = client.translate_text(
        request={
            "parent": parent,
            "contents": [text],
            "mime_type": "text/plain",
            "target_language_code": "en",
        }
    )
    t = resp.translations[0]
    return t.translated_text, t.detected_language_code


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", type=int, default=130)
    args = ap.parse_args()

    if not PDF_PATH.exists():
        print(f"missing pdf: {PDF_PATH}", file=sys.stderr)
        return 1

    print(f"page {args.page} of {PDF_PATH.name}")
    png = render_page_png(PDF_PATH, args.page - 1, DPI)
    print(f"  rendered {len(png):,} bytes")

    print("OCR...")
    text = ocr_png(png)
    print(f"  {len(text):,} chars detected")
    print()
    print("--- ORIGINAL (first 600 chars) ---")
    print(text[:600])
    print()

    print("translating...")
    en, lang = translate_to_en(text)
    print(f"  detected source language: {lang}")
    print(f"  {len(en):,} chars translated")
    print()
    print("--- ENGLISH (first 600 chars) ---")
    print(en[:600])
    return 0


if __name__ == "__main__":
    sys.exit(main())
