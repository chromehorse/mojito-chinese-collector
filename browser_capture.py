"""Off-device renderer for the registry's explicit browser routes."""
from contextlib import redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

import httpx

from run import load_manifest, save_manifest
from scraper.adapters._common import AdapterError, clean_tree, extract_links, parse_article
from scraper.config import MAX_RESPONSE_BYTES, USER_AGENT
from scraper.fetcher import Fetcher, utcnow
from scraper.normalize import build_record, to_markdown
from scraper.registry import load_registry
from scraper.relevance import is_relevant


RESOURCE_TYPES = {
    "document": {"text/html", "application/xhtml+xml"},
    "script": {"text/javascript", "application/javascript"},
    "stylesheet": {"text/css"},
    "xhr": {"application/json", "text/html", "text/plain"},
    "fetch": {"application/json", "text/html", "text/plain"},
}
ARTICLE_PATH = r"/portal/journal/portal/client/paper/[0-9a-f]+(?:[?#].*)?$"
GOV_LATEST = "https://www.gov.cn/zhengce/zuixin/"
AI_POLICY_TERMS = ("人工智能", "大模型", "智能体", "具身智能", "智能算力", "算法推荐", "深度合成")
MAX_BROWSER_REQUESTS = 72
MAX_BROWSER_BYTES = 25 * 1024 * 1024


def exact_origin(url, domains):
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.netloc == parts.hostname and parts.hostname in domains


def ai_policy_relevant(title, paragraphs):
    if any(term in title for term in AI_POLICY_TERMS):
        return True
    matching = [p for p in paragraphs if any(term in p for term in AI_POLICY_TERMS)]
    return len(matching) >= 2 or any(len(p) >= 100 for p in matching)


def capture(entry, output):
    from playwright.sync_api import Error, sync_playwright

    recipe = entry["recipe"]
    domains = set(entry["allowed_domains"])
    listing = GOV_LATEST if entry["id"] == "gov_policy_db" else recipe.get("rung_a_endpoint", entry["list_url"])
    summary = {"status": "ok", "recipe": entry["id"], "discovered": 0, "inline_papers": 0,
               "parsed": 0, "written": 0, "abstracts": 0, "blocked_requests": 0,
               "blocked_http": 0, "parse_failures": 0}
    policy = Fetcher(cache_dir=output.parent / "cache",
                     transport=httpx.HTTPTransport(trust_env=False),
                     strict_html_hosts={host: {} for host in domains})
    try:
        initial = policy.fetch(listing, domains)
        if not initial.ok:
            return {**summary, "status": "listing_denied", "listing_http": initial.status or 0,
                    "robots_blocked": policy.stats["robots_blocked"]}
        blocked_hosts, requests, received = set(), 0, 0
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=12,
                          headers={"User-Agent": USER_AGENT}) as client, sync_playwright() as playwright:
            browser = playwright.chromium.launch(chromium_sandbox=True, channel=os.environ.get("COLLECTOR_BROWSER"),
                                                 args=["--disable-dev-shm-usage", "--disable-background-networking"])
            context = browser.new_context(accept_downloads=False, service_workers="block", user_agent=USER_AGENT)
            context.route_web_socket("**/*", lambda ws: ws.close())

            def guard(route):
                nonlocal requests, received
                request = route.request
                url = request.url
                host = urlsplit(url).hostname
                allowed = RESOURCE_TYPES.get(request.resource_type, ())
                if (request.method != "GET" or not allowed or not exact_origin(url, domains)
                        or host in blocked_hosts or requests >= MAX_BROWSER_REQUESTS
                        or received >= MAX_BROWSER_BYTES or not policy._robots_allows(url)):
                    summary["blocked_requests"] += 1
                    return route.abort()
                if url == listing and request.resource_type == "document":
                    return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=initial.text)
                if request.resource_type in {"document", "fetch", "xhr"}:
                    policy._polite_wait(url)
                requests += 1
                try:
                    with client.stream("GET", url) as response:
                        if response.status_code in {401, 403, 429}:
                            blocked_hosts.add(host)
                            summary["blocked_http"] += 1
                        mime = response.headers.get("content-type", "").partition(";")[0].strip().lower()
                        if response.status_code != 200 or mime not in allowed:
                            summary["blocked_requests"] += 1
                            return route.abort()
                        body = bytearray()
                        for chunk in response.iter_bytes(chunk_size=64 * 1024):
                            received += len(chunk)
                            body.extend(chunk)
                            if len(body) > MAX_RESPONSE_BYTES or received > MAX_BROWSER_BYTES:
                                summary["blocked_requests"] += 1
                                return route.abort()
                    if request.resource_type == "document":
                        decoded, _ = policy.decode(bytes(body), response.encoding)
                        if decoded is None:
                            summary["blocked_requests"] += 1
                            return route.abort()
                        return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=decoded)
                    return route.fulfill(status=200, content_type=mime, body=bytes(body))
                except httpx.HTTPError:
                    summary["blocked_requests"] += 1
                    return route.abort()

            context.route("**/*", guard)
            page = context.new_page()
            page.set_default_navigation_timeout(35_000)
            page.goto(listing, wait_until="domcontentloaded")
            try:
                page.locator("div.paperBox, a[href*='content_']").first.wait_for(timeout=8_000)
            except Error:
                pass
            listing_html = page.content()
            if len(listing_html.encode()) > MAX_RESPONSE_BYTES:
                return {**summary, "status": "listing_oversize"}
            summary["inline_papers"] = len(clean_tree(listing_html).css("div.paperBox"))
            pattern = recipe.get("article_url_pattern") or ARTICLE_PATH
            urls = [url for url in extract_links(listing_html, listing, "a", domains, pattern)
                    if exact_origin(url, domains)][:2]
            summary["discovered"] = len(urls)
            manifest = load_manifest(output / "manifest.json")
            for url in urls:
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    html = page.content()
                    if page.url != url or len(html.encode()) > MAX_RESPONSE_BYTES:
                        raise AdapterError("Unexpected article navigation")
                    parsed = parse_article(html, entry, url)
                    if not parsed["date"]:
                        raise AdapterError("Missing publication date")
                except (AdapterError, Error):
                    summary["parse_failures"] += 1
                    continue
                summary["parsed"] += 1
                relevant = (ai_policy_relevant(parsed["title"], parsed["body_paragraphs"])
                            if entry["id"] == "gov_policy_db" else
                            is_relevant(parsed["title"], "\n".join(parsed["body_paragraphs"])))
                if not relevant:
                    continue
                complete = not recipe.get("abstract_only", False)
                row, paragraphs = build_record(parsed, entry, url, utcnow().isoformat(), body_complete=complete)
                record_id = row["record_id"]
                normalized = output / "_normalized"
                normalized.mkdir(parents=True, exist_ok=True)
                (normalized / f"{record_id}.md").write_text(to_markdown(row, paragraphs), encoding="utf-8")
                manifest["records"][record_id] = {k: row[k] for k in (
                    "source", "canonical_url", "title_zh", "published", "content_hash", "docnum_hash",
                    "republication_of", "body_complete", "fetched")}
                summary["written"] += 1
                summary["abstracts"] += int(not complete)
            if summary["parsed"]:
                manifest_path = output / "manifest.json"
                save_manifest(manifest, manifest_path)
                # The parent stays private until the renderer exits and the uploader owns it.
                manifest_path.chmod(0o644)
            if not summary["parsed"]:
                summary["status"] = "abstract_listing_only" if summary["inline_papers"] else "no_validated_article"
            context.close()
            browser.close()
    finally:
        policy.close()
    return summary


def main():
    os.umask(0o022)
    try:
        recipe = next(entry for entry in load_registry()
                      if entry["id"] == sys.argv[1] and entry["status"] == "requires-js")
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            result = capture(recipe, Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/data/output"))
        print(json.dumps(result))
    except Exception as error:
        # Never emit browser request URLs, page content, or tracebacks to a public workflow.
        detail = str(error).lower()
        indicators = {label: any(word in detail for word in words) for label, words in {
            "sandbox": ("no usable sandbox", "namespace sandbox", "sandbox_host_linux", "zygote"),
            "permissions": ("operation not permitted", "permission denied"),
            "writable_profile": ("read-only file system", "crashpad", "user data directory"),
            "timeout": ("timeout", "timed out"),
        }.items()}
        print(json.dumps({"status": "browser_failed", "error_type": type(error).__name__, **indicators}))
        return 1
    return int(result["status"] != "ok")


if __name__ == "__main__":
    raise SystemExit(main())
