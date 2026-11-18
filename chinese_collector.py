"""Package the Chinese scraper and run its public-text intake on a separate host."""

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
from urllib.parse import parse_qs, quote, urljoin, urlsplit, urlunsplit


MIIT_ATTACHMENT_PATH = re.compile(r"/cms_files/filemanager/\d+/attach/\d+/[0-9a-f]+\.pdf", re.I)


class _MiitPdfViewer(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attributes):
        if tag != "iframe":
            return
        attrs = dict(attributes)
        viewer = urlsplit(attrs.get("src") or "")
        file_path = attrs.get("fileurl") or ""
        if (not viewer.scheme and not viewer.netloc
                and viewer.path == "/cms_files/filemanager/script/pdfjs/web/viewer.html"
                and parse_qs(viewer.query).get("file") == [file_path]
                and MIIT_ATTACHMENT_PATH.fullmatch(file_path)):
            self.paths.append(file_path)


def miit_pdf_links(html, url):
    """Read exact attachment references from the ministry's HTML viewer markup."""
    if urlsplit(url).netloc != "www.miit.gov.cn":
        return []
    parser = _MiitPdfViewer()
    parser.feed(html)
    parser.close()
    return list(dict.fromkeys(urljoin(url, path) for path in parser.paths))


def parse_miit_article(text, entry, url):
    from scraper.adapters import policy_api

    article = policy_api.parse(text, entry, url)
    existing = {item["url"] for item in article["attachments"]}
    article["attachments"].extend({"url": link, "content_type": "linked/pdf"}
                                  for link in miit_pdf_links(text, url) if link not in existing)
    return article


def nda_attachment_announcement(title, paragraphs):
    """Identify NDA notices whose policy is referenced in a separate attachment."""
    return (title.endswith("》印发") and paragraphs[-1] in {"附件：", "附件:"}
            and not any(re.match(r"[一二三四五六七八九十]+、", paragraph) for paragraph in paragraphs))


def package(scraper, destination):
    scraper = scraper.resolve(strict=True)
    destination = destination.resolve()
    paths = [Path(name) for name in ("run.py", "requirements.txt", "_sources.json")]
    for directory in ("scraper", "scraper/adapters"):
        paths.extend(path.relative_to(scraper) for path in sorted((scraper / directory).glob("*.py")))
    files = {path: (scraper / path).resolve(strict=True) for path in paths}
    if any(not src.is_file() or not src.is_relative_to(scraper) for src in files.values()):
        raise ValueError("The collector package must contain only local scraper code and configuration.")
    destination.mkdir(parents=True, exist_ok=False)
    for path, src in files.items():
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
    shutil.copyfile(__file__, destination / "chinese_collector.py")


@contextmanager
def collection_lock(data):
    data.mkdir(parents=True, exist_ok=True)
    with (data / "collector.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def https_url(url):
    parts = urlsplit(url)
    return urlunsplit(parts._replace(scheme="https")) if parts.scheme == "http" else url


def publisher_options(entries, max_pages):
    hosts = {}
    for entry in entries:
        recipe = entry["recipe"]
        for host in entry["allowed_domains"]:
            hosts.setdefault(host, {})
        if strict := recipe.get("strict_html"):
            hosts[entry["allowed_domains"][0]].update(strict)
        if entry["kind"] == "policy-api":
            api = recipe["api"]
            start = int(api.get("page_start", 1))
            count = min(int(api.get("max_pages", 3)), max_pages)
            for page in range(start, start + count):
                url = api["endpoint"].replace("{q}", quote(api.get("q", ""), safe="")).replace("{page}", str(page))
                host = urlsplit(url).hostname
                if host in entry["allowed_domains"]:
                    hosts[host].setdefault("json_urls", set()).add(url)
    return hosts


def historical_edition(entry, name):
    """Use an exact registered document when a rolling index no longer lists it."""
    from scraper.adapters._common import (AdapterError, clean_tree, collect_attachment_links,
                                          complete_body_paragraphs, first_text, parse_date_zh)

    edition = entry["recipe"].get("historical_editions", {}).get(name)
    if not edition:
        raise ValueError("Unknown historical edition for the selected publisher.")
    url = edition["url"]
    origin = urlsplit(url)
    if (origin.scheme != "https" or origin.netloc != origin.hostname
            or origin.hostname not in entry["allowed_domains"] or origin.fragment):
        raise ValueError("Historical edition must use an exact registered HTTPS origin.")
    recipe = {"self_article": True}
    if "strict_html" in entry["recipe"]:
        recipe["strict_html"] = entry["recipe"]["strict_html"]
    pinned = {**entry, "kind": "listing", "list_url": url, "backfill_cap": 1,
              "allowed_domains": [origin.hostname], "recipe": recipe}

    def parse(html, source, requested):
        tree = clean_tree(html)
        selectors = edition["article"]
        title = first_text(tree, selectors["title"])
        body = complete_body_paragraphs(tree, selectors["body"])
        visible = tree.body.text(separator=" ", strip=True)
        if pattern := selectors.get("date_label_pattern"):
            dates = {parse_date_zh(value) for value in re.findall(pattern, visible)}
            published = dates.pop() if len(dates) == 1 else None
        else:
            published = parse_date_zh(first_text(tree, selectors["date"]))
        if (requested != url or published != edition["published"] or not title or not body
                or edition["title_text"] not in title
                or any(not any(marker in paragraph for paragraph in body) for marker in edition["body_markers"])
                or not body[-1].endswith(edition["last_text"])):
            raise AdapterError("Historical edition failed its publisher, date or content checks.")
        if numbering := edition.get("numbered_articles"):
            numbers = re.findall(r"^第([一二三四五六七八九十]+)条", "\n".join(body), re.M)
            digits = "一二三四五六七八九"
            labels = [*digits, "十", *("十" + digit for digit in digits), "二十",
                      *("二十" + digit for digit in digits), "三十", *("三十" + digit for digit in digits)]
            if not 0 < numbering <= len(labels) or numbers != labels[:numbering]:
                raise AdapterError("Historical edition has missing or out-of-order articles.")
        document_number = edition.get("document_number")
        if document_number and document_number not in visible:
            raise AdapterError("Historical edition does not carry its expected document number.")
        return {"title": title, "date": published, "document_number": document_number, "body_paragraphs": body,
                "attachments": collect_attachment_links(tree, requested, source["allowed_domains"])}

    return pinned, parse


def collect(data, recipe_ids=(), cap=None, edition=None):
    import httpx
    import run as scraper_run
    from scraper.adapters import policy_api
    from scraper.fetcher import Fetcher, FetchResult
    from scraper.registry import load_registry

    registry = load_registry()
    unknown = set(recipe_ids) - {entry["id"] for entry in registry}
    if unknown:
        raise ValueError(f"Unknown collector recipes: {', '.join(sorted(unknown))}.")
    selected = [entry for entry in registry if not recipe_ids or entry["id"] in recipe_ids]
    article_parsers = {"miit_kjs": parse_miit_article}
    if edition:
        if len(recipe_ids) != 1 or len(selected) != 1:
            raise ValueError("A historical edition requires exactly one publisher recipe.")
        pinned, parse_edition = historical_edition(selected[0], edition)
        article_parsers[pinned["id"]] = parse_edition
        entries, deferred = [pinned], {}
    else:
        entries = [entry for entry in selected if entry["status"] == "active"]
        deferred = {entry["id"]: entry["status"] for entry in selected if entry["status"] != "active"}
    for entry in entries:
        entry["list_url"] = https_url(entry["list_url"])
        recipe = entry["recipe"]
        if recipe.get("discover_url"):
            recipe["discover_url"] = https_url(recipe["discover_url"])

    class PublicTextFetcher(Fetcher):
        def fetch(self, url, allowed_domains, **options):
            parts = urlsplit(url)
            if parts.scheme != "https" or parts.netloc != parts.hostname or parts.hostname not in allowed_domains:
                self._warn("Collector rejected a URL outside its exact HTTPS publisher origins.")
                return FetchResult(url=url, skip_reason="strict-https-origin-required")
            return super().fetch(url, allowed_domains, **options)

    output = data / "output"
    manifest_path = output / "manifest.json"
    with collection_lock(data):
        manifest = scraper_run.load_manifest(manifest_path)
        fetcher = PublicTextFetcher(transport=httpx.HTTPTransport(trust_env=False), cache_dir=data / "cache",
                                    strict_html_hosts=publisher_options(entries, policy_api.MAX_PAGES_HARD))
        try:
            summaries = [scraper_run.process_recipe(entry, fetcher, manifest, cap=cap, incremental=True,
                                                    out_dir=output / "_normalized",
                                                    article_parser=article_parsers.get(entry["id"]))
                         for entry in entries]
        finally:
            fetcher.close()
        scraper_run.save_manifest(manifest, manifest_path)
        report = {"summaries": summaries, "deferred": deferred, "fetcher_stats": fetcher.stats,
                  "warnings": fetcher.warnings}
        (output / "run_stats.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def failed_report(report):
    return not report["summaries"] or any(
        row["error"] or row["fetch_failures"] or row["parse_failures"] for row in report["summaries"]
    )


def print_collection_summary(data, recipe_ids, cap, edition=None):
    """Keep publisher output in the collector's private working directory."""
    try:
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            report = collect(data.resolve(), recipe_ids, cap, edition)
    except Exception:
        # The public entry point must not put publisher strings or local paths in a traceback.
        print(json.dumps({"status": "error"}))
        return 1
    failed = failed_report(report)
    counts = ("discovered", "parsed", "written", "fetch_failures", "parse_failures")
    print(json.dumps({
        "status": "error" if failed else "ok",
        "recipes": len(report["summaries"]),
        "deferred": len(report["deferred"]),
        "recipe_errors": sum(bool(row["error"]) for row in report["summaries"]),
        **{key: sum(row[key] for row in report["summaries"]) for key in counts},
    }))
    return int(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pack = commands.add_parser("package")
    pack.add_argument("scraper", type=Path)
    pack.add_argument("destination", type=Path)
    fetch = commands.add_parser("collect")
    fetch.add_argument("--data", type=Path, required=True)
    fetch.add_argument("--recipe", action="append", default=[])
    fetch.add_argument("--edition", help="exact historical edition declared by the selected recipe")
    fetch.add_argument("--cap", type=int)
    fetch.add_argument("--summary-only", action="store_true",
                       help="print only aggregate counts and a fixed status label")
    args = parser.parse_args()
    try:
        if args.command == "package":
            package(args.scraper, args.destination)
            return 0
        if args.cap is not None and args.cap < 1:
            parser.error("--cap must be positive")
        if args.summary_only:
            return print_collection_summary(args.data, args.recipe, args.cap, args.edition)
        report = collect(args.data.resolve(), args.recipe, args.cap, args.edition)
        print(json.dumps(report, ensure_ascii=False))
        return int(failed_report(report))
    except (OSError, ValueError) as error:
        parser.exit(1, f"Collector: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
