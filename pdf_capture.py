"""Verify a registered publisher attachment and extract its text on a disposable VM."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


CASES = {
    "tc260_005": {
        "parent": "https://www.cac.gov.cn/2026-05/22/c_1781191242686496.htm",
        "pdf": "https://www.cac.gov.cn/rootimages/uploadimg/1781191242772713/1781191242772713.pdf",
        "title": "人工智能应用伦理安全指引",
        "date": "2026-05-19",
        "pages": 9,
        "anchors": {1: "TC260-005", 2: "原则性、参考性技术文件", 3: "应用开发指引",
                    5: "术语和定义", 6: "人类主导权影响", 7: "敏捷共治", 8: "服务提供指引",
                    9: "帮助建设良性人工智能生态"},
        "cross_page": {6: "健康有序应用。", 7: "渠道有效运转。", 8: "人工智能服务。"},
    },
    "miit_ethics_control": {
        "parent": "https://www.miit.gov.cn/jgsj/kjs/wjfb/art/2026/art_2995f16b28504ddcbb604e918eb15759.html",
        "pdf": "https://www.miit.gov.cn/cms_files/filemanager/1226211233/attach/20263/a532ba50febb46c9b5424d436e63de81.pdf",
        "title": "人工智能科技伦理审查与服务办法",
        "date": "2026-04-02",
        "pages": 12,
        "anchors": {},
        "cross_page": {},
    },
}


class CaptureError(ValueError):
    pass


def fetch(case, quarantine):
    import httpx
    from chinese_collector import miit_pdf_links
    from scraper.adapters._common import clean_tree, collect_attachment_links, first_text
    from scraper.config import MAX_PDF_BYTES
    from scraper.fetcher import Fetcher

    parent, url = case["parent"], case["pdf"]
    host = urlsplit(parent).netloc
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.netloc != host or parts.hostname != host
            or parts.query or parts.fragment or not parts.path.endswith(".pdf")):
        raise CaptureError("unexpected_attachment_origin")
    fetcher = Fetcher(cache_dir=Path("/tmp/pdf-parent"), transport=httpx.HTTPTransport(trust_env=False),
                      strict_html_hosts={host: {}})
    try:
        page = fetcher.fetch(parent, [host])
        if not page.ok or page.final_url != parent:
            raise CaptureError("parent_not_available")
        tree = clean_tree(page.text)
        title = first_text(tree, "#con_title, h1") or ""
        attachments = {item["url"] for item in collect_attachment_links(tree, parent, [host])}
        attachments.update(miit_pdf_links(page.text, parent))
        if case["title"] not in title or url not in attachments:
            raise CaptureError("publisher_link_not_verified")
        if not fetcher._robots_allows(url):
            raise CaptureError("attachment_crawl_policy")
        fetcher._polite_wait(url)
        dest = quarantine / "attachment.pdf"
        with fetcher._client.stream("GET", url, follow_redirects=False,
                                    headers={"Accept": "application/pdf", "Accept-Encoding": "identity"}) as response:
            mime = response.headers.get("content-type", "").partition(";")[0].strip().casefold()
            if response.status_code != 200 or mime != "application/pdf":
                raise CaptureError("unexpected_attachment_response")
            size = 0
            with dest.open("xb") as output:
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    if size == 0 and not chunk.startswith(b"%PDF-"):
                        raise CaptureError("invalid_pdf_header")
                    size += len(chunk)
                    if size > MAX_PDF_BYTES:
                        raise CaptureError("attachment_size_cap")
                    output.write(chunk)
            if not size:
                raise CaptureError("empty_pdf")
        return {"status": "downloaded", "bytes": size}
    finally:
        fetcher.close()


def compact(text):
    return re.sub(r"\s+", "", text)


def score_pages(case, pages):
    compact_pages = [compact(page) for page in pages]
    anchor_hits = sum(compact(text) in compact_pages[number - 1]
                      for number, text in case["anchors"].items())
    title_in_document = compact(case["title"]) in "".join(compact_pages)
    article_labels = re.findall(r"(?m)^\s*第([一二三四五六七八九十]+)\s*条", "\n".join(pages))
    cross_page_hits = 0
    for index, phrase in case["cross_page"].items():
        tail = re.sub(r"\s+[0-9IVX]+\s*$", "", pages[index - 1].rstrip())
        head = re.sub(r"^\s*[0-9IVX]+\s+", "", pages[index])
        cross_page_hits += compact(phrase) in compact(tail)[-140:] + compact(head)[:140]
    return {"nonempty_pages": sum(bool(text) for text in compact_pages),
            "title_found": title_in_document, "anchor_hits": anchor_hits,
            "cross_page_hits": cross_page_hits, "numbered_articles": len(article_labels),
            "chinese_characters": sum(len(re.findall(r"[\u3400-\u9fff]", page)) for page in pages),
            "replacement_characters": sum(page.count("\ufffd") + page.count("\x00") for page in pages)}


def extract(case, quarantine):
    from pypdf import PdfReader

    path = quarantine / "attachment.pdf"
    with path.open("rb") as stream:
        reader = PdfReader(stream, strict=True)
        if reader.is_encrypted:
            raise CaptureError("encrypted_pdf")
        if len(reader.pages) != case["pages"]:
            raise CaptureError("unexpected_page_count")
        modes = {}
        for mode in ("plain", "layout"):
            pages = [page.extract_text(extraction_mode=mode) or "" for page in reader.pages]
            if sum(len(page.encode("utf-8")) for page in pages) > 3 * 1024 * 1024:
                raise CaptureError("text_size_cap")
            modes[mode] = score_pages(case, pages)
    return {"status": "measured", "pages": case["pages"], "expected_anchors": len(case["anchors"]),
            "expected_cross_page": len(case["cross_page"]), "modes": modes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("fetch", "extract"))
    parser.add_argument("case", choices=tuple(CASES))
    parser.add_argument("quarantine", type=Path)
    args = parser.parse_args()
    try:
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            run = fetch if args.phase == "fetch" else extract
            result = run(CASES[args.case], args.quarantine)
        print(json.dumps(result), flush=True)
    except Exception as error:
        print(json.dumps({"status": "pdf_capture_failed", "phase": args.phase,
                          "reason": str(error) if isinstance(error, CaptureError) else type(error).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
