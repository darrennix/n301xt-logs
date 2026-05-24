"""
Auto-rotate scanned PDF pages using dictionary-voted OCR.

For every PDF in original-pdf/ produce a copy in rotated-pdf/ where each
page's /Rotate metadata has been corrected so the page renders upright.
No re-rasterizing of scanned images, no third-party API calls.

Detection algorithm:
  Render each page once at --dpi (default 200), then for each rotation in
  [0, 90, 180, 270] OCR the rotated image (with deu+eng) and count how
  many recognized tokens match a real word in a combined English+German
  dictionary. Tokens are required to be at least MIN_TOKEN_LEN chars to
  avoid the 3-letter coincidence problem (random gibberish at the wrong
  rotation produces many spurious 3-letter dictionary matches). Hits are
  length-weighted so long real words dominate. Pages with no hits in any
  rotation are left unchanged and flagged 'no_text'.

Usage:
    .venv/bin/python rotate_pdfs.py
    .venv/bin/python rotate_pdfs.py --only "D CHIC - L-AKTE - 1-11.pdf"
    .venv/bin/python rotate_pdfs.py --workers 6 --dpi 200 --min-hits 8
"""

from __future__ import annotations

import argparse
import io
import multiprocessing as mp
import os
import re
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import fitz  # PyMuPDF
import pikepdf
import pytesseract
from PIL import Image
from tqdm import tqdm

ROOT = Path(__file__).resolve().parent
SRC_DIR = ROOT / "original-pdf"
DST_DIR = ROOT / "rotated-pdf"
REPORT_DIR = DST_DIR / "_reports"
DICT_PATHS = (
    Path("/usr/share/dict/words"),    # English (web2, ~236k entries)
    ROOT / "dicts" / "de.txt",        # German wordlist (enz/german-wordlist)
)
MIN_TOKEN_LEN = 5  # discard tokens shorter than this when scoring
OCR_LANG = "deu+eng"

ROTATIONS = (0, 90, 180, 270)
# Allow ASCII letters plus the German umlauts/eszett in tokens so OCR
# output like "Flugzeug" or "Größe" is preserved. We accept either
# precomposed characters or fallbacks Tesseract sometimes produces (e.g.
# "ae" / "oe" / "ue" / "ss") naturally via the dictionary.
TOKEN_RE = re.compile(rf"[A-Za-zÄÖÜäöüß]{{{MIN_TOKEN_LEN},}}")

# Loaded lazily inside each worker process.
_DICT: frozenset[str] | None = None


def _normalize(word: str) -> str:
    return word.strip().lower()


def load_dict(paths=DICT_PATHS) -> frozenset[str]:
    words: set[str] = set()
    for p in paths:
        if not p.exists():
            continue
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                w = _normalize(line)
                if not w or "'" in w or len(w) < MIN_TOKEN_LEN:
                    continue
                words.add(w)
    return frozenset(words)


def _get_dict() -> frozenset[str]:
    global _DICT
    if _DICT is None:
        _DICT = load_dict()
    return _DICT


def render_page_png(pdf_path: str, page_index: int, dpi: int) -> bytes:
    """Render a page from raw (ignoring /Rotate) to a grayscale PNG."""
    doc = fitz.open(pdf_path)
    try:
        page = doc.load_page(page_index)
        # In-memory only: forces the renderer to ignore the page's /Rotate
        # so detection runs against the underlying scan, not whatever value
        # the source PDF already had.
        page.set_rotation(0)
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY, alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()


def score_orientation(im: Image.Image, words: frozenset[str]) -> tuple[int, int]:
    """Return (length_weighted_score, total_tokens_seen) for the oriented image.

    Score formula: sum over distinct dictionary-hit tokens of
    (len(token) - MIN_TOKEN_LEN + 1). A 5-letter hit contributes 1, a
    7-letter hit 3, a 10-letter hit 6 — heavily favoring real long words
    over short coincidences.
    """
    text = pytesseract.image_to_string(
        im, lang=OCR_LANG, config="--psm 6 --oem 1"
    )
    score = 0
    total = 0
    seen: set[str] = set()
    for tok in TOKEN_RE.findall(text):
        tl = tok.lower()
        total += 1
        if tl in seen:
            continue
        seen.add(tl)
        if tl in words:
            score += len(tl) - MIN_TOKEN_LEN + 1
    return score, total


def detect_page(pdf_path: str, page_index: int, dpi: int) -> dict:
    """Run dictionary-vote orientation detection on one page.

    Returns a dict with keys:
        page_index, scores ({deg: hits}), totals ({deg: total_tokens}),
        best_rotation, error
    """
    out = {
        "page_index": page_index,
        "scores": {r: 0 for r in ROTATIONS},
        "totals": {r: 0 for r in ROTATIONS},
        "best": None,
        "error": None,
    }
    try:
        words = _get_dict()
        png = render_page_png(pdf_path, page_index, dpi)
        with Image.open(io.BytesIO(png)) as base:
            base.load()
            for r in ROTATIONS:
                # PIL.rotate is counter-clockwise by default; we want the
                # *page* to appear rotated clockwise by r so OCR sees that
                # orientation. expand=True preserves the full image.
                im = base if r == 0 else base.rotate(-r, expand=True)
                hits, total = score_orientation(im, words)
                out["scores"][r] = hits
                out["totals"][r] = total
        out["best"] = max(out["scores"], key=lambda k: out["scores"][k])
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}".strip()
    return out


def decide(scores: dict[int, int], min_hits: int, margin: float, min_gap: int):
    """Return (chosen_rotation, decision_class)."""
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_r, best_s = ordered[0]
    runner_s = ordered[1][1] if len(ordered) > 1 else 0
    if best_s == 0:
        return 0, "no_text"
    confident = (
        best_s >= min_hits
        and best_s - runner_s >= min_gap
        and (runner_s == 0 or best_s >= runner_s * margin)
    )
    return best_r, ("confident" if confident else "weak")


def save_rotated_pdf(
    src: Path, dst: Path, report: Path, results: list[dict],
    dpi: int, min_hits: int, margin: float, min_gap: int,
) -> dict:
    """Write the rotated PDF + per-page report from already-computed results."""
    summary = {
        "file": src.name, "pages": len(results),
        "rotated": 0, "kept_zero": 0, "no_text": 0, "weak": 0, "errors": 0,
    }
    DST_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = dst.with_suffix(dst.suffix + ".tmp")

    with pikepdf.open(str(src)) as pdf, open(report, "w") as rf:
        rf.write(f"# {src.name}\n")
        rf.write(
            f"# pages={len(results)} dpi={dpi} min_hits={min_hits} "
            f"margin={margin} min_gap={min_gap}\n"
        )
        rf.write(
            "# page\texisting\ts0\ts90\ts180\ts270\tt0\tt90\tt180\tt270\t"
            "applied\tdecision\tnote\n"
        )
        for i, r in enumerate(results):
            page = pdf.pages[i]
            existing = int(page.obj.get("/Rotate", 0)) % 360
            if r is None or r["error"] is not None:
                err = (r["error"] if r else "no_result")
                rf.write(
                    f"{i}\t{existing}\t0\t0\t0\t0\t0\t0\t0\t0\t"
                    f"{existing}\terror\t{err}\n"
                )
                summary["errors"] += 1
                continue

            chosen, decision = decide(r["scores"], min_hits, margin, min_gap)
            s, t = r["scores"], r["totals"]
            note = ""
            if decision == "no_text":
                applied = existing
                summary["no_text"] += 1
            else:
                applied = chosen
                if applied != existing:
                    page.Rotate = applied
                    summary["rotated"] += 1
                else:
                    summary["kept_zero"] += 1
                if decision == "weak":
                    summary["weak"] += 1
                    note = (
                        f"best={s[chosen]} "
                        f"runner={sorted(s.values(), reverse=True)[1]}"
                    )
            rf.write(
                f"{i}\t{existing}\t{s[0]}\t{s[90]}\t{s[180]}\t{s[270]}\t"
                f"{t[0]}\t{t[90]}\t{t[180]}\t{t[270]}\t"
                f"{applied}\t{decision}\t{note}\n"
            )
        pdf.save(str(tmp_path))
    os.replace(tmp_path, dst)
    return summary


def _empty_result(page_index: int, error: str | None = None) -> dict:
    return {
        "page_index": page_index,
        "scores": {r: 0 for r in ROTATIONS},
        "totals": {r: 0 for r in ROTATIONS},
        "best": None,
        "error": error,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(SRC_DIR))
    ap.add_argument("--dst", default=str(DST_DIR))
    ap.add_argument("--only", default=None, help="Process a single PDF filename")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--min-hits", type=int, default=15)
    ap.add_argument("--margin", type=float, default=1.5)
    ap.add_argument("--min-gap", type=int, default=8)
    ap.add_argument(
        "--workers", type=int,
        default=max(1, (os.cpu_count() or 4) - 2),
        help="Total parallel page-OCR workers across all PDFs",
    )
    ap.add_argument("--force", action="store_true", help="Re-process files that already exist")
    args = ap.parse_args()

    src_dir = Path(args.src)
    dst_dir = Path(args.dst)
    dst_dir.mkdir(parents=True, exist_ok=True)
    (dst_dir / "_reports").mkdir(parents=True, exist_ok=True)

    if args.only:
        candidates = [src_dir / args.only]
        if not candidates[0].exists():
            print(f"error: {candidates[0]} not found", file=sys.stderr)
            return 2
    else:
        candidates = sorted(p for p in src_dir.glob("*.pdf"))

    # Filter out already-completed files (resumability).
    todo: list[tuple[Path, int]] = []
    skipped = 0
    for src in candidates:
        dst = dst_dir / src.name
        if dst.exists() and not args.force:
            print(f"skip (exists): {src.name}")
            skipped += 1
            continue
        try:
            with fitz.open(str(src)) as doc:
                n = doc.page_count
        except Exception as e:
            print(f"FAILED to open {src.name}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        todo.append((src, n))

    if not todo:
        print("nothing to do.")
        return 0

    total_pages = sum(n for _, n in todo)
    print(
        f"Processing {len(todo)} PDF(s) ({total_pages} pages) with "
        f"{args.workers} workers @ {args.dpi} DPI "
        f"(min_hits={args.min_hits}, margin={args.margin}, min_gap={args.min_gap})"
    )

    # One global pool for all PDFs. Pages stream out as workers finish, so
    # there is no idle time between files. Each PDF is saved as soon as all
    # of its pages have been scored.
    results: dict[str, list[dict | None]] = {
        src.name: [None] * n for src, n in todo
    }
    pending: dict[str, int] = {src.name: n for src, n in todo}
    src_by_name: dict[str, Path] = {src.name: src for src, _ in todo}
    file_t0: dict[str, float] = {}

    overall_t0 = time.time()
    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=args.workers, mp_context=ctx) as ex:
        futures = {}
        for src, n in todo:
            file_t0[src.name] = time.time()
            for i in range(n):
                fut = ex.submit(detect_page, str(src), i, args.dpi)
                futures[fut] = (src.name, i)

        with tqdm(total=total_pages, unit="pg", smoothing=0.05) as bar:
            for fut in as_completed(futures):
                name, i = futures[fut]
                try:
                    results[name][i] = fut.result()
                except Exception as e:
                    results[name][i] = _empty_result(
                        i, f"future: {type(e).__name__}: {e}"
                    )
                pending[name] -= 1
                bar.update(1)

                if pending[name] == 0:
                    src = src_by_name[name]
                    dst = dst_dir / name
                    report = dst_dir / "_reports" / (name + ".tsv")
                    try:
                        summary = save_rotated_pdf(
                            src, dst, report, results[name],
                            dpi=args.dpi, min_hits=args.min_hits,
                            margin=args.margin, min_gap=args.min_gap,
                        )
                        elapsed = round(time.time() - file_t0[name], 1)
                        bar.write(
                            f"done: {name} pages={summary['pages']} "
                            f"rotated={summary['rotated']} weak={summary['weak']} "
                            f"no_text={summary['no_text']} err={summary['errors']} "
                            f"in {elapsed}s"
                        )
                    except Exception as e:
                        bar.write(
                            f"FAILED {name} during save: "
                            f"{type(e).__name__}: {e}"
                        )
                        traceback.print_exc()
                    # Free memory.
                    results.pop(name, None)

    print(f"total elapsed: {round(time.time() - overall_t0, 1)}s "
          f"(skipped {skipped} already-done file(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
