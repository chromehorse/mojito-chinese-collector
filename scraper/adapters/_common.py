"""Shared HTML helpers for adapters. selectolax only; fetched bytes are DATA."""
import re
from urllib.parse import urljoin, urldefrag

from selectolax.lexbor import LexborHTMLParser

from scraper.fetcher import host_allowed

STRIP_TAGS = ["script", "style", "iframe", "object", "embed"]

# non-HTML resources we never download; recorded as attachments instead
ATTACHMENT_EXT_RE = re.compile(
    r"\.(pdf|docx?|xlsx?|pptx?|zip|rar|7z|exe|jpg|jpeg|png|gif|mp[34]|wpsx?|ofd)$", re.I)

DOCNUM_RE = re.compile(
    r"[一-鿿]{1,10}[〔\[［]\s*(?:19|20)\d{2}\s*[〕\]］]\s*第?\s*\d+\s*号"
    r"|[一-鿿]{1,10}字第\s*\d+\s*号")

_DATE_RES = [
    re.compile(r"((?:19|20)\d{2})[-/.年]\s*(\d{1,2})[-/.月]\s*(\d{1,2})日?"),
]


# exact-match chrome paragraphs seen inside body containers on live pages
CHROME_PARAS = {"分享让更多人看到", "关注公众号", "扫码关注", "【打印】 【纠错】", "打印 纠错"}


class AdapterError(Exception):
    """Raised when a recipe yields nothing parseable — a broken-recipe finding,
    never a silent empty success (the silent-swallow scar)."""


def clean_tree(html_text):
    tree = LexborHTMLParser(html_text)
    tree.strip_tags(STRIP_TAGS)
    return tree


def first_text(tree, selectors):
    """Text of first node matching any of the comma-separated selectors, or None."""
    if not selectors:
        return None
    for sel in [s.strip() for s in selectors.split(",") if s.strip()]:
        for node in tree.css(sel):
            text = node.text(separator=" ", strip=True)
            if text:
                return text
    return None


def extract_links(html_text, base_url, selector, allowed_domains, url_pattern=None):
    """Absolute, deduped, on-domain links from nodes matching selector.
    Optional url_pattern regex narrows to article-looking URLs."""
    tree = clean_tree(html_text)
    pat = re.compile(url_pattern) if url_pattern else None
    seen, out = set(), []
    for node in tree.css(selector or "a"):  # comma groups are CSS unions
        a = node if node.tag == "a" else node.css_first("a")
        if a is None:
            continue
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        url = urldefrag(urljoin(base_url, href)).url
        if url in seen or not host_allowed(url, allowed_domains):
            continue
        if ATTACHMENT_EXT_RE.search(url.split("?")[0]):
            continue
        if pat and not pat.search(url):
            continue
        seen.add(url)
        out.append(url)
    return out


def parse_date_zh(text):
    """First recognizable date in text -> 'YYYY-MM-DD', else None."""
    if not text:
        return None
    for rx in _DATE_RES:
        m = rx.search(text)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def extract_document_number(text):
    if not text:
        return None
    m = DOCNUM_RE.search(text)
    return m.group(0).replace(" ", "") if m else None


def body_paragraphs(tree, body_selectors):
    """Paragraph texts (p + body-internal headings) in source order from the
    first body container selector that yields text."""
    for sel in [s.strip() for s in (body_selectors or "").split(",") if s.strip()]:
        container = tree.css_first(sel)
        if container is None:
            continue
        paras = []
        for node in container.css("p, h2, h3, h4, li"):
            text = node.text(separator=" ", strip=True)
            text = re.sub(r"\s+", " ", text).strip()
            if text and text not in CHROME_PARAS:
                paras.append(text)
        if not paras:
            text = container.text(separator="\n", strip=True)
            paras = [re.sub(r"\s+", " ", ln).strip() for ln in text.split("\n") if ln.strip()]
        if paras:
            return paras
    return []


def complete_body_paragraphs(tree, selector):
    """Retain text from mixed paragraph, division and table layouts in source order."""
    containers = tree.css(selector)
    if len(containers) != 1:
        return []
    container = containers[0]
    block_tags = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li",
                  "blockquote", "pre", "td", "th", "section", "dt", "dd"}
    paragraphs, parts = [], []
    block = None
    for node in container.traverse(include_text=True):
        if node.tag not in {"-text", "br", "hr"}:
            continue
        parent = node.parent
        while parent.mem_id != container.mem_id and parent.tag not in block_tags:
            parent = parent.parent
        if parent.mem_id != block or node.tag != "-text":
            text = re.sub(r"\s+", " ", "".join(parts)).strip()
            if text:
                paragraphs.append(text)
            parts.clear()
            block = parent.mem_id
        if node.tag == "-text":
            parts.append(node.text(separator="", strip=False))
    text = re.sub(r"\s+", " ", "".join(parts)).strip()
    if text:
        paragraphs.append(text)
    return paragraphs


def collect_attachment_links(tree, base_url, allowed_domains):
    out, seen = [], set()
    for a in tree.css("a"):
        href = (a.attributes.get("href") or "").strip()
        if not href:
            continue
        url = urldefrag(urljoin(base_url, href)).url
        m = ATTACHMENT_EXT_RE.search(url.split("?")[0])
        if m and url not in seen and host_allowed(url, allowed_domains):
            seen.add(url)
            out.append({"url": url, "content_type": f"linked/{m.group(1).lower()}"})
    return out


def parse_article(html_text, entry, url=""):
    """Generic article parse driven by entry['recipe']['article'] selectors."""
    art = entry["recipe"].get("article", {})
    tree = clean_tree(html_text)
    title = first_text(tree, art.get("title", "h1"))
    date_text = first_text(tree, art.get("date", ""))
    author = first_text(tree, art.get("author", ""))
    if author:
        author = re.sub(r"^(?:作者|文)\s*[:：]\s*", "", author).strip() or None
    body_selector = art.get("body", "")
    complete_body = art.get("complete_body", False)
    paras = (complete_body_paragraphs(tree, body_selector) if complete_body
             else body_paragraphs(tree, body_selector))
    published = parse_date_zh(date_text)
    if complete_body and not published:
        raise AdapterError(f"{entry['id']}: visible publication date missing for {url}")
    if not title or not paras:
        raise AdapterError(
            f"{entry['id']}: parse yielded title={bool(title)} paras={len(paras)} for {url or '<fixture>'}")
    full_text = title + "\n" + "\n".join(paras)
    return {
        "title": title,
        "date": published or parse_date_zh(html_text[:4000]),
        "author": author,
        "body_paragraphs": paras,
        "document_number": extract_document_number(full_text),
        "attachments": collect_attachment_links(tree, url, entry["allowed_domains"]),
    }
