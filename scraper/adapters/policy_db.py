"""policy-db (State Council search, CAC, MIIT): result listings -> document pages.
Document pages carry document_number (〔YYYY〕 / 字第…号 forms) when present."""
import json

from scraper.adapters._common import AdapterError, extract_links, parse_article


def _json_result_urls(text, entry):
    """gov.cn's policy library serves JSON; pull document URLs out of it."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    urls = []
    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("url", "childurl", "curl") and isinstance(v, str) and v.startswith("http"):
                    urls.append(v)
                else:
                    walk(v)
        elif isinstance(obj, list):
            for it in obj:
                walk(it)
    walk(data)
    from scraper.fetcher import host_allowed
    return [u for u in urls if host_allowed(u, entry["allowed_domains"])]


def discover(entry, fetcher, cap=None):
    cap = cap or entry["backfill_cap"]
    r = fetcher.fetch(entry["list_url"], entry["allowed_domains"], max_age_days=entry["cadence_days"])
    if not r.ok:
        raise AdapterError(f"{entry['id']}: listing fetch failed ({r.skip_reason or r.status})")
    urls = None
    if r.content_type == "application/json" or entry["recipe"].get("json_listing"):
        urls = _json_result_urls(r.text, entry)
    if urls is None:
        urls = extract_links(r.text, entry["list_url"], entry["recipe"].get("list_selector"),
                             entry["allowed_domains"], entry["recipe"].get("article_url_pattern"))
    if not urls:
        raise AdapterError(f"{entry['id']}: zero document links (JS-rendered listing?)")
    return urls[:cap]


def parse(html_text, entry, url=""):
    return parse_article(html_text, entry, url)
