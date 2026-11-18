"""seed-doc (MOST ethics code): one canonical URL. Revision monitoring
(content_hash change on re-fetch -> revision-detected) is handled by run.py,
which keeps both records."""
from scraper.adapters._common import parse_article


def discover(entry, fetcher, cap=None):
    return [entry["list_url"]]


def parse(html_text, entry, url=""):
    return parse_article(html_text, entry, url)
