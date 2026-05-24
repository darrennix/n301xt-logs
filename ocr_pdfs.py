"""End-to-end OCR + translation pipeline for D-CHIC logbook PDFs.

Pipeline steps (each resumable via skip-if-exists):
  1. Render each page of the source PDF to PNG (300 dpi).
  2. Send each PNG to Google Document AI -> .ocr.json.
  3. Send the OCR text to Google Cloud Translation v3 -> .en.json.
  4. Stamp invisible text from the OCR onto the original PDF -> searchable PDF.
  5. Emit a per-pdf JSONL: one record per page (text + text_en + tokens).

Outputs go under ocr/ alongside rotated-pdf/.

Usage:
    .venv/bin/python ocr_pdfs.py --pdf "D CHIC - L-AKTE - 1-11.pdf" --pages 1-10
    .venv/bin/python ocr_pdfs.py --pdf <name>            # all pages
    .venv/bin/python ocr_pdfs.py --pdf <name> --steps render,ocr,translate,jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KEY_PATH = ROOT / "secrets" / "gcp-service-account.json"
PROJECT_ID = "gen-lang-client-0551697674"
PROCESSOR_NAME = (
    "projects/722351201105/locations/eu/processors/75cf48cdcd867839"
)
SRC_DIR = ROOT / "rotated-pdf"
OUT_DIR = ROOT / "ocr"
DPI = 300
DOCAI_ENGINE = "google-docai-ocr@2026-05"
TRANSLATE_ENGINE = "google-translate-v3@2026-05"

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(KEY_PATH)
os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID

import io

import fitz  # PyMuPDF
from PIL import Image
from google.api_core.client_options import ClientOptions
from google.cloud import documentai, translate_v3 as translate
from tqdm import tqdm

DOCAI_SYNC_LIMIT_BYTES = 18 * 1024 * 1024  # leave headroom under 20 MB cap


# ---------- helpers ----------

def parse_pages(spec: str | None, total: int) -> list[int]:
    if not spec:
        return list(range(1, total + 1))
    out: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(chunk))
    return [p for p in out if 1 <= p <= total]


def page_paths(stem: str, page: int) -> dict[str, Path]:
    base = OUT_DIR / "pages" / stem
    return {
        "png": base / f"{page:04d}.png",
        "ocr": base / f"{page:04d}.ocr.json",
        "en":  base / f"{page:04d}.en.json",
    }


# ---------- step 1: render ----------

def render_page(pdf_path: Path, page_idx: int, png_path: Path) -> None:
    if png_path.exists():
        return
    png_path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open(pdf_path) as doc:
        doc.load_page(page_idx).get_pixmap(dpi=DPI).save(png_path)


# ---------- step 2: OCR ----------

def make_docai_client() -> documentai.DocumentProcessorServiceClient:
    opts = ClientOptions(api_endpoint="eu-documentai.googleapis.com")
    return documentai.DocumentProcessorServiceClient(client_options=opts)


def _slice(text: str, layout) -> str:
    segs = layout.text_anchor.text_segments
    if not segs:
        return ""
    return "".join(text[int(s.start_index):int(s.end_index)] for s in segs)


def _bbox(layout) -> list[float]:
    verts = layout.bounding_poly.normalized_vertices
    if not verts:
        return []
    xs = [v.x for v in verts]
    ys = [v.y for v in verts]
    return [min(xs), min(ys), max(xs), max(ys)]


def _langs(elem) -> list[str]:
    return [d.language_code for d in elem.detected_languages]


def build_ocr_json(doc, png_path: Path) -> dict:
    page = doc.pages[0]
    text = doc.text or ""

    def stripped(layout):
        return _slice(text, layout).strip()

    blocks = [
        {"text": stripped(b.layout), "bbox": _bbox(b.layout),
         "conf": float(b.layout.confidence), "lang": _langs(b)}
        for b in page.blocks
    ]
    lines = [
        {"text": stripped(l.layout), "bbox": _bbox(l.layout),
         "conf": float(l.layout.confidence), "lang": _langs(l)}
        for l in page.lines
    ]
    tokens = [
        {"text": stripped(t.layout), "bbox": _bbox(t.layout),
         "conf": float(t.layout.confidence), "lang": _langs(t)}
        for t in page.tokens
    ]
    return {
        "page_image": png_path.name,
        "engine": DOCAI_ENGINE,
        "image_size": {
            "w_px": int(page.dimension.width),
            "h_px": int(page.dimension.height),
            "dpi": DPI,
        },
        "lang_detected": sorted({l for tok in tokens for l in tok["lang"]}),
        "text": text,
        "blocks": blocks,
        "lines": lines,
        "tokens": tokens,
    }


def shrink_for_docai(png_bytes: bytes) -> tuple[bytes, str]:
    """If PNG exceeds the sync API limit, return JPEG-compressed (and possibly
    downscaled) bytes. Returns (data, mime_type)."""
    if len(png_bytes) <= DOCAI_SYNC_LIMIT_BYTES:
        return png_bytes, "image/png"
    img = Image.open(io.BytesIO(png_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    for max_side in (5000, 4000, 3200, 2600, 2000):
        scaled = img
        if max(img.size) > max_side:
            r = max_side / max(img.size)
            scaled = img.resize(
                (int(img.size[0] * r), int(img.size[1] * r)),
                Image.LANCZOS,
            )
        buf = io.BytesIO()
        scaled.save(buf, format="JPEG", quality=85, optimize=True)
        data = buf.getvalue()
        if len(data) <= DOCAI_SYNC_LIMIT_BYTES:
            return data, "image/jpeg"
    # Last resort: very small + lower quality
    img.thumbnail((1800, 1800), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70, optimize=True)
    return buf.getvalue(), "image/jpeg"


def ocr_page(client, png_path: Path, ocr_path: Path) -> None:
    if ocr_path.exists():
        return
    data, mime = shrink_for_docai(png_path.read_bytes())
    raw = documentai.RawDocument(content=data, mime_type=mime)
    req = documentai.ProcessRequest(name=PROCESSOR_NAME, raw_document=raw)
    resp = client.process_document(request=req)
    payload = build_ocr_json(resp.document, png_path)
    ocr_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


# ---------- step 3: translate ----------

def make_translate_client() -> translate.TranslationServiceClient:
    return translate.TranslationServiceClient()


def translate_page(client, ocr_path: Path, en_path: Path) -> None:
    if en_path.exists():
        return
    ocr = json.loads(ocr_path.read_text())
    text = ocr.get("text", "")
    if not text.strip():
        en_path.write_text(json.dumps({
            "engine": TRANSLATE_ENGINE,
            "source_lang": ocr.get("lang_detected", []),
            "text_original": text, "text_en": "",
        }, ensure_ascii=False, indent=2))
        return
    parent = f"projects/{PROJECT_ID}/locations/global"
    resp = client.translate_text(request={
        "parent": parent, "contents": [text],
        "mime_type": "text/plain", "target_language_code": "en",
    })
    t = resp.translations[0]
    en_path.write_text(json.dumps({
        "engine": TRANSLATE_ENGINE,
        "source_lang": [t.detected_language_code],
        "text_original": text,
        "text_en": t.translated_text,
    }, ensure_ascii=False, indent=2))


# ---------- step 4: searchable PDF ----------

def write_searchable_pdf(pdf_path: Path, stem: str, pages: list[int],
                         out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    src = fitz.open(pdf_path)
    try:
        for n in pages:
            page = src.load_page(n - 1)
            ocr_path = page_paths(stem, n)["ocr"]
            if not ocr_path.exists():
                continue
            ocr = json.loads(ocr_path.read_text())
            pw, ph = page.rect.width, page.rect.height
            for tok in ocr["tokens"]:
                t = tok["text"]
                bbox = tok["bbox"]
                if not t or not bbox:
                    continue
                x0, y0, x1, y1 = bbox  # normalized, top-left origin
                th = (y1 - y0) * ph
                if th <= 0:
                    continue
                # PyMuPDF uses top-left origin in page.rect when /Rotate is
                # applied, so y maps directly. Place baseline near the bottom
                # of the token bbox.
                x = x0 * pw
                y = y1 * ph
                fontsize = max(1.0, th * 0.85)
                try:
                    page.insert_text((x, y), t, fontsize=fontsize,
                                     fontname="helv", render_mode=3)
                except Exception:
                    pass
        src.save(out_path)
    finally:
        src.close()


# ---------- step 5: JSONL ----------

def emit_jsonl(stem: str, pdf_name: str, pages: list[int],
               out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for n in pages:
            paths = page_paths(stem, n)
            if not paths["ocr"].exists():
                continue
            ocr = json.loads(paths["ocr"].read_text())
            en = (json.loads(paths["en"].read_text())
                  if paths["en"].exists() else {})
            rec = {
                "pdf": pdf_name,
                "page": n,
                "lang": en.get("source_lang") or ocr.get("lang_detected", []),
                "lang_token_level": ocr.get("lang_detected", []),
                "text": ocr.get("text", ""),
                "text_en": en.get("text_en", ""),
                "tokens": ocr.get("tokens", []),
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------- driver ----------

def run(pdf_name: str, pages_spec: str | None, steps: set[str],
        workers: int) -> int:
    pdf_path = SRC_DIR / pdf_name
    if not pdf_path.exists():
        print(f"missing pdf: {pdf_path}", file=sys.stderr)
        return 1
    stem = pdf_path.stem

    with fitz.open(pdf_path) as doc:
        total = doc.page_count
    pages = parse_pages(pages_spec, total)
    print(f"{pdf_name}: {total} pages total, processing {len(pages)} "
          f"({pages[0]}..{pages[-1]})")

    failures: list[str] = []

    def drain(jobs, desc):
        for fut in tqdm(as_completed(jobs), total=len(jobs), desc=desc):
            try:
                fut.result()
            except Exception as e:
                n = jobs[fut]
                msg = f"{desc} page {n} FAILED: {type(e).__name__}: {e}"
                failures.append(msg)
                tqdm.write(f"  {msg}")

    if "render" in steps:
        with ThreadPoolExecutor(max_workers=min(workers, 4)) as pool:
            jobs = {pool.submit(render_page, pdf_path, n - 1,
                                page_paths(stem, n)["png"]): n for n in pages}
            drain(jobs, "render")

    if "ocr" in steps:
        client = make_docai_client()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            jobs = {pool.submit(ocr_page, client,
                                page_paths(stem, n)["png"],
                                page_paths(stem, n)["ocr"]): n for n in pages}
            drain(jobs, "ocr")

    if "translate" in steps:
        client = make_translate_client()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            jobs = {pool.submit(translate_page, client,
                                page_paths(stem, n)["ocr"],
                                page_paths(stem, n)["en"]): n for n in pages}
            drain(jobs, "translate")

    if failures:
        print(f"  WARNING: {len(failures)} per-page failures (logged above)")

    if "searchable" in steps:
        out = OUT_DIR / "searchable" / f"{stem}.pdf"
        print(f"writing searchable PDF: {out}")
        write_searchable_pdf(pdf_path, stem, pages, out)

    if "jsonl" in steps:
        out = OUT_DIR / "text" / f"{stem}.jsonl"
        print(f"writing jsonl: {out}")
        emit_jsonl(stem, pdf_name, pages, out)

    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default=None,
                    help="filename inside rotated-pdf/ (omit to process all)")
    ap.add_argument("--pages", default=None,
                    help='page spec, e.g. "1-10" or "1,3,5-7"')
    ap.add_argument("--steps",
                    default="render,ocr,translate,searchable,jsonl",
                    help="comma-separated subset of steps to run")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    steps = {s.strip() for s in args.steps.split(",") if s.strip()}

    if args.pdf:
        targets = [args.pdf]
    else:
        targets = sorted(p.name for p in SRC_DIR.glob("*.pdf"))
        print(f"processing all {len(targets)} PDF(s) in {SRC_DIR.name}/")
    rc = 0
    for name in targets:
        print()
        print(f"=== {name} ===")
        rc |= run(name, args.pages, steps, args.workers)
    return rc


if __name__ == "__main__":
    sys.exit(main())
