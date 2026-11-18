"""journal-toc (自然辩证法通讯): TOC -> per-article pages/abstracts.
If full text is not openly served, the abstract is captured (body_complete=False
is decided by the normalizer via the abstract_only recipe flag)."""
from scraper.adapters._common import AdapterError, extract_links, parse_article


def discover(entry, fetcher, cap=None):
    cap = cap or entry["backfill_cap"]
    recipe = entry["recipe"]
    r = fetcher.fetch(entry["list_url"], entry["allowed_domains"], max_age_days=entry["cadence_days"])
    if not r.ok:
        raise AdapterError(f"{entry['id']}: TOC fetch failed ({r.skip_reason or r.status})")
    page_url, html = entry["list_url"], r.text
    if recipe.get("toc_url_pattern"):  # homepage -> newest issue TOC first
        toc_links = extract_links(html, page_url, recipe.get("toc_selector", "a"),
                                  entry["allowed_domains"], recipe["toc_url_pattern"])
        if toc_links:
            rt = fetcher.fetch(toc_links[0], entry["allowed_domains"], max_age_days=entry["cadence_days"])
            if rt.ok:
                page_url, html = toc_links[0], rt.text
    urls = extract_links(html, page_url, recipe.get("list_selector"),
                         entry["allowed_domains"], recipe.get("article_url_pattern"))
    if not urls:
        raise AdapterError(f"{entry['id']}: zero article links on TOC")
    return urls[:cap]


def parse(html_text, entry, url=""):
    return parse_article(html_text, entry, url)
