"""Capture the co-issuing ministry's complete copy of the 2026 AI ethics rules."""

from contextlib import redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import re
import sys

import httpx

from run import load_manifest, process_recipe, save_manifest
from scraper.adapters._common import AdapterError, clean_tree, complete_body_paragraphs, first_text, parse_date_zh
from scraper.fetcher import Fetcher
from scraper.registry import load_registry


RECIPE = "moa_ai_ethics"
POLICY = "人工智能科技伦理审查与服务办法（试行）"
NUMBER = "工信部联科〔2026〕75号"
APPENDIX = "需要开展科技伦理专家复核的人工智能科技活动清单"
LAST = "本清单将根据工作需要动态调整。"
DIGITS = "一二三四五六七八九"
ARTICLES = [*DIGITS, "十", *("十" + digit for digit in DIGITS),
            "二十", *("二十" + digit for digit in DIGITS),
            "三十", *("三十" + digit for digit in DIGITS[:7])]


def parse(html, entry, url):
    tree = clean_tree(html)
    title = first_text(tree, "h1")
    body = complete_body_paragraphs(tree, "div.TRS_Editor")
    date_match = re.search(r"日期[：:]\s*(\d{4}-\d{2}-\d{2})\s*作者[：:]", tree.body.text(separator=" ", strip=True))
    date = parse_date_zh(date_match[1]) if date_match else None
    policy_order = [match[1] for paragraph in body if (match := re.match(r"第([一二三四五六七八九十]+)条", paragraph))]
    if (url != entry["list_url"] or not title or "人工智能科技伦理审查与服务办法" not in title
            or date != "2026-05-14" or NUMBER not in body or POLICY not in body
            or policy_order != ARTICLES or APPENDIX not in body or not body or body[-1] != LAST):
        raise AdapterError("Official AI ethics copy failed a publication or completeness check")
    return {"title": title, "date": date, "document_number": NUMBER, "body_paragraphs": body}


def capture(data):
    entry = next(item for item in load_registry() if item["id"] == RECIPE)
    output = data / "output"
    path = output / "manifest.json"
    manifest = load_manifest(path)
    fetcher = Fetcher(cache_dir=data / "cache", transport=httpx.HTTPTransport(trust_env=False),
                      strict_html_hosts={host: {} for host in entry["allowed_domains"]})
    try:
        report = process_recipe(entry, fetcher, manifest, cap=1, incremental=True,
                                out_dir=output / "_normalized", article_parser=parse)
        save_manifest(manifest, path)
    finally:
        fetcher.close()
    return {**report, "error": bool(report["error"]), "robots_blocked": fetcher.stats["robots_blocked"]}


def main():
    try:
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            result = capture(Path(sys.argv[1]))
    except Exception as error:
        print(json.dumps({"status": "policy_copy_failed", "error_type": type(error).__name__}))
        return 1
    print(json.dumps(result))
    return int(bool(result["error"] or result["fetch_failures"] or result["parse_failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
