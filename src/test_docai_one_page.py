"""Submit a single page from the smaller logbook PDF to Document AI and
print a summary of the result.

Usage:
    .venv/bin/python test_docai_one_page.py [--page N]
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
from google.cloud import documentai


def render_page_png(pdf_path: Path, page_idx: int, dpi: int) -> bytes:
    with fitz.open(pdf_path) as doc:
        page = doc.load_page(page_idx)
        pix = page.get_pixmap(dpi=dpi)
        return pix.tobytes("png")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", type=int, default=1, help="1-based page number")
    args = ap.parse_args()

    if not PDF_PATH.exists():
        print(f"missing pdf: {PDF_PATH}", file=sys.stderr)
        return 1

    print(f"rendering page {args.page} of {PDF_PATH.name} at {DPI} dpi...")
    png = render_page_png(PDF_PATH, args.page - 1, DPI)
    print(f"  {len(png):,} bytes PNG")

    opts = ClientOptions(api_endpoint="eu-documentai.googleapis.com")
    client = documentai.DocumentProcessorServiceClient(client_options=opts)
    raw_doc = documentai.RawDocument(content=png, mime_type="image/png")
    request = documentai.ProcessRequest(name=PROCESSOR_NAME, raw_document=raw_doc)

    print("calling Document AI...")
    result = client.process_document(request=request)
    doc = result.document

    text = doc.text or ""
    n_pages = len(doc.pages)
    n_tokens = sum(len(p.tokens) for p in doc.pages)
    n_lines = sum(len(p.lines) for p in doc.pages)
    n_blocks = sum(len(p.blocks) for p in doc.pages)
    print()
    print(f"pages:  {n_pages}")
    print(f"blocks: {n_blocks}")
    print(f"lines:  {n_lines}")
    print(f"tokens: {n_tokens}")
    print(f"chars:  {len(text):,}")
    print()
    print("--- first 800 chars of detected text ---")
    print(text[:800])
    print("--- end ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
