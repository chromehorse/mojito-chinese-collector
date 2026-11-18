"""Run bounded historical batches through Mojito's regular Chinese scraper."""

import argparse
from collections import Counter
from contextlib import nullcontext, redirect_stderr
from datetime import date
import json
import os
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

if __package__ == "tools":
    from tools.chinese_collector import collection_lock
else:
    from chinese_collector import collection_lock


def qiushi_issues(entry, fetcher, year):
    from scraper.adapters._common import AdapterError, clean_tree

    def links(url, selector, pattern):
        response = fetcher.fetch(url, entry["allowed_domains"], max_age_days=entry["cadence_days"])
        if not response.ok:
            raise AdapterError(f"Qiushi archive request failed ({response.skip_reason or response.status})")
        for anchor in clean_tree(response.text).css(selector):
            href = anchor.attributes.get("href")
            if not href:
                continue
            target = urljoin(url, href)
            parts = urlsplit(target)
            if (parts.scheme == "https" and parts.netloc in entry["allowed_domains"]
                    and not parts.query and not parts.fragment and re.search(pattern, parts.path)):
                yield re.sub(r"\s+", "", anchor.text(strip=True)), target

    recipe = entry["recipe"]
    years = {url for label, url in links(entry["list_url"], recipe["year_link_selector"],
                                        recipe["year_url_pattern"]) if label == f"{year}年"}
    if len(years) != 1:
        raise AdapterError(f"Qiushi archive must have exactly one index labeled {year}年")
    issues = {}
    for label, url in links(years.pop(), recipe["issue_link_selector"], recipe["issue_url_pattern"]):
        if match := re.fullmatch(rf"《求是》{year}年第(\d{{1,2}})期", label):
            number = int(match[1])
            if number in issues and issues[number] != url:
                raise AdapterError(f"Qiushi archive has two links for issue {number}")
            issues[number] = url
    if not issues:
        raise AdapterError("Qiushi archive returned no labeled issues")
    return issues


def collect(data, recipe_id, year, first_issue, last_issue, local_manifest, cap=None):
    import httpx
    from run import load_manifest, process_recipe, save_manifest
    from scraper.adapters import issue_archive, listing
    from scraper.adapters._common import AdapterError
    from scraper.fetcher import Fetcher
    from scraper.registry import load_registry

    entry = next(item for item in load_registry() if item["id"] == recipe_id and item["status"] == "active")
    output = data / "output"
    target = output / "manifest.json"
    known = load_manifest(local_manifest) if local_manifest else {"records": {}, "skipped": {}}
    strict_hosts = {host: entry["recipe"].get("strict_html", {})
                    for host in entry["allowed_domains"]}
    issue_numbers = []
    fetcher = Fetcher(cache_dir=data / "cache", strict_html_hosts=strict_hosts,
                      transport=httpx.HTTPTransport(trust_env=False))
    summaries = []
    outside_year = []
    try:
        with collection_lock(data):
            batch = load_manifest(target)
            manifest = {
                **batch,
                "records": {**known["records"], **batch["records"]},
                "skipped": {**known["skipped"], **batch["skipped"]},
            }
            if recipe_id == "qiushi":
                issues = qiushi_issues(entry, fetcher, year)
                issue_numbers = list(range(first_issue, (last_issue or max(issues)) + 1))
                if not issue_numbers or set(issue_numbers).difference(issues):
                    raise AdapterError("Qiushi archive is missing requested issues")
                batches = [{**entry, "kind": "listing", "list_url": issues[number]}
                           for number in issue_numbers]
                parse = issue_archive.parse
            else:
                batches = [entry]
                parse = listing.parse

            def parse_dated(html, recipe, url):
                article = parse(html, recipe, url)
                try:
                    published = date.fromisoformat(article["date"] or "")
                except ValueError:
                    raise AdapterError("Historical document has no valid publication date") from None
                if published.year != year or published > date.today():
                    outside_year.append(url)
                    # Keep this response outside the requested edition. Do not mark it irrelevant.
                    raise AdapterError("Publication date outside the requested year")
                if (recipe_id == "nda_ai_policy" and article["title"].endswith("》印发")
                        and article["body_paragraphs"][-1] in {"附件：", "附件:"}
                        and not any(re.match(r"[一二三四五六七八九十]+、", paragraph)
                                    for paragraph in article["body_paragraphs"])):
                    raise AdapterError("Policy announcement links to a separate edition")
                return article

            for number, source in enumerate(batches):
                before = len(outside_year)
                summary = process_recipe(source, fetcher, manifest, cap=cap, incremental=True,
                                         out_dir=output / "_normalized", article_parser=parse_dated)
                summary["outside_year"] = len(outside_year) - before
                summary["parse_failures"] -= summary["outside_year"]
                if issue_numbers:
                    summary["issue"] = issue_numbers[number]
                manifest["failures"] = [item for item in manifest["failures"]
                                        if item["detail"] != "Publication date outside the requested year"]
                for key in ("records", "skipped"):
                    batch[key].update({k: v for k, v in manifest[key].items() if k not in known[key]})
                batch["failures"] = manifest["failures"]
                save_manifest(batch, target)
                summaries.append(summary)
                print(json.dumps({**summary, "error": bool(summary["error"])}, ensure_ascii=False), flush=True)
                if any(reason.startswith("host-paused-http-") for reason in fetcher._stopped_hosts.values()):
                    break
    finally:
        fetcher.close()
    months = Counter(record["published"][:7] for record in batch["records"].values()
                     if record["source"] == recipe_id and (record.get("published") or "")[:4] == str(year))
    failed = (len(summaries) != len(batches) or any(row["error"] or row["fetch_failures"]
                                                  or row["parse_failures"] for row in summaries))
    print(json.dumps({"status": "partial" if failed else "ok", "recipe": recipe_id,
                      "staged_by_month": dict(sorted(months.items()))}), flush=True)
    return int(failed)


def main():
    from scraper.adapters._common import AdapterError

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--known-manifest", type=Path)
    parser.add_argument("--recipe", choices=("qiushi", "nda_ai_policy"), required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--first-issue", type=int, default=1)
    parser.add_argument("--last-issue", type=int)
    parser.add_argument("--cap", type=int, help="maximum article candidates per issue or listing for a live probe")
    parser.add_argument("--summary-only", action="store_true", help="omit publisher warnings from public CI logs")
    args = parser.parse_args()
    if args.known_manifest and (not args.known_manifest.is_file()
                                or args.known_manifest.resolve() == (args.data / "output/manifest.json").resolve()):
        parser.error("The known manifest must be an existing file outside the historical batch output.")
    if (not 1900 <= args.year <= date.today().year or not 1 <= args.first_issue <= 30
            or (args.last_issue is not None and not args.first_issue <= args.last_issue <= 30)):
        parser.error("Invalid year or issue range.")
    if args.recipe != "qiushi" and (args.first_issue != 1 or args.last_issue is not None):
        parser.error("Issue ranges are available for Qiushi.")
    if args.cap is not None and args.cap < 1:
        parser.error("The article cap must be positive.")
    try:
        with open(os.devnull, "w") if args.summary_only else nullcontext() as sink:
            with redirect_stderr(sink) if sink else nullcontext():
                return collect(args.data, args.recipe, args.year, args.first_issue, args.last_issue,
                               args.known_manifest, args.cap)
    except (AdapterError, OSError, ValueError) as error:
        if args.summary_only:
            print(json.dumps({"status": "historical_capture_failed", "error_type": type(error).__name__}))
            return 1
        parser.exit(1, f"Historical capture: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
