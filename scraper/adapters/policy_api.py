"""policy-api (annex v1.1 Rung A): a JSON data endpoint discovered by bundle
analysis replaces a JS-rendered listing. GET only, through the normal fetcher
(all gates apply; application/json was always an allowed content type).

recipe["api"] = {
  "endpoint": "https://.../search?q={q}&page={page}",   # {q}/{page} substituted
  "q": "人工智能",                                        # already URL-safe? encoded here
  "page_start": 1,
  "max_pages": 3,                                        # hard bound, <= registry cap
  "url_keys": ["url", "childurl", "curl"],               # walked over the JSON
  "url_prefix": "",                                      # optional urljoin base for relative urls
  "article_json": {"title": "title", "date": "pubtime",  # ONLY if document pages
                   "body": "content"}                    # themselves are JSON
}

Document pages are fetched per-URL by run.py and parsed here: HTML pages go
through parse_article with the entry's article selectors; JSON payloads are
mapped via article_json (body HTML fragments are still parsed as text with
selectolax — nothing is ever executed).
"""
import json
import re
from urllib.parse import quote, urljoin

from selectolax.lexbor import LexborHTMLParser

from scraper.adapters._common import (AdapterError, clean_tree, collect_attachment_links,
                                      extract_document_number, extract_links, parse_article,
                                      parse_date_zh)
from scraper.fetcher import host_allowed

MAX_PAGES_HARD = 10


def _dig(data, dotted):
    cur = data
    for part in (dotted or "").split("."):
        if not part:
            return None
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _walk_urls(obj, url_keys, found):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in url_keys and isinstance(v, str) and v.strip():
                found.append(v.strip())
            else:
                _walk_urls(v, url_keys, found)
    elif isinstance(obj, list):
        for it in obj:
            _walk_urls(it, url_keys, found)


def discover(entry, fetcher, cap=None):
    cap = cap or entry["backfill_cap"]
    api = entry["recipe"].get("api")
    if not api or not api.get("endpoint"):
        raise AdapterError(f"{entry['id']}: policy-api recipe missing api.endpoint")
    url_keys = set(api.get("url_keys") or ["url", "childurl", "curl"])
    prefix = api.get("url_prefix") or entry["list_url"]
    q = quote(api.get("q", ""), safe="")
    urls, seen = [], set()
    page = int(api.get("page_start", 1))
    max_pages = min(int(api.get("max_pages", 3)), MAX_PAGES_HARD)
    for _ in range(max_pages):
        page_url = api["endpoint"].replace("{q}", q).replace("{page}", str(page))
        r = fetcher.fetch(page_url, entry["allowed_domains"], max_age_days=entry["cadence_days"])
        if not r.ok:
            raise AdapterError(f"{entry['id']}: api fetch failed ({r.skip_reason or r.status}) {page_url}")
        try:
            data = json.loads(r.text)
        except ValueError as e:
            raise AdapterError(f"{entry['id']}: api returned non-JSON ({e}) {page_url}")
        found = []
        html_path = api.get("list_html_path")
        if html_path:
            # CMS envelope: links live inside an HTML fragment at a JSON path
            # (e.g. MIIT jpaas returns {data:{html:"<ul>...<a href=..."}})
            fragment = _dig(data, html_path)
            if not isinstance(fragment, str):
                raise AdapterError(f"{entry['id']}: no HTML at api path {html_path!r} (schema drift?)")
            found = extract_links(fragment, prefix, entry["recipe"].get("list_selector"),
                                  entry["allowed_domains"], entry["recipe"].get("article_url_pattern"))
        else:
            _walk_urls(data, url_keys, found)
        fresh = 0
        for u in found:
            u = urljoin(prefix, u)
            if u in seen or not host_allowed(u, entry["allowed_domains"]):
                continue
            seen.add(u)
            urls.append(u)
            fresh += 1
        if fresh == 0 or len(urls) >= cap:
            break
        page += 1
    if not urls:
        raise AdapterError(f"{entry['id']}: api yielded zero document urls (schema drift?)")
    return urls[:cap]


def _fragment_paras(html_fragment):
    tree = LexborHTMLParser(f"<div id='__w'>{html_fragment}</div>")
    tree.strip_tags(["script", "style", "iframe", "object", "embed"])
    root = tree.css_first("#__w")
    paras = []
    if root is not None:
        for node in root.css("p, h2, h3, h4, li"):
            t = re.sub(r"\s+", " ", node.text(separator=" ", strip=True)).strip()
            if t:
                paras.append(t)
        if not paras:
            text = root.text(separator="\n", strip=True)
            paras = [re.sub(r"\s+", " ", ln).strip() for ln in text.split("\n") if ln.strip()]
    return paras


def parse(text, entry, url=""):
    aj = entry["recipe"].get("api", {}).get("article_json")
    stripped = text.lstrip()
    if aj and stripped[:1] in "[{":
        try:
            data = json.loads(stripped)
        except ValueError:
            raise AdapterError(f"{entry['id']}: article payload is not valid JSON: {url}")
        def dig(path):
            cur = data
            for part in (path or "").split("."):
                if not part:
                    return None
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    return None
            return cur if isinstance(cur, str) else None
        title = dig(aj.get("title"))
        body_html = dig(aj.get("body")) or ""
        paras = _fragment_paras(body_html)
        if not title or not paras:
            raise AdapterError(
                f"{entry['id']}: json article yielded title={bool(title)} paras={len(paras)} for {url}")
        tree = clean_tree(body_html)
        full_text = title + "\n" + "\n".join(paras)
        return {
            "title": title.strip(),
            "date": parse_date_zh(dig(aj.get("date")) or ""),
            "author": (dig(aj.get("author")) or None),
            "body_paragraphs": paras,
            "document_number": extract_document_number(full_text),
            "attachments": collect_attachment_links(tree, url, entry["allowed_domains"]),
        }
    return parse_article(text, entry, url)
