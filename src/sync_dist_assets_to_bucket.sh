#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"

load_railway_bucket_credentials() {
  if ! command -v railway >/dev/null 2>&1; then
    return
  fi

  local line key value
  while IFS= read -r line; do
    [[ "${line}" == *=* ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    case "${key}" in
      AWS_ENDPOINT_URL|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_S3_BUCKET_NAME|AWS_DEFAULT_REGION|AWS_S3_URL_STYLE)
        export "${key}=${value}"
        ;;
    esac
  done < <(railway bucket credentials --bucket "${RAILWAY_BUCKET_NAME:-assets}" 2>/dev/null || true)
}

if [[ -z "${BUCKET_NAME:-${BUCKET:-${AWS_S3_BUCKET_NAME:-${AWS_BUCKET_NAME:-}}}}" ]] ||
   [[ -z "${BUCKET_ENDPOINT:-${ENDPOINT:-${AWS_ENDPOINT_URL_S3:-${AWS_ENDPOINT_URL:-}}}}" ]]; then
  load_railway_bucket_credentials
fi

bucket_name="${BUCKET_NAME:-${BUCKET:-${AWS_S3_BUCKET_NAME:-${AWS_BUCKET_NAME:-}}}}"
endpoint="${BUCKET_ENDPOINT:-${ENDPOINT:-${AWS_ENDPOINT_URL_S3:-${AWS_ENDPOINT_URL:-}}}}"
region="${BUCKET_REGION:-${REGION:-${AWS_DEFAULT_REGION:-auto}}}"

if [[ -z "${bucket_name}" || -z "${endpoint}" ]]; then
  echo "Missing bucket name or endpoint." >&2
  echo "Run these as two separate commands:" >&2
  echo '  eval "$(railway bucket credentials --bucket assets)"' >&2
  echo "  ./src/sync_dist_assets_to_bucket.sh" >&2
  exit 1
fi

if [[ -n "${BUCKET_ACCESS_KEY_ID:-}" ]]; then
  export AWS_ACCESS_KEY_ID="${BUCKET_ACCESS_KEY_ID}"
fi

if [[ -n "${BUCKET_SECRET_ACCESS_KEY:-}" ]]; then
  export AWS_SECRET_ACCESS_KEY="${BUCKET_SECRET_ACCESS_KEY}"
fi

export AWS_DEFAULT_REGION="${region}"

if [[ -z "${AWS_ACCESS_KEY_ID:-}" || -z "${AWS_SECRET_ACCESS_KEY:-}" ]]; then
  echo "Missing AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY." >&2
  echo "Run these as two separate commands:" >&2
  echo '  eval "$(railway bucket credentials --bucket assets)"' >&2
  echo "  ./src/sync_dist_assets_to_bucket.sh" >&2
  exit 1
fi

echo "Syncing PDF page assets..."
aws s3 sync "${DIST_DIR}/pages" "s3://${bucket_name}/pages" \
  --endpoint-url "${endpoint}" \
  --only-show-errors \
  --exclude ".DS_Store" \
  --cache-control "public, max-age=31536000, immutable"

echo "Syncing page text assets..."
aws s3 sync "${DIST_DIR}/text" "s3://${bucket_name}/text" \
  --endpoint-url "${endpoint}" \
  --only-show-errors \
  --exclude ".DS_Store" \
  --cache-control "public, max-age=31536000, immutable"

echo "Done."
