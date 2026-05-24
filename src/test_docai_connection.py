"""Probe Google Document AI connectivity.

Loads credentials from secrets/gcp-service-account.json, then:
  1. Lists existing processors in `us` and `eu`.
  2. If no OCR processor exists in the preferred region, creates one.
  3. Prints the processor resource name to use from now on.

Usage:
    .venv/bin/python test_docai_connection.py [--create] [--region eu|us]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KEY_PATH = ROOT / "secrets" / "gcp-service-account.json"
PROJECT_ID = "gen-lang-client-0551697674"
DEFAULT_REGION = "eu"
PROCESSOR_DISPLAY_NAME = "n301xt-logbook-ocr"
PROCESSOR_TYPE = "OCR_PROCESSOR"

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(KEY_PATH)
os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID

from google.api_core.client_options import ClientOptions
from google.cloud import documentai


def client_for(region: str) -> documentai.DocumentProcessorServiceClient:
    opts = ClientOptions(api_endpoint=f"{region}-documentai.googleapis.com")
    return documentai.DocumentProcessorServiceClient(client_options=opts)


def list_region(region: str) -> list[documentai.Processor]:
    client = client_for(region)
    parent = client.common_location_path(PROJECT_ID, region)
    return list(client.list_processors(parent=parent))


def create_ocr_processor(region: str) -> documentai.Processor:
    client = client_for(region)
    parent = client.common_location_path(PROJECT_ID, region)
    proc = documentai.Processor(
        display_name=PROCESSOR_DISPLAY_NAME,
        type_=PROCESSOR_TYPE,
    )
    return client.create_processor(parent=parent, processor=proc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--create", action="store_true",
                    help="Create an OCR processor if none exists.")
    ap.add_argument("--region", default=DEFAULT_REGION, choices=["us", "eu"])
    args = ap.parse_args()

    if not KEY_PATH.exists():
        print(f"missing key file: {KEY_PATH}", file=sys.stderr)
        return 1

    print(f"project: {PROJECT_ID}")
    print(f"key:     {KEY_PATH}")
    print()

    existing = {}
    for region in ("us", "eu"):
        try:
            procs = list_region(region)
        except Exception as e:
            print(f"[{region}] ERROR: {type(e).__name__}: {e}")
            continue
        existing[region] = procs
        print(f"[{region}] {len(procs)} processor(s):")
        for p in procs:
            print(f"  - {p.display_name!r}  type={p.type_}  state={p.state.name}")
            print(f"    name={p.name}")

    ocr_in_region = [
        p for p in existing.get(args.region, [])
        if p.type_ == PROCESSOR_TYPE
    ]
    if ocr_in_region:
        print()
        print(f"OK: existing OCR processor in {args.region}:")
        print(f"  {ocr_in_region[0].name}")
        return 0

    if not args.create:
        print()
        print(f"No OCR processor in {args.region}. Re-run with --create to make one.")
        return 0

    print()
    print(f"Creating OCR processor in {args.region}...")
    try:
        new_proc = create_ocr_processor(args.region)
    except Exception as e:
        print(f"create_processor FAILED: {type(e).__name__}: {e}")
        return 2
    print(f"Created: {new_proc.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
