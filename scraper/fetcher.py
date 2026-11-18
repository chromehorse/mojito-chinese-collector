"""The ONLY module that touches the network.

Hard-rule enforcement lives here: content-type gate, 5MB streaming cap,
manual redirect loop with domain guard, robots.txt, per-domain politeness
delay, honest UA, 30s timeout, bounded retries, disk cache, careful decode.
Fetched bytes are DATA — written to disk and decoded as text, nothing else.
"""
import codecs
import hashlib
import json
import logging
import random
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

import httpx
from charset_normalizer import from_bytes

from scraper import config

log = logging.getLogger("fetcher")


def utcnow():
    """Shared tz-aware-UTC helper (timezone scar: never naive, never utcnow())."""
    return datetime.now(timezone.utc)


def _host(url):
    return (urlsplit(url).hostname or "").lower()


def host_allowed(url, allowed_domains):
    h = _host(url)
    for d in allowed_domains:
        d = d.lower()
        if h == d or h.endswith("." + d):
            return True
    return False


def _base_content_type(ct_header):
    return (ct_header or "").split(";")[0].strip().lower()


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int = 0
    content_type: str = ""
    text: str = None
    encoding: str = None
    from_cache: bool = False
    fetched_at: str = ""
    skip_reason: str = None      # oversize | disallowed-content-type | redirect-off-domain |
                                 # robots-disallowed | decode_failed | http-error | fetch-error |
                                 # too-many-redirects
    attachment: dict = None      # {url, content_type} when content-type gate skips the body

    @property
    def ok(self):
        return self.skip_reason is None and 200 <= self.status < 300 and self.text is not None


class Fetcher:
    def __init__(self, transport=None, sleep=time.sleep, cache_dir=None,
                 delay=config.PER_DOMAIN_DELAY_SECONDS, jitter=config.DELAY_JITTER_SECONDS,
                 strict_html_hosts=None):
        self._client = httpx.Client(
            http2=False,
            follow_redirects=False,  # redirects handled manually with a domain guard
            timeout=config.TIMEOUT_SECONDS,
            headers={"User-Agent": config.USER_AGENT},
            transport=transport,
        )
        self._sleep = sleep
        self._delay = delay
        self._jitter = jitter
        self._last_fetch = {}      # domain -> monotonic time of last network hit
        self._strict_html_hosts = strict_html_hosts or {}
        self._stopped_hosts = {}
        self._robots = {}          # netloc -> RobotFileParser or "allow-all" / "deny-all"
        self.cache_dir = cache_dir or config.CACHE_DIR
        self.stats = {
            "network_fetches": 0, "cache_hits": 0, "oversize_aborts": 0,
            "offdomain_redirect_aborts": 0, "disallowed_content_type": 0,
            "robots_blocked": 0, "decode_failed": 0, "http_errors": 0,
            "fetch_errors": 0, "retries": 0,
        }
        self.warnings = []         # surfaced into RUN_REPORT (silent-swallow scar)

    def close(self):
        self._client.close()

    def _warn(self, msg):
        log.warning(msg)
        self.warnings.append(msg)

    # ---------- politeness ----------

    def _polite_wait(self, url):
        h = _host(url)
        last = self._last_fetch.get(h)
        now = time.monotonic()
        if last is not None:
            wait = self._delay + random.uniform(0, self._jitter) - (now - last)
            if wait > 0:
                self._sleep(wait)
        self._last_fetch[h] = time.monotonic()

    def _strict_robots(self, robots_url, host):
        options = self._strict_html_hosts[host]
        current = robots_url
        for _ in range(config.MAX_REDIRECTS + 1):
            self._polite_wait(current)
            with self._client.stream("GET", current) as response:
                self.stats["network_fetches"] += 1
                status = response.status_code
                if response.is_redirect:
                    current = urljoin(current, response.headers["location"])
                    parts = urlsplit(current)
                    if parts.scheme != "https" or parts.netloc != host:
                        break
                    continue
                if status in {404, 410}:
                    return "allow-all"
                if status != 200:
                    break
                content_type = _base_content_type(response.headers.get("content-type"))
                missing_path = options.get("robots_missing_path")
                soft_404 = (missing_path and current == f"https://{host}{missing_path}"
                            and content_type == "text/html")
                if not soft_404 and content_type != "text/plain":
                    break
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 500 * 1024:
                        break
                if len(body) > 500 * 1024:
                    break
                text = body.decode("utf-8-sig", errors="replace")
                if soft_404:
                    if options["robots_missing_marker"] in text:
                        return "allow-all"
                    break
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(text.splitlines())
                return parser
        self._warn(f"unverified robots.txt for {host}")
        return "deny-all"

    def _robots_allows(self, url):
        parts = urlsplit(url)
        netloc = parts.netloc.lower()
        rp = self._robots.get(netloc)
        if rp is None:
            robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
            strict = netloc in self._strict_html_hosts
            try:
                if strict:
                    rp = self._strict_robots(robots_url, netloc)
                else:
                    self._polite_wait(robots_url)
                    resp = self._client.get(robots_url)
                    self.stats["network_fetches"] += 1
                    if resp.status_code == 200:
                        parser = urllib.robotparser.RobotFileParser()
                        parser.parse(resp.text.splitlines())
                        rp = parser
                    elif resp.status_code in (401, 403):
                        rp = "deny-all"
                    else:
                        rp = "allow-all"
            except httpx.HTTPError as e:
                self._warn(f"robots.txt fetch failed for {netloc}: {type(e).__name__}")
                rp = "deny-all" if strict else "allow-all"
            self._robots[netloc] = rp
        if rp == "allow-all":
            return True
        if rp == "deny-all":
            return False
        return rp.can_fetch(config.USER_AGENT, url)

    # ---------- cache ----------

    def _cache_paths(self, url):
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.json", self.cache_dir / f"{key}.body"

    def _cache_get(self, url, max_age_days):
        meta_path, body_path = self._cache_paths(url)
        if not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(meta["fetched_at"])
        except (ValueError, KeyError) as e:
            self._warn(f"corrupt cache entry for {url}: {e}")
            return None
        if utcnow() - fetched_at > timedelta(days=max_age_days):
            return None
        body = body_path.read_bytes() if body_path.exists() else b""
        return meta, body

    def _cache_put(self, url, status, content_type, final_url, body):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        meta_path, body_path = self._cache_paths(url)
        body_path.write_bytes(body)
        meta = {"url": url, "final_url": final_url, "status": status,
                "content_type": content_type, "fetched_at": utcnow().isoformat()}
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    # ---------- decode ----------

    def decode(self, body, declared_charset):
        """Declared charset only if it strictly decodes AND normalizer agrees;
        else charset-normalizer; else GB18030; else None (decode_failed)."""
        best = None
        try:
            match = from_bytes(body).best()
            if match is not None:
                best = match.encoding
        except Exception as e:  # normalizer crash on hostile bytes must not kill the run
            self._warn(f"charset-normalizer error: {type(e).__name__}")
        if declared_charset:
            try:
                decoded = body.decode(declared_charset)
                if best is None:
                    return decoded, declared_charset
                try:
                    same = codecs.lookup(best).name == codecs.lookup(declared_charset).name
                except LookupError:
                    same = False
                if same or body.decode(best, errors="replace") == decoded:
                    return decoded, declared_charset
                # declared decodes but normalizer disagrees -> prefer normalizer (lying header)
            except (LookupError, UnicodeDecodeError):
                pass
        if best is not None:
            try:
                return body.decode(best), best
            except UnicodeDecodeError:
                pass
        try:
            return body.decode("gb18030"), "gb18030"
        except UnicodeDecodeError:
            return None, None

    # ---------- fetch ----------

    def _strict_content_types(self, url):
        types = {"text/html", "application/xhtml+xml"}
        if url in self._strict_html_hosts[_host(url)].get("json_urls", ()):
            types.add("application/json")
        return types

    def fetch(self, url, allowed_domains, max_age_days=0, is_retry=False, extra_types=None):
        """Fetch a URL as text under all hard rules. max_age_days>0 enables cache reuse.

        extra_types widens the content-type gate for this call only; the sole
        legitimate caller is bundle_scan.py with JS_ANALYSIS_CONTENT_TYPES
        (annex amendment 1 — bundles fetched as text, never executed)."""
        if not host_allowed(url, allowed_domains):
            self._warn(f"refused off-domain fetch: {url}")
            return FetchResult(url=url, skip_reason="redirect-off-domain")

        parts = urlsplit(url)
        host = parts.hostname
        strict = host in self._strict_html_hosts
        if strict and (parts.scheme != "https" or parts.netloc != host):
            return FetchResult(url=url, skip_reason="strict-https-origin-required")
        if host in self._stopped_hosts:
            return FetchResult(url=url, skip_reason=self._stopped_hosts[host])

        if max_age_days > 0:
            cached = self._cache_get(url, max_age_days)
            if cached and strict:
                meta, body = cached
                if (meta.get("final_url") != url or meta.get("status") != 200
                        or meta.get("content_type") not in self._strict_content_types(url)
                        or not 0 < len(body) <= config.MAX_RESPONSE_BYTES):
                    cached = None
            if cached:
                meta, body = cached
                self.stats["cache_hits"] += 1
                text, enc = self.decode(body, None) if body else ("", None)
                if text is None:
                    self.stats["decode_failed"] += 1
                    return FetchResult(url=url, final_url=meta["final_url"], status=meta["status"],
                                       content_type=meta["content_type"], from_cache=True,
                                       fetched_at=meta["fetched_at"], skip_reason="decode_failed")
                return FetchResult(url=url, final_url=meta["final_url"], status=meta["status"],
                                   content_type=meta["content_type"], text=text, encoding=enc,
                                   from_cache=True, fetched_at=meta["fetched_at"])

        if not self._robots_allows(url):
            self.stats["robots_blocked"] += 1
            self._warn(f"robots disallows {url}")
            return FetchResult(url=url, skip_reason="robots-disallowed")

        current = url
        for _hop in range(config.MAX_REDIRECTS + 1):
            result = self._fetch_once(current, allowed_domains, extra_types)
            if result.skip_reason == "__redirect__":
                nxt = result.final_url
                if strict or not host_allowed(nxt, allowed_domains):
                    self.stats["offdomain_redirect_aborts"] += 1
                    self._warn(f"aborted off-domain redirect {current} -> {nxt}")
                    return FetchResult(url=url, final_url=nxt, status=result.status,
                                       skip_reason="redirect-off-domain")
                current = nxt
                continue
            result.url = url
            return result
        self._warn(f"too many redirects: {url}")
        return FetchResult(url=url, skip_reason="too-many-redirects")

    def download_attachment(self, url, allowed_domains, dest_path,
                            allowed_types=None, max_bytes=None):
        """Rung-B binary download (annex amendment 2). Streams to dest_path under
        domain guard, robots, politeness, manual redirect loop, content-type gate
        (default application/pdf) and 30MB cap. Bytes are NEVER decoded, parsed,
        or opened here — the file lands in quarantine for Defender + pypdf only.
        Returns FetchResult with text=None; .status/.content_type/.skip_reason set."""
        allowed_types = allowed_types or config.PDF_CONTENT_TYPES
        max_bytes = max_bytes or config.MAX_PDF_BYTES
        if not host_allowed(url, allowed_domains):
            self._warn(f"refused off-domain attachment fetch: {url}")
            return FetchResult(url=url, skip_reason="redirect-off-domain")
        if not self._robots_allows(url):
            self.stats["robots_blocked"] += 1
            self._warn(f"robots disallows {url}")
            return FetchResult(url=url, skip_reason="robots-disallowed")
        current = url
        for _hop in range(config.MAX_REDIRECTS + 1):
            self._polite_wait(current)
            try:
                with self._client.stream("GET", current) as resp:
                    self.stats["network_fetches"] += 1
                    if resp.status_code in (301, 302, 303, 307, 308):
                        nxt = urljoin(current, resp.headers.get("location", ""))
                        if not host_allowed(nxt, allowed_domains):
                            self.stats["offdomain_redirect_aborts"] += 1
                            self._warn(f"aborted off-domain redirect {current} -> {nxt}")
                            return FetchResult(url=url, final_url=nxt, status=resp.status_code,
                                               skip_reason="redirect-off-domain")
                        current = nxt
                        continue
                    if resp.status_code >= 400:
                        self.stats["http_errors"] += 1
                        self._warn(f"http {resp.status_code} on {current}")
                        return FetchResult(url=url, final_url=current, status=resp.status_code,
                                           skip_reason="http-error")
                    ct = _base_content_type(resp.headers.get("content-type"))
                    if ct not in allowed_types:
                        self.stats["disallowed_content_type"] += 1
                        return FetchResult(url=url, final_url=current, status=resp.status_code,
                                           content_type=ct, skip_reason="disallowed-content-type")
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            self.stats["oversize_aborts"] += 1
                            self._warn(f"oversize abort (> {max_bytes}B): {current}")
                            return FetchResult(url=url, final_url=current, status=resp.status_code,
                                               content_type=ct, skip_reason="oversize")
                        chunks.append(chunk)
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    dest_path.write_bytes(b"".join(chunks))
                    return FetchResult(url=url, final_url=current, status=resp.status_code,
                                       content_type=ct, fetched_at=utcnow().isoformat())
            except httpx.HTTPError as e:
                self.stats["fetch_errors"] += 1
                self._warn(f"fetch error on {current}: {type(e).__name__}")
                return FetchResult(url=url, skip_reason="fetch-error")
        self._warn(f"too many redirects: {url}")
        return FetchResult(url=url, skip_reason="too-many-redirects")

    def _fetch_once(self, url, allowed_domains, extra_types=None):
        strict = _host(url) in self._strict_html_hosts
        allowed_ct = (self._strict_content_types(url) if strict
                      else config.ALLOWED_CONTENT_TYPES | (extra_types or set()))
        last_error = None
        for attempt in range(config.MAX_RETRIES + 1):
            if attempt:
                self.stats["retries"] += 1
                self._sleep(2 ** attempt)
            self._polite_wait(url)
            try:
                with self._client.stream("GET", url) as resp:
                    self.stats["network_fetches"] += 1
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location", "")
                        return FetchResult(url=url, final_url=urljoin(url, loc),
                                           status=resp.status_code, skip_reason="__redirect__")
                    if strict and resp.status_code in {401, 403, 429}:
                        reason = f"host-paused-http-{resp.status_code}"
                        self._stopped_hosts[_host(url)] = reason
                        self.stats["http_errors"] += 1
                        self._warn(f"{reason} on {url}")
                        return FetchResult(url=url, final_url=url, status=resp.status_code,
                                           skip_reason=reason)
                    if resp.status_code >= 500:
                        last_error = f"http-{resp.status_code}"
                        continue  # retryable
                    if resp.status_code >= 400:
                        self.stats["http_errors"] += 1
                        self._warn(f"http {resp.status_code} on {url}")
                        return FetchResult(url=url, final_url=url, status=resp.status_code,
                                           skip_reason="http-error")
                    ct = _base_content_type(resp.headers.get("content-type"))
                    if ct not in allowed_ct:
                        self.stats["disallowed_content_type"] += 1
                        resp.close()
                        return FetchResult(url=url, final_url=url, status=resp.status_code,
                                           content_type=ct, skip_reason="disallowed-content-type",
                                           attachment={"url": url, "content_type": ct})
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():
                        size += len(chunk)
                        if size > config.MAX_RESPONSE_BYTES:
                            self.stats["oversize_aborts"] += 1
                            self._warn(f"oversize abort (> {config.MAX_RESPONSE_BYTES}B): {url}")
                            return FetchResult(url=url, final_url=url, status=resp.status_code,
                                               content_type=ct, skip_reason="oversize")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    declared = None
                    if "charset=" in resp.headers.get("content-type", "").lower():
                        declared = resp.headers["content-type"].lower().split("charset=")[-1].split(";")[0].strip().strip('"')
                    text, enc = self.decode(body, declared)
                    fetched_at = utcnow().isoformat()
                    self._cache_put(url, resp.status_code, ct, url, body)
                    if text is None:
                        self.stats["decode_failed"] += 1
                        self._warn(f"decode failed: {url}")
                        return FetchResult(url=url, final_url=url, status=resp.status_code,
                                           content_type=ct, fetched_at=fetched_at,
                                           skip_reason="decode_failed")
                    return FetchResult(url=url, final_url=url, status=resp.status_code,
                                       content_type=ct, text=text, encoding=enc,
                                       fetched_at=fetched_at)
            except httpx.TimeoutException:
                last_error = "timeout"
                continue  # retryable
            except httpx.HTTPError as e:
                self.stats["fetch_errors"] += 1
                self._warn(f"fetch error on {url}: {type(e).__name__}")
                return FetchResult(url=url, skip_reason="fetch-error")
        self.stats["fetch_errors"] += 1
        self._warn(f"retries exhausted on {url}: {last_error}")
        return FetchResult(url=url, skip_reason="fetch-error")
