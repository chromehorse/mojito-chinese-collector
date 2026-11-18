"""issue-archive (Qiushi): mulu page -> per-issue pages -> article links, newest-first."""
import re

from scraper.adapters._common import AdapterError, clean_tree, extract_links, parse_article

MAX_ISSUES = 30


def _date_key(url):
    m = re.search(r"/(\d{8})/", url)
    return m.group(1) if m else "00000000"


def discover(entry, fetcher, cap=None):
    cap = cap or entry["backfill_cap"]
    recipe = entry["recipe"]
    r = fetcher.fetch(entry["list_url"], entry["allowed_domains"], max_age_days=entry["cadence_days"])
    if not r.ok:
        raise AdapterError(f"{entry['id']}: mulu fetch failed ({r.skip_reason or r.status})")
    list_page_url, list_html = entry["list_url"], r.text
    if recipe.get("year_link_selector"):
        # mulu -> current-year page, which holds the per-issue links
        year_links = extract_links(list_html, list_page_url, recipe["year_link_selector"],
                                   entry["allowed_domains"], recipe.get("year_url_pattern"))
        if not year_links:
            raise AdapterError(f"{entry['id']}: zero year links on mulu page")
        ry = fetcher.fetch(year_links[0], entry["allowed_domains"], max_age_days=entry["cadence_days"])
        if not ry.ok:
            raise AdapterError(f"{entry['id']}: year page fetch failed ({ry.skip_reason or ry.status})")
        list_page_url, list_html = year_links[0], ry.text
    issue_urls = extract_links(list_html, list_page_url, recipe.get("issue_link_selector", "a"),
                               entry["allowed_domains"], recipe.get("issue_url_pattern"))
    issue_urls = [u for u in issue_urls if u != list_page_url]
    if recipe.get("issue_date_sort"):
        issue_urls.sort(key=_date_key, reverse=True)  # newest-first
    if not issue_urls:
        raise AdapterError(f"{entry['id']}: zero issue links on mulu page")
    urls = []
    max_issues = int(recipe.get("max_issues", MAX_ISSUES))
    for issue_url in issue_urls[:max_issues]:  # mulu lists newest first
        if len(urls) >= cap:
            break
        ri = fetcher.fetch(issue_url, entry["allowed_domains"], max_age_days=entry["cadence_days"])
        if not ri.ok:
            fetcher._warn(f"{entry['id']}: issue page failed {issue_url} ({ri.skip_reason})")
            continue
        for u in extract_links(ri.text, issue_url, recipe.get("list_selector", "a"),
                               entry["allowed_domains"], recipe.get("article_url_pattern")):
            if u not in urls and u != issue_url:
                urls.append(u)
    if not urls:
        raise AdapterError(f"{entry['id']}: zero article links across issues")
    return urls[:cap]


def parse(html_text, entry, url=""):
    article = parse_article(html_text, entry, url)
    if not article["author"] and entry["id"] == "qiushi":
        for label in clean_tree(html_text).css("div.inner span.appellation"):
            if match := re.fullmatch(r"作者\s*[:：]\s*(.+)", label.text(strip=True)):
                article["author"] = match[1].strip()
                break
    return article
