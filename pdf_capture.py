"""Verify a registered publisher attachment and extract its text on a disposable VM."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import unicodedata
from urllib.parse import urljoin, urlsplit


CASES = {
    "tc260_005": {
        "parent": "https://www.cac.gov.cn/2026-05/22/c_1781191242686496.htm",
        "pdf": "https://www.cac.gov.cn/rootimages/uploadimg/1781191242772713/1781191242772713.pdf",
        "title": "人工智能应用伦理安全指引",
        "opening": "人工智能应用伦理安全指引",
        "date": "2026-05-19",
        "document_number": "TC260-005",
        "publisher": "全国网络安全标准化技术委员会",
        "pages": 9,
        "page_labels": (None, "I", "II", "III", "1", "2", "3", "4", "5"),
        "ending": "帮助建设良性人工智能生态。",
        "anchors": {1: "TC260-005", 2: "原则性、参考性技术文件", 3: "应用开发指引",
                    5: "术语和定义", 6: "人类主导权影响", 7: "敏捷共治", 8: "服务提供指引",
                    9: "帮助建设良性人工智能生态"},
        "cross_page": {6: "健康有序应用。", 7: "渠道有效运转。", 8: "人工智能服务。"},
    },
    "miit_ethics_control": {
        "parent": "https://www.miit.gov.cn/jgsj/kjs/wjfb/art/2026/art_2995f16b28504ddcbb604e918eb15759.html",
        "pdf": "https://www.miit.gov.cn/cms_files/filemanager/1226211233/attach/20263/a532ba50febb46c9b5424d436e63de81.pdf",
        "title": "人工智能科技伦理审查与服务办法",
        "opening": "第一章总则",
        "date": "2026-04-02",
        "pages": 12,
        "anchors": {1: "第一条", 12: "本清单将根据工作需要动态调整"},
        "cross_page": {3: "主管部门监督。", 5: "系统的鲁棒性", 7: "人工智能科技活动清单"},
    },
    "shanghai_manufacturing_2026": {
        "parent": "https://www.sheitc.sh.gov.cn/cyfz/20260112/11e3168ff89a4eeea2b1afd193c19418.html",
        "pdf": "https://www.sheitc.sh.gov.cn/cmsres/bd/bd2a507f86fd48eea75d71c94d247c84/3d967ffb9266571689033d2857f6a0ee.pdf",
        "title": "上海市经济信息化委关于组织2026年上海市制造业智能化发展项目申报的通知",
        "opening": "上海市经济和信息化委员会文件",
        "date": "2026-01-12",
        "document_number": "沪经信软〔2026〕20号",
        "publisher": "上海市经济和信息化委员会",
        "pages": 6,
        "title_meta": "ArticleTitle",
        "anchors": {
            1: "2026年1月10日",
            2: "支持“AI+制造”样板企业梯度培育",
            3: "支持工业互联网平台智能化转型升级",
            4: "上海市制造业智能化发展支持方向",
            5: "经评估后给予分档支持",
            6: "2026年1月12日印发",
        },
        "cross_page": {2: "数据采集、处理", 3: "信用记录良好"},
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
        if metadata_title := case.get("title_meta"):
            title_node = tree.css_first(f'meta[name="{metadata_title}"]')
            title = title_node.attributes.get("content", "") if title_node else ""
        else:
            title = first_text(tree, "#con_title, h1") or ""
        attachments = {item["url"] for item in collect_attachment_links(tree, parent, [host])}
        attachments.update(miit_pdf_links(page.text, parent))
        if host == "www.cac.gov.cn":
            article = tree.css_first("#BodyLabel")
            visible = article.text(separator="\n", strip=True) if article else ""
            # This publisher prints the exact attachment URL next to its viewer link.
            attachments.update(urljoin(parent, line.strip()) for line in visible.splitlines()
                               if line.strip() in {url, url.removeprefix("https:")})
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
    opening_found = compact(case["opening"]) in compact_pages[0]
    article_labels = re.findall(r"(?m)^\s*第([一二三四五六七八九十]+)\s*条", "\n".join(pages))
    cross_page_hits = 0
    for index, phrase in case["cross_page"].items():
        tail = re.sub(r"\s+[0-9IVX]+\s*$", "", pages[index - 1].rstrip())
        head = re.sub(r"^\s*[0-9IVX]+\s+", "", pages[index])
        cross_page_hits += compact(phrase) in compact(tail)[-140:] + compact(head)[:140]
    return {"nonempty_pages": sum(bool(text) for text in compact_pages),
            "opening_found": opening_found, "anchor_hits": anchor_hits,
            "cross_page_hits": cross_page_hits, "numbered_articles": len(article_labels),
            "distinct_numbered_articles": len(set(article_labels)),
            "chinese_characters": sum(len(re.findall(r"[\u3400-\u9fff]", page)) for page in pages),
            "replacement_characters": sum(page.count("\ufffd") + page.count("\x00") for page in pages)}


def without_folio(page, label):
    lines = page.strip("\n").splitlines()
    boundaries = [i for i, row in enumerate(lines) if row.strip()][:1]
    if boundaries:
        last = len(lines) - 1 - next(i for i, row in enumerate(reversed(lines)) if row.strip())
        boundaries = list(dict.fromkeys([boundaries[0], last]))
    if label is not None:
        matches = [i for i in boundaries if unicodedata.normalize("NFKC", lines[i].strip()) == label]
        if len(matches) != 1:
            raise CaptureError("folio_not_verified")
        lines.pop(matches[0])
    return "\n".join(lines)


_BLOCK_START = re.compile(r"^(?:\d+(?:\.\d+)*(?:\s+|$)|[a-z]\s*[）)]|GB/T\s+\d+|\[来源[：:])")
_HAN = re.compile(r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]")


def join_line(left, right):
    space = "" if _HAN.match(left[-1]) or _HAN.match(right[0]) else " "
    return left + space + right


def page_blocks(page):
    blocks = []
    boundary = True
    for line in page.splitlines():
        line = " ".join(line.split())
        if not line:
            boundary = True
        elif boundary or _BLOCK_START.match(line):
            blocks.append(line)
            boundary = False
        else:
            blocks[-1] = join_line(blocks[-1], line)
    return blocks


def normalize(case, modes):
    plain, layout = ([without_folio(text, label) for text, label in zip(modes[mode], case["page_labels"], strict=True)]
                     for mode in ("plain", "layout"))
    if list(map(compact, plain)) != list(map(compact, layout)):
        raise CaptureError("text_order_disagrees")
    paragraphs, spans = [], []
    for number, page in enumerate(layout, 1):
        blocks = page_blocks(page)
        if not blocks:
            raise CaptureError("empty_page")
        if phrase := case["cross_page"].get(number - 1):
            joined = join_line(paragraphs[-1], blocks.pop(0))
            if compact(phrase) not in compact(joined):
                raise CaptureError("page_continuation_not_verified")
            paragraphs[-1] = joined
            spans[-1] = (spans[-1][0], number)
        paragraphs.extend(blocks)
        spans.extend([(number, number)] * len(blocks))
    if (compact("".join(paragraphs)) != compact("".join(plain))
            or not compact(paragraphs[-1]).endswith(compact(case["ending"]))
            or compact(case["date"]) not in compact(layout[0])):
        raise CaptureError("normalized_text_not_verified")
    return paragraphs, spans


def write_capture(case, path, output, paragraphs, spans):
    from run import load_manifest, save_manifest
    from scraper.fetcher import utcnow
    from scraper.normalize import build_record, to_markdown
    from scraper.registry import load_registry

    entry = next(e for e in load_registry() if e["id"] == "cac_regs")
    meta, text = build_record({"title": case["title"] + " 1.0", "date": case["date"],
                               "body_paragraphs": paragraphs, "document_number": case["document_number"],
                               "issuing_institution": case["publisher"]},
                              entry, case["pdf"], utcnow().isoformat())
    meta.update(source_genre="guidance", attachment_parent=case["parent"],
                attachment_sha256=sha256(path.read_bytes()).hexdigest(),
                pdf_page_count=case["pages"], pdf_page_spans=",".join(f"{start}-{end}" for start, end in spans))
    normalized = output / "_normalized"
    normalized.mkdir(parents=True, exist_ok=True)
    (normalized / (meta["record_id"] + ".md")).write_text(to_markdown(meta, text), encoding="utf-8")
    manifest_path = output / "manifest.json"
    manifest = load_manifest(manifest_path)
    manifest["records"][meta["record_id"]] = {key: meta[key] for key in (
        "source", "canonical_url", "title_zh", "published", "content_hash", "docnum_hash",
        "republication_of", "body_complete", "fetched")}
    save_manifest(manifest, manifest_path)


def extract(case, quarantine, output=None):
    from pypdf import PdfReader

    path = quarantine / "attachment.pdf"
    with path.open("rb") as stream:
        reader = PdfReader(stream, strict=True)
        if reader.is_encrypted:
            raise CaptureError("encrypted_pdf")
        if len(reader.pages) != case["pages"]:
            raise CaptureError("unexpected_page_count")
        text_by_mode = {}
        for mode in ("plain", "layout"):
            pages = [page.extract_text(extraction_mode=mode) or "" for page in reader.pages]
            if sum(len(page.encode("utf-8")) for page in pages) > 3 * 1024 * 1024:
                raise CaptureError("text_size_cap")
            text_by_mode[mode] = pages
    modes = {mode: score_pages(case, pages) for mode, pages in text_by_mode.items()}
    staged = {}
    if output is not None:
        if "page_labels" not in case or any(
                m["nonempty_pages"] != case["pages"] or not m["opening_found"] or m["replacement_characters"]
                or m["anchor_hits"] != len(case["anchors"]) or m["cross_page_hits"] != len(case["cross_page"])
                for m in modes.values()):
            raise CaptureError("edition_quality_gate")
        paragraphs, spans = normalize(case, text_by_mode)
        write_capture(case, path, output, paragraphs, spans)
        staged = {"staged": 1, "paragraphs": len(paragraphs),
                  "cross_page_paragraphs": sum(first != last for first, last in spans)}
    page_modes_agree = [compact(plain) == compact(layout) for plain, layout in
                        zip(text_by_mode["plain"], text_by_mode["layout"], strict=True)]
    return {"status": "measured", "pages": case["pages"], "page_modes_agree": page_modes_agree,
            "expected_anchors": len(case["anchors"]),
            "expected_cross_page": len(case["cross_page"]), "modes": modes, **staged}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("fetch", "extract"))
    parser.add_argument("case", choices=tuple(CASES))
    parser.add_argument("quarantine", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            case = CASES[args.case]
            result = (fetch(case, args.quarantine) if args.phase == "fetch"
                      else extract(case, args.quarantine, args.output))
        print(json.dumps(result), flush=True)
    except Exception as error:
        print(json.dumps({"status": "pdf_capture_failed", "phase": args.phase,
                          "reason": str(error) if isinstance(error, CaptureError) else type(error).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
