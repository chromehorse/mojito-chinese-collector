"""Use People's Daily's own e-paper for the blocked commentary indexes."""
import argparse
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
import json
import os
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

import httpx

from run import load_manifest, process_recipe, save_manifest
from scraper.adapters._common import AdapterError, clean_tree, parse_article, parse_date_zh
from scraper.fetcher import Fetcher
from scraper.normalize import PEN_NAMES, is_personal_name


HOST = "paper.people.com.cn"
SECTIONS = ("评论", "理论", "国际", "要闻", "政治", "科技", "经济", "财经")
COLUMNS = ("人民时评", "人民论坛", "人民观点", "评论员观察", "钟声", "纵横", "现场评论", "微观",
           "连线评论员", "法治聚焦", "人民锐评", "政策解读")
AI_TITLES = ("人工智能", "大模型", "智能体", "算力", "AI")
REPORTER_CREDIT = re.compile(r"(本报记者|记者)\s*(.+)")
REPORTER_DATELINE = re.compile(r"^本报[\u3400-\u9fff]{1,16}\d{1,2}月\d{1,2}日(?:电|讯)\s*[（(](记者[^）)]+)[）)]")
CONTROLS = {
    "2026-01-05": "content_30129344.html",
    "2026-01-09": "content_30130819.html",
    "2026-11-17": "content_30191569.html",
    "2026-09-16": "content_30181273.html",
}
CONTROL_TEXT = {
    "content_30129344.html": (("第二十条规定",), "既有力度又有温度。"),
    "content_30130819.html": (("彭飞：", "曾毅："), "以免把人类社会引向万劫不复的危险境地。"),
}
CONTROL_FAILURES = {"control_markers", "control_ending_last_block", "control_ending_before_tail",
                    "control_ending_missing", "control_byline"}
PRINTED_REPORTERS = {
    "/rmrb/pc/content/202601/05/content_30129344.html": ("加强安全监管，促进人工智能健康发展", "金歆"),
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


class PrintFetcher(Fetcher):
    """Prefer strict UTF-8 for this publisher's print editions."""

    def decode(self, body, declared_charset):
        try:
            return body.decode("utf-8-sig"), "utf-8"
        except UnicodeDecodeError:
            return super().decode(body, declared_charset)


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
        return [], {landing.skip_reason or "issue-unavailable": 1}
    key = f"{edition:%Y%m}/{edition:%d}"
    section_pattern = rf"/rmrb/pc/layout/{key}/node_\d+\.html"
    article_pattern = rf"/rmrb/pc/content/{key}/content_\d+\.html"
    if not any(local_links(landing.text, issue, section_pattern, ("版",))):
        return [], {"issue-sections-missing": 1}
    sections = dict.fromkeys(local_links(landing.text, issue, section_pattern, SECTIONS))
    terms = COLUMNS + AI_TITLES
    articles = list(local_links(landing.text, issue, article_pattern, terms))
    failures = Counter()
    for section in (url for url in sections if url != issue):
        content = fetcher.fetch(section, [HOST], max_age_days=1)
        if content.ok:
            if not any(local_links(content.text, section, section_pattern, ("版",))):
                failures["dated-section-links-missing"] += 1
                continue
            articles.extend(local_links(content.text, section, article_pattern, terms))
        else:
            failures[content.skip_reason or "section-unavailable"] += 1
            if HOST in fetcher._stopped_hosts:
                break
    return list(dict.fromkeys(articles)), failures


def reporter_credit(line):
    match = REPORTER_CREDIT.fullmatch(line)
    if not match:
        return None
    names = []
    for field in re.split(r"[、，,]", match[2]):
        parts = [re.sub(r"\s+", "", part) for part in re.split(r"\s{2,}", field.strip())]
        for padded, group in groupby(parts, key=lambda name: len(name) == 1):
            if padded:
                # The print template pads two-character names, as in 王  政.
                name = "".join(group)
                if len(name) != 2:
                    return None
                names.append(name)
            else:
                names.extend(group)
    if all(is_personal_name(name) for name in names):
        return match[1] + "、".join(names)
    return None


def credit_names(credit):
    return re.sub(r"[\s、，,]+", "", credit or "").removeprefix("本报记者").removeprefix("记者")


def printed_credit(text, published):
    credits = set()
    for stamp in clean_tree(text).css("p.sec > span.date"):
        printed_date = stamp.text(separator="", strip=False).strip()
        publication = re.sub(r"\s+", "", printed_date)
        if not publication.startswith("《人民日报》") or parse_date_zh(publication) != published:
            continue
        header = stamp.parent.text(separator="", strip=False).strip()
        if not header.endswith(printed_date):
            continue
        label = header.removesuffix(printed_date).strip()
        if reporter := reporter_credit(label):
            credits.add(reporter)
        elif (name := re.sub(r"\s+", "", label)) and (is_personal_name(name) or name in PEN_NAMES):
            credits.add(name)
    if len(credits) > 1:
        raise AdapterError("Conflicting printed e-paper credits")
    return next(iter(credits), None)


def parse_epaper(text, entry, url):
    parsed = parse_article(text, entry, url)
    path = urlsplit(url).path
    edition = re.fullmatch(r"/rmrb/pc/content/(\d{4})(\d{2})/(\d{2})/content_\d+\.html", path)
    if not edition or parsed["date"] != "-".join(edition.groups()):
        raise AdapterError("E-paper article date differs from the requested edition")
    credit = printed_credit(text, parsed["date"])
    dateline = REPORTER_DATELINE.match(parsed["body_paragraphs"][0])
    if dateline and (reporter := reporter_credit(dateline[1])):
        if credit and credit_names(credit) != credit_names(reporter):
            raise AdapterError("Conflicting printed e-paper credits")
        if not (credit and REPORTER_CREDIT.fullmatch(credit)):
            credit = reporter
    if credit:
        existing = credit_names(parsed["author"])
        if existing and existing != credit_names(credit):
            raise AdapterError("Conflicting printed e-paper credits")
        parsed["author"] = credit
    if control := CONTROL_TEXT.get(path.rsplit("/", 1)[-1]):
        markers, ending = control
        body = parsed["body_paragraphs"]
        if not all(any(marker in paragraph for paragraph in body) for marker in markers):
            raise AdapterError("control_markers")
        compact_body = [re.sub(r"\s+", "", paragraph) for paragraph in body]
        if not compact_body[-1].endswith(ending):
            if ending in compact_body[-1]:
                raise AdapterError("control_ending_last_block")
            if any(ending in paragraph for paragraph in compact_body[:-1]):
                raise AdapterError("control_ending_before_tail")
            raise AdapterError("control_ending_missing")
    if not parsed["author"] and "（钟声）" in parsed["title"]:
        parsed["author"] = "钟声"
    if printed := PRINTED_REPORTERS.get(path):
        title, reporter = printed
        actual = credit_names(parsed["author"])
        if title not in parsed["title"] or actual not in {"", reporter}:
            raise AdapterError("control_byline")
        # The publisher's rendered print header displays this credit separately from the article body.
        parsed["author"] = "本报记者" + reporter
    return parsed


def parse_collection_article(text, entry, url):
    article = parse_epaper(text, entry, url)
    if reporter_credit(article["author"] or ""):
        # This is the single-article entry created for process_recipe below.
        entry["source_genre_default"] = "news"
    return article


def collect(output, dates, *, verify_controls, cap=4, control_articles_only=False):
    totals = {"status": "ok", "issues": len(dates), "discovered": 0, "parsed": 0,
              "written": 0, "confirmed_controls": 0, "parse_failures": 0, "fetch_failures": 0,
              "issues_checked": 0, "issues_incomplete": 0, "issues_without_candidates": 0,
              "layout_failures": 0, "capped_candidates": 0}
    layout_failures = Counter()
    controls = []
    fetcher = PrintFetcher(cache_dir=output.parent / "cache",
                           transport=httpx.HTTPTransport(trust_env=False), strict_html_hosts={HOST: {}})
    manifest_path = output / "manifest.json"
    manifest = load_manifest(manifest_path)
    try:
        for edition in dates:
            parsed_before = totals["parsed"]
            control = CONTROLS.get(edition.isoformat()) if verify_controls else None
            if control_articles_only:
                candidates = [f"https://{HOST}/rmrb/pc/content/{edition:%Y%m}/{edition:%d}/{control}"]
                failures = {}
            else:
                candidates, failures = articles_from_edition(edition, fetcher)
            totals["issues_checked"] += 1
            totals["issues_incomplete"] += bool(failures)
            layout_failures.update(failures)
            totals["layout_failures"] += sum(failures.values())
            totals["issues_without_candidates"] += not failures and not candidates
            totals["discovered"] += len(candidates)
            if control:
                candidates = [url for url in candidates if url.endswith("/" + control)]
                totals["confirmed_controls"] += bool(candidates)
            selected = candidates[:cap]
            totals["capped_candidates"] += len(candidates) - len(selected)
            for url in selected:
                if HOST in fetcher._stopped_hosts:
                    break
                entry = {**ENTRY, "list_url": url}
                report = process_recipe(entry, fetcher, manifest, cap=1, incremental=True,
                                        out_dir=output / "_normalized", article_parser=parse_collection_article)
                for key in ("parsed", "written", "parse_failures", "fetch_failures"):
                    totals[key] += report[key]
                if control:
                    outcome = "written" if report["written"] else "no_result"
                    if report["parse_failures"]:
                        detail = manifest["failures"][-1]["detail"]
                        outcome = detail if detail in CONTROL_FAILURES else "article_parse"
                    elif report["fetch_failures"]:
                        outcome = "article_fetch"
                    controls.append(outcome)
            if totals["parsed"] > parsed_before:
                save_manifest(manifest, manifest_path)
            if HOST in fetcher._stopped_hosts:
                break
    finally:
        fetcher.close()
    totals["issues_unattempted"] = len(dates) - totals["issues_checked"]
    if any(totals[key] for key in ("layout_failures", "issues_unattempted", "fetch_failures", "parse_failures")):
        totals["status"] = "partial"
    if layout_failures:
        totals["layout_failure_reasons"] = dict(sorted(layout_failures.items()))
    if verify_controls and (totals["confirmed_controls"] != len(dates) or totals["written"] != len(dates)):
        totals["status"] = "control_failed"
    if verify_controls:
        totals["control_outcomes"] = controls
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-controls", action="store_true")
    parser.add_argument("--control-articles-only", action="store_true")
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--first", type=date.fromisoformat)
    parser.add_argument("--last", type=date.fromisoformat)
    parser.add_argument("issues", nargs="*", type=date.fromisoformat)
    args = parser.parse_args()
    if args.control_articles_only and not args.verify_controls:
        parser.error("Use article-only verification with registered controls.")
    now = datetime.now(timezone(timedelta(hours=8))).date()
    if args.first or args.last:
        if (args.issues or args.verify_controls or not args.first or not args.last
                or not 0 <= (args.last - args.first).days < 366 or args.last > now):
            parser.error("Specify one dated range of up to 366 days, without individual issues or controls.")
        dates = [args.first + timedelta(days=offset) for offset in range((args.last - args.first).days + 1)]
        cap = None
    else:
        dates = args.issues or [now, now - timedelta(days=1)]
        if args.verify_controls and any(d.isoformat() not in CONTROLS for d in dates):
            parser.error("Each control requires a registered edition date.")
        cap = 4
    try:
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            result = collect(args.data / "output", dates, verify_controls=args.verify_controls, cap=cap,
                             control_articles_only=args.control_articles_only)
    except Exception as error:
        print(json.dumps({"status": "epaper_failed", "error_type": type(error).__name__}))
        return 1
    print(json.dumps(result))
    return int(result["status"] != "ok")


if __name__ == "__main__":
    raise SystemExit(main())
