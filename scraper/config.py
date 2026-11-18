"""Project-wide constants. All paths resolve from this file, never from cwd."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SOURCES_PATH = PROJECT_ROOT / "_sources.json"
CACHE_DIR = PROJECT_ROOT / "cache"
OUTPUT_DIR = PROJECT_ROOT / "output"
NORMALIZED_DIR = OUTPUT_DIR / "_normalized"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"

USER_AGENT = "MojitoResearchCrawler/1.0 (personal academic research)"
PER_DOMAIN_DELAY_SECONDS = 8.0   # minimum; jitter added on top
DELAY_JITTER_SECONDS = 4.0       # uniform 0..jitter added to delay
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
TIMEOUT_SECONDS = 30.0
MAX_REDIRECTS = 5
MAX_RETRIES = 2                  # only on timeout / 5xx

ALLOWED_CONTENT_TYPES = {
    "text/html",
    "text/plain",
    "application/xhtml+xml",
    "application/json",
    "application/xml",  # sitemaps only
}

# Annex v1.1 amendment 1: JS bundles may be fetched AS TEXT for Rung-A endpoint
# analysis only (bundle_scan.py passes these via extra_types). Never executed,
# never imported, cache-only storage, same 5MB cap.
JS_ANALYSIS_CONTENT_TYPES = {
    "application/javascript",
    "text/javascript",
    "application/x-javascript",
}

# Annex v1.1 amendment 2: PDFs downloadable under Rung-B rules only —
# quarantine dir, Defender scan, pure-Python extraction, never opened.
PDF_CONTENT_TYPES = {"application/pdf"}
MAX_PDF_BYTES = 30 * 1024 * 1024
QUARANTINE_DIR = PROJECT_ROOT / "quarantine"
