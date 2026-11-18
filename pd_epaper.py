"""Use People's Daily's own e-paper for the blocked commentary indexes."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

import httpx

from run import load_manifest, process_recipe, save_manifest
from scraper.adapters._common import clean_tree, parse_article
from scraper.fetcher import Fetcher


HOST = "paper.people.com.cn"
SECTIONS = ("评论", "理论", "国际", "要闻")
COLUMNS = ("人民时评", "人民论坛", "人民观点", "评论员观察", "钟声", "纵横", "现场评论", "微观")
CONTROLS = {
    "2026-11-17": "content_30191569.html",
    "2026-09-16": "content_30181273.html",
}
ENTRY = {
    "id": "pd_epaper", "name": "人民日报 People's Daily e-paper",
    "kind": "listing", "allowed_domains": [HOST], "country": "CN", "language": "zh",
    "corpus": "government", "authority_level": "central-doctrinal",
    "source_genre_default": "institutional-commentary", "independence_cluster": "cc-party-press",
    "cadence_days": 1, "backfill_cap": 10, "status": "active",
    "recipe": {"self_article": True, "article": {
        "title": "h1", "date": ".date", "author": ".author, .article-author",
        "body": "#ozoom", "complete_body": True,
    }, "strict_html": {}},
}


def edition_url(edition):
    return f"https://{HOST}/rmrb/pc/layout/{edition:%Y%m}/{edition:%d}/node_01.html"


def local_links(html, page, path_pattern, labels):
    for anchor in clean_tree(html).css("a[href]"):
        href = anchor.attributes.get("href")
        url = urljoin(page, href)
        parts = urlsplit(url)
        if (parts.scheme == "https" and parts.netloc == HOST and not parts.query and not parts.fragment
                and re.fullmatch(path_pattern, parts.path)
                and any(label in anchor.text(strip=True) for label in labels)):
            yield url


def articles_from_edition(edition, fetcher):
    issue = edition_url(edition)
    landing = fetcher.fetch(issue, [HOST], max_age_days=1)
    if not landing.ok:
        return []
    key = f"{edition:%Y%m}/{edition:%d}"
    section_pattern = rf"/rmrb/pc/layout/{key}/node_\d+\.html"
    article_pattern = rf"/rmrb/pc/content/{key}/content_\d+\.html"
    sections = list(dict.fromkeys(local_links(landing.text, issue, section_pattern, SECTIONS)))
    articles = list(local_links(landing.text, issue, article_pattern, COLUMNS))
    for section in (url for url in sections[:12] if url != issue):
        content = fetcher.fetch(section, [HOST], max_age_days=1)
        if content.ok:
            articles.extend(local_links(content.text, section, article_pattern, COLUMNS))
    return list(dict.fromkeys(articles))


def parse_epaper(text, entry, url):
    parsed = parse_article(text, entry, url)
    if not parsed["author"] and "（钟声）" in parsed["title"]:
        parsed["author"] = "钟声"
    return parsed


def collect(output, dates, *, verify_controls):
    totals = {"status": "ok", "issues": len(dates), "discovered": 0, "parsed": 0,
              "written": 0, "confirmed_controls": 0, "parse_failures": 0, "fetch_failures": 0}
    fetcher = Fetcher(cache_dir=output.parent / "cache",
                      transport=httpx.HTTPTransport(trust_env=False), strict_html_hosts={HOST: {}})
    manifest_path = output / "manifest.json"
    manifest = load_manifest(manifest_path)
    try:
        for edition in dates:
            candidates = articles_from_edition(edition, fetcher)
            totals["discovered"] += len(candidates)
            control = CONTROLS.get(edition.isoformat()) if verify_controls else None
            if control:
                candidates = [url for url in candidates if url.endswith("/" + control)]
                totals["confirmed_controls"] += bool(candidates)
            for url in candidates[:4]:
                entry = {**ENTRY, "list_url": url}
                report = process_recipe(entry, fetcher, manifest, cap=1, incremental=True,
                                        out_dir=output / "_normalized", article_parser=parse_epaper)
                for key in ("parsed", "written", "parse_failures", "fetch_failures"):
                    totals[key] += report[key]
    finally:
        fetcher.close()
    if totals["parsed"]:
        save_manifest(manifest, manifest_path)
    if not totals["discovered"] or totals["fetch_failures"] or totals["parse_failures"]:
        totals["status"] = "partial"
    if verify_controls and (totals["confirmed_controls"] != len(dates) or totals["written"] != len(dates)):
        totals["status"] = "control_failed"
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-controls", action="store_true")
    parser.add_argument("issues", nargs="*", type=date.fromisoformat)
    args = parser.parse_args()
    now = datetime.now(timezone(timedelta(hours=8))).date()
    dates = args.issues or [now, now - timedelta(days=1)]
    try:
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            result = collect(Path("/data/output"), dates, verify_controls=args.verify_controls)
    except Exception as error:
        print(json.dumps({"status": "epaper_failed", "error_type": type(error).__name__}))
        return 1
    print(json.dumps(result))
    return int(result["status"] != "ok")


if __name__ == "__main__":
    raise SystemExit(main())
