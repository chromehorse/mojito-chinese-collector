"""listing: index page(s) -> article links (PD commentary/钟声, Xinhua, CAICT, I-AIIG).

Recipe options:
  list_selector / article_url_pattern  — article links on the index page
  next_page_selector                   — same-domain pagination (bounded)
  section_link_selector/section_url_pattern — hub page -> section indexes -> articles
  self_article: true                   — the list_url IS the single article
"""
from scraper.adapters._common import AdapterError, extract_links, parse_article

MAX_PAGES = 5
MAX_SECTIONS = 5


def _collect(entry, fetcher, page_url, cap, urls):
    recipe = entry["recipe"]
    pages = 0
    while page_url and pages < MAX_PAGES and len(urls) < cap:
        r = fetcher.fetch(page_url, entry["allowed_domains"], max_age_days=entry["cadence_days"])
        if not r.ok:
            if pages == 0:
                fetcher._warn(f"{entry['id']}: index fetch failed {page_url} ({r.skip_reason or r.status})")
            break
        links = extract_links(r.text, page_url, recipe.get("list_selector"),
                              entry["allowed_domains"], recipe.get("article_url_pattern"))
        for u in links:
            if u not in urls and u != page_url:
                urls.append(u)
        pages += 1
        nxt = None
        if recipe.get("next_page_selector"):
            nxt_links = extract_links(r.text, page_url, recipe["next_page_selector"],
                                      entry["allowed_domains"])
            nxt = nxt_links[0] if nxt_links else None
        page_url = nxt if nxt != page_url else None
    return urls


def discover(entry, fetcher, cap=None):
    cap = cap or entry["backfill_cap"]
    recipe = entry["recipe"]
    if recipe.get("self_article"):
        return [entry["list_url"]]
    urls = []
    if recipe.get("section_link_selector"):
        r = fetcher.fetch(entry["list_url"], entry["allowed_domains"],
                          max_age_days=entry["cadence_days"])
        if not r.ok:
            raise AdapterError(f"{entry['id']}: hub fetch failed ({r.skip_reason or r.status})")
        sections = extract_links(r.text, entry["list_url"], recipe["section_link_selector"],
                                 entry["allowed_domains"], recipe.get("section_url_pattern"))
        # articles linked directly on the hub count too
        _collect_page_only(entry, r.text, urls)
        for sec in sections[:MAX_SECTIONS]:
            if len(urls) >= cap:
                break
            _collect(entry, fetcher, sec, cap, urls)
    else:
        # discover_url: index lives on a different page than the pinned list_url
        # (e.g. caict, whose list_url is itself an article on the news hub)
        _collect(entry, fetcher, recipe.get("discover_url", entry["list_url"]), cap, urls)
    if not urls:
        raise AdapterError(f"{entry['id']}: listing yielded zero article links")
    return urls[:cap]


def _collect_page_only(entry, html, urls):
    recipe = entry["recipe"]
    for u in extract_links(html, entry["list_url"], recipe.get("list_selector"),
                           entry["allowed_domains"], recipe.get("article_url_pattern")):
        if u not in urls:
            urls.append(u)


def parse(html_text, entry, url=""):
    return parse_article(html_text, entry, url)
