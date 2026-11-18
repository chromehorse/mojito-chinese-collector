"""Read-only recipe health checks. A zero-return or parse-fail is a FINDING
reported for a human to act on — nothing here deactivates a source (the
mass-deactivation scar: no streak logic exists in this codebase, deliberately)."""
from scraper.adapters import BY_KIND
from scraper.adapters._common import AdapterError

EXPECTED_MIN_LINKS = {"seed-doc": 1}
DEFAULT_EXPECTED_MIN = 3


def check(entry, fetcher, sample_cap=5):
    adapter = BY_KIND[entry["kind"]]
    report = {"id": entry["id"], "status": entry["status"], "verdict": None,
              "listing_links": 0, "expected_min": EXPECTED_MIN_LINKS.get(entry["kind"], DEFAULT_EXPECTED_MIN),
              "sample_url": None, "sample_title": None, "sample_body_paras": 0, "detail": None}
    try:
        urls = adapter.discover(entry, fetcher, cap=sample_cap)
    except AdapterError as e:
        report["verdict"] = "broken-recipe"
        report["detail"] = str(e)
        return report
    report["listing_links"] = len(urls)
    if len(urls) < report["expected_min"]:
        report["verdict"] = "broken-recipe"
        report["detail"] = f"only {len(urls)} links (< {report['expected_min']})"
        return report
    sample = urls[0]
    report["sample_url"] = sample
    r = fetcher.fetch(sample, entry["allowed_domains"], max_age_days=entry["cadence_days"])
    if not r.ok:
        report["verdict"] = "blocked" if r.status in (401, 403, 451) else "broken-recipe"
        report["detail"] = f"sample fetch: {r.skip_reason or r.status}"
        return report
    try:
        parsed = adapter.parse(r.text, entry, sample)
    except AdapterError as e:
        report["verdict"] = "broken-recipe"
        report["detail"] = str(e)
        return report
    report["sample_title"] = parsed["title"][:80]
    report["sample_body_paras"] = len(parsed["body_paragraphs"])
    report["verdict"] = "healthy"
    return report
