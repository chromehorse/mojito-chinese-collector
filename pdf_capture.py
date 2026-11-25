"""Verify a registered publisher attachment and extract its text on a disposable VM."""

import argparse
from bisect import bisect_left, bisect_right
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from itertools import accumulate
import json
from math import isfinite
import os
from pathlib import Path
import re
from traceback import extract_tb
import unicodedata
from urllib.parse import urljoin, urlsplit


CASES = {
    "ndrc_plan_2026": {
        "parent": "https://www.ndrc.gov.cn/fggz/fzzlgh/gjfzgh/",
        "pdf": "https://www.ndrc.gov.cn/fggz/fzzlgh/gjfzgh/202603/U020260317369114704096.pdf",
        "title": "国家发展战略和规划",
        "title_selector": "title",
        "probe_only": True,
    },
    "ndrc_plan_2026_archive": {
        "parent": "https://www.ndrc.gov.cn/fggz/fzzlgh/gjfzgh/",
        "pdf": "https://www.ndrc.gov.cn/fggz/fzzlgh/gjfzgh/202603/U020260317369114704096.pdf",
        "title": "国家发展战略和规划",
        "title_selector": "title",
        "parent_archive_timestamp": "20260719101554",
        "archive_timestamp": "20260324133111",
        "pdf_sha256": "29b3c70720b1faff458a123cb29672ee2ee8874a53b6d4a91f402a73f6945027",
        "pages": 140,
        "inspect_all_pages": True,
        "inspection_capture": True,
        "record_title": "中华人民共和国国民经济和社会发展第十五个五年规划纲要",
        "source": "ndrc_national_plans",
        "source_genre": "decree",
        "date": "2026-03-17",
        "document_number": None,
        "publisher": None,
        "probe_only": True,
    },
    "cac_filing_2026_feb": {
        "parent": "https://www.cac.gov.cn/2024-04/02/c_1713729983803145.htm",
        "pdf": ("https://www.cac.gov.cn/cms/pub/interact/downloadfile.jsp?filepath=NUtqEIwGiCjGm2Bhl20cvLGupl4aHWqlv5jFDErUO79vz0vHSSJ6sATU6DtmcGo1YoYJDltyJqpCTDRSAKq6GlAfqphfFn00jUfFRLdBE5I="
                "&fText=%E7%94%9F%E6%88%90%E5%BC%8F%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD%E6%9C%8D%E5%8A%A1%E5%B7%B2%E5%A4%87%E6%A1%88%E5%92%8C%E5%B7%B2%E7%99%BB%E8%AE%B0%E4%BF%A1%E6%81%AF%EF%BC%882026%E5%B9%B42%E6%9C%88%EF%BC%89"),
        "title": "国家互联网信息办公室关于发布生成式人工智能服务已备案信息的公告",
        "roster_month": 2,
        "record_title": "生成式人工智能服务已备案和已登记信息（2026年2月）",
        "source": "cac_regs",
        "source_genre": "institutional-commentary",
        "date": "2026-03-17",
        "document_number": None,
        "publisher": "国家互联网信息办公室",
        "pages": 4,
        "rows_by_page": (23, 25, 23, 23),
        "header_pages": (1, 3),
        "pdf_sha256": "1c6dca73b3de14bf20a25f80513b7122e52330ccc76b6873c2a8b47f58062327",
        "serial_ranges": ((749, 796), (436, 481)),
    },
    "cac_filing_2026_apr": {
        "parent": "https://www.cac.gov.cn/2024-04/02/c_1713729983803145.htm",
        "pdf": ("https://www.cac.gov.cn/cms/pub/interact/downloadfile.jsp?filepath=NUtqEIwGiCjGm2Bhl20cvGww3O6bSTCWgK0Ed72mFb7wB7HPrRt~1khXArUkYV7jzFbGV4URDRziReL724DuNlAfqphfFn00jUfFRLdBE5I="
                "&fText=%E7%94%9F%E6%88%90%E5%BC%8F%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD%E6%9C%8D%E5%8A%A1%E5%B7%B2%E5%A4%87%E6%A1%88%E5%92%8C%E5%B7%B2%E7%99%BB%E8%AE%B0%E4%BF%A1%E6%81%AF%EF%BC%882026%E5%B9%B44%E6%9C%88%EF%BC%89"),
        "title": "国家互联网信息办公室关于发布生成式人工智能服务已备案信息的公告",
        "roster_month": 4,
        "record_title": "生成式人工智能服务已备案和已登记信息（2026年4月）",
        "source": "cac_regs",
        "source_genre": "institutional-commentary",
        "date": "2026-05-13",
        "document_number": None,
        "publisher": "国家互联网信息办公室",
        "pages": 5,
        "rows_by_page": (23, 27, 22, 23, 26),
        "header_pages": (1, 4),
        "pdf_sha256": "71506cb507995eb1fa7cef325b4e87d0a75cf32e77d86ce6a52fe05caffdc3b4",
        "serial_ranges": ((797, 868), (482, 530)),
    },
    "cac_filing_2026_jun": {
        "parent": "https://www.cac.gov.cn/2024-04/02/c_1713729983803145.htm",
        "pdf": ("https://www.cac.gov.cn/cms/pub/interact/downloadfile.jsp?filepath=NUtqEIwGiCjGm2Bhl20cvGmT22KDqGR7FvHLiywcKRhvz0vHSSJ6sATU6DtmcGo1EK~B~GRIuQzz0vNKmupMtFAfqphfFn00jUfFRLdBE5I="
                "&fText=%E7%94%9F%E6%88%90%E5%BC%8F%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD%E6%9C%8D%E5%8A%A1%E5%B7%B2%E5%A4%87%E6%A1%88%E5%92%8C%E5%B7%B2%E7%99%BB%E8%AE%B0%E4%BF%A1%E6%81%AF%EF%BC%882026%E5%B9%B46%E6%9C%88%EF%BC%89"),
        "title": "国家互联网信息办公室关于发布生成式人工智能服务已备案信息的公告",
        "roster_month": 6,
        "record_title": "生成式人工智能服务已备案和已登记信息（2026年6月）",
        "source": "cac_regs",
        "source_genre": "institutional-commentary",
        "date": "2026-07-10",
        "document_number": None,
        "publisher": "国家互联网信息办公室",
        "pages": 8,
        "rows_by_page": (21, 25, 24, 25, 25, 22, 26, 20),
        "header_pages": (1, 6),
        "pdf_sha256": "1ce457e44bc100ae49fa0240362d62655eafd72c8d5fb21a6b8ca223427a00b8",
        "serial_ranges": ((869, 988), (531, 598)),
    },
    "cac_filing_2026_aug": {
        "parent": "https://www.cac.gov.cn/2024-04/02/c_1713729983803145.htm",
        "pdf": ("https://www.cac.gov.cn/cms/pub/interact/downloadfile.jsp?filepath=NUtqEIwGiCjGm2Bhl20cvGmT22KDqGR7FvHLiywcKRhvz0vHSSJ6sATU6DtmcGo1jaT2QSSyYLABPnQL~026zVAfqphfFn00jUfFRLdBE5I="
                "&fText=%E7%94%9F%E6%88%90%E5%BC%8F%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD%E6%9C%8D%E5%8A%A1%E5%B7%B2%E5%A4%87%E6%A1%88%E5%92%8C%E5%B7%B2%E7%99%BB%E8%AE%B0%E4%BF%A1%E6%81%AF%EF%BC%882026%E5%B9%B48%E6%9C%88%EF%BC%89"),
        "title": "国家互联网信息办公室关于发布生成式人工智能服务已备案信息的公告",
        "roster_month": 8,
        "record_title": "生成式人工智能服务已备案和已登记信息（2026年8月）",
        "source": "cac_regs",
        "source_genre": "institutional-commentary",
        "date": "2026-09-14",
        "document_number": None,
        "publisher": "国家互联网信息办公室",
        "pages": 12,
        "rows_by_page": (23, 24, 25, 26, 19, 7, 19, 24, 23, 22, 23, 22),
        "header_pages": (1, 6, 7),
        "columns_by_page": (6, 6, 6, 6, 6, 7, 6, 6, 6, 6, 6, 6),
        "pdf_sha256": "081f8abad11d3e89cc94e9c4e8f76133613a4f4e05b14d6a88e086be050629e5",
        "serial_ranges": ((989, 1105), (1, 7), (599, 731)),
    },
    "tc260_005": {
        "parent": "https://www.cac.gov.cn/2026-05/22/c_1781191242686496.htm",
        "pdf": "https://www.cac.gov.cn/rootimages/uploadimg/1781191242772713/1781191242772713.pdf",
        "title": "人工智能应用伦理安全指引",
        "record_title": "人工智能应用伦理安全指引 1.0",
        "source": "cac_regs",
        "source_genre": "guidance",
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
    "miit_ai_applications_2025": {
        "parent": "https://www.miit.gov.cn/jgsj/kjs/wjfb/art/2026/art_735cc3b48391470ea3baec587e526d5f.html",
        "pdf": "https://www.miit.gov.cn/cms_files/filemanager/1226211233/attach/20267/9d62254ba76243e8b795af58c4c18e6a.pdf",
        "title": "工业和信息化部办公厅关于印发2025年人工智能应用典型案例名单的通知",
        "pages": 16,
        "probe_only": True,
        "inspect_all_pages": True,
        "inspect_grid": True,
    },
    "shanghai_manufacturing_2026": {
        "parent": "https://www.sheitc.sh.gov.cn/cyfz/20260112/11e3168ff89a4eeea2b1afd193c19418.html",
        "pdf": "https://www.sheitc.sh.gov.cn/cmsres/bd/bd2a507f86fd48eea75d71c94d247c84/3d967ffb9266571689033d2857f6a0ee.pdf",
        "title": "上海市经济信息化委关于组织2026年上海市制造业智能化发展项目申报的通知",
        "source": "shanghai_manufacturing_grant",
        "source_genre": "decree",
        "opening": "上海市经济和信息化委员会文件",
        "date": "2026-01-12",
        "document_number": "沪经信软〔2026〕20号",
        "publisher": "上海市经济和信息化委员会",
        "pages": 6,
        "title_meta": "ArticleTitle",
        "dashed_folios": True,
        "annex_page": 5,
        "annex_title": "2026年上海市制造业智能化发展项目指南",
        "prose_selector": ".text-main",
        "prose_count": 33,
        "annex_intro": "附件：2026年上海市制造业智能化发展支持方向",
        "footer": ("上海市经济和信息化委员会办公室", "2026年1月12日印发"),
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
    from chinese_collector import edition_snapshot, miit_pdf_links
    from scraper.adapters._common import clean_tree, collect_attachment_links, complete_body_paragraphs, first_text
    from scraper.config import MAX_PDF_BYTES
    from scraper.fetcher import Fetcher

    parent, url = case["parent"], case.get("pdf")
    parent_request = edition_snapshot({"url": parent, "archive_timestamp": case.get("parent_archive_timestamp")}) or parent
    attachment_request = edition_snapshot({"url": url, "archive_timestamp": case.get("archive_timestamp")}) or url
    archived = parent_request != parent or attachment_request != url
    if archived and (not case.get("probe_only") or parent_request == parent or attachment_request == url):
        raise CaptureError("archive_inspection_only")
    roster_month = case.get("roster_month")
    host = urlsplit(parent).netloc
    if roster_month is None:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.netloc != host or parts.hostname != host
                or parts.query or parts.fragment or not parts.path.endswith(".pdf")):
            raise CaptureError("unexpected_attachment_origin")
    request_host = urlsplit(parent_request).hostname
    fetcher = Fetcher(cache_dir=Path("/tmp/pdf-parent"), transport=httpx.HTTPTransport(trust_env=False),
                      strict_html_hosts={host: {}, request_host: {}})
    try:
        page = fetcher.fetch(parent_request, [request_host])
        if not page.ok or page.final_url != parent_request:
            raise CaptureError("parent_not_available")
        tree = clean_tree(page.text)
        if metadata_title := case.get("title_meta"):
            title_node = tree.css_first(f'meta[name="{metadata_title}"]')
            title = title_node.attributes.get("content", "") if title_node else ""
        else:
            title = first_text(tree, case.get("title_selector", "#con_title, h1")) or ""
        if roster_month is not None:
            from route_probe import CAC_ROSTER_INDEX, MEDIA_TYPE, cac_roster_links

            url = cac_roster_links(page.text).get(roster_month)
            if parent != CAC_ROSTER_INDEX or title != case["title"] or url != case["pdf"]:
                raise CaptureError("publisher_link_not_verified")
        else:
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
        if selector := case.get("prose_selector"):
            paragraphs = complete_body_paragraphs(tree, selector)
            markers = [i for i, text in enumerate(paragraphs) if compact(text) == case["annex_intro"]]
            if markers != [case["prose_count"] - 1]:
                raise CaptureError("publisher_prose_boundary_not_verified")
            (quarantine / "parent-paragraphs.json").write_text(
                json.dumps(paragraphs[:case["prose_count"]], ensure_ascii=False), encoding="utf-8")
        if not fetcher._robots_allows(attachment_request):
            raise CaptureError("attachment_crawl_policy")
        fetcher._polite_wait(attachment_request)
        dest = quarantine / "attachment.pdf"
        accept = "*/*" if roster_month else "application/pdf"
        with fetcher._client.stream("GET", attachment_request, follow_redirects=False,
                                    headers={"Accept": accept, "Accept-Encoding": "identity"}) as response:
            mime = response.headers.get("content-type", "").partition(";")[0].strip().casefold()
            disposition = response.headers.get("content-disposition", "").partition(";")[0].strip().casefold()
            allowed_mime = {"application/pdf", "application/octet-stream"} if roster_month else {"application/pdf"}
            # All four registered CAC roster URLs sent malformed media types with an
            # attachment disposition and PDF magic. Accept that publisher behavior here,
            # retaining the signature check before writing and the offline PDF parser.
            cac_attachment = bool(roster_month and mime and not MEDIA_TYPE.fullmatch(mime)
                                  and disposition == "attachment")
            if response.status_code != 200 or (mime not in allowed_mime and not cac_attachment):
                if roster_month:
                    reported = {
                        *allowed_mime, "application/download", "application/force-download", "application/x-download",
                        "application/x-msdownload", "application/x-pdf", "text/html", "text/plain",
                    }
                    kind = mime if mime in reported else "other" if mime else "missing"
                    raise CaptureError(f"roster_response_{response.status_code}_{kind}")
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
        return {"status": "downloaded", "bytes": size,
                **({"response_type": "cac_malformed_attachment" if cac_attachment else mime} if roster_month else {})}
    finally:
        fetcher.close()


def compact(text):
    return re.sub(r"\s+", "", text)


def inspect_dashed_folios(text_by_mode):
    cleaned, counts = {}, {}
    for mode, pages in text_by_mode.items():
        matches = [re.subn(rf"[—–-][ \t]*{i}[ \t]*[—–-]", "", page)
                   for i, page in enumerate(pages, 1)]
        cleaned[mode] = [text for text, _ in matches]
        counts[mode] = [count for _, count in matches]
    return cleaned, counts


def annex_coordinates(page):
    from pypdf._text_extraction._layout_mode._fixed_width_page import (
        TextStateManager, recurse_to_target_op, set_font_operation,
    )
    from pypdf.generic import ArrayObject, ContentStream, StreamObject

    lines = set()
    horizontal, vertical = set(), set()
    operators = Counter()
    previous = None

    def location(x, y, matrix):
        a, b, c, d, e, f = matrix
        return round(a * x + c * y + e, 1), round(b * x + d * y + f, 1)

    def segment(first, last):
        if abs(first[0] - last[0]) > 5 and abs(first[1] - last[1]) < 2:
            horizontal.add(first[1])
            lines.add(("h", *first, *last))
        elif abs(first[1] - last[1]) > 5 and abs(first[0] - last[0]) < 2:
            vertical.add(first[0])
            lines.add(("v", *first, *last))

    def inspect(operator, operands, cm, _):
        nonlocal previous
        if operator in {b"m", b"l", b"re", b"S", b"Do", b"f", b"f*"}:
            operators[operator.decode()] += 1
        if operator == b"re":
            x, y, width, height = map(float, operands)
            segment(location(x, y, cm), location(x + width, y + height, cm))
        elif operator in {b"m", b"l"}:
            current = location(*map(float, operands), cm)
            if operator == b"l" and previous is not None:
                segment(previous, current)
            previous = current
        elif operator in {b"S", b"s", b"f", b"f*", b"n"}:
            previous = None

    page.extract_text(visitor_operand_before=inspect)
    contents = page.get("/Contents")
    if contents is not None:
        contents = contents.get_object()
    if not isinstance(contents, (ArrayObject, StreamObject)):
        raise CaptureError("annex_content_missing")
    # Reuse the pinned layout engine before it shifts the blocks left. The
    # table borders and text origins must share the page's coordinate system.
    # Match its byte-based content reader so CID font codes remain intact.
    fonts = page._layout_mode_fonts()
    state = TextStateManager()
    operations = iter(ContentStream(contents, page.pdf, "bytes").operations)
    blocks = []
    for operands, operator in operations:
        if operator in (b"BT", b"q"):
            groups, _ = recurse_to_target_op(operations, state, b"ET" if operator == b"BT" else b"Q", fonts, True)
            blocks.extend(groups)
        elif operator == b"Tf":
            set_font_operation(state, fonts, operands)
        else:
            state.set_state_param(operator, operands)
    return (sorted(vertical), sorted(horizontal, reverse=True), lines, blocks,
            {"line_segments": len(lines), "path_operators": dict(operators), "block_count": len(blocks)})


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


_SHANGHAI_CELLS = (
    (0, 0, 1, 1), (0, 1, 1, 2), (0, 3, 1, 1), (0, 4, 1, 1),
    (1, 0, 4, 1), (1, 1, 2, 1), (1, 2, 1, 1), (1, 3, 1, 1), (1, 4, 1, 1),
    (2, 2, 1, 1), (2, 3, 1, 1), (2, 4, 1, 1),
    (3, 1, 1, 2), (3, 3, 1, 1), (3, 4, 1, 1),
    (4, 1, 1, 2), (4, 3, 1, 1), (4, 4, 1, 1),
)
_SHANGHAI_HEADERS = ("专题", "方向名称", "支持内容", "联系人")


def verify_annex_grid(xs, ys, lines):
    if len(xs) != 6 or len(ys) != 6 or len(lines) != 12:
        raise CaptureError("annex_grid_size")
    owners = {(r, c): (row, col)
              for row, col, height, width in _SHANGHAI_CELLS
              for r in range(row, row + height) for c in range(col, col + width)}

    def covered(axis, fixed, start, end):
        low, high = sorted((start, end))
        intervals = []
        for kind, x1, y1, x2, y2 in lines:
            if kind != axis:
                continue
            first, last, level = (x1, x2, y1) if axis == "h" else (y1, y2, x1)
            a, b = sorted((first, last))
            if abs(level - fixed) <= 0.2 and min(b, high) - max(a, low) > 0.5:
                intervals.append((a, b))
        if not intervals:
            return False
        position = low
        for a, b in sorted(intervals):
            if a > position + 0.5:
                raise CaptureError("annex_partial_grid_edge")
            position = max(position, b)
        if position < high - 0.5:
            raise CaptureError("annex_partial_grid_edge")
        return True

    for row, y in enumerate(ys):
        for col, (left, right) in enumerate(zip(xs, xs[1:])):
            separates = owners.get((row - 1, col)) != owners.get((row, col))
            if covered("h", y, left, right) != separates:
                raise CaptureError("annex_horizontal_merge")
    for col, x in enumerate(xs):
        for row, (top, bottom) in enumerate(zip(ys, ys[1:])):
            separates = owners.get((row, col - 1)) != owners.get((row, col))
            if covered("v", x, bottom, top) != separates:
                raise CaptureError("annex_vertical_merge")


def positioned_text(fragments, *, separate_lines=False):
    lines = []
    for y, left, right, text in sorted(fragments, key=lambda item: (-item[0], item[1])):
        if not lines or lines[-1][0] - y > 1.1:
            lines.append((y, []))
        lines[-1][1].append((left, right, text))
    rendered = []
    for _, pieces in lines:
        text = ""
        previous_right = None
        for left, right, fragment in sorted(pieces):
            if (text and left - previous_right > 1.5 and fragment and not text[-1].isspace()
                    and not fragment[0].isspace() and not _HAN.match(text[-1]) and not _HAN.match(fragment[0])):
                text += " "
            text += fragment
            previous_right = right
        if text.strip():
            rendered.append(text.strip())
    if not rendered:
        return ""
    if separate_lines:
        return " ".join(rendered)
    text, *rest = rendered
    for line in rest:
        text = join_line(text, line)
    return text


def shanghai_annex(case, coordinates, plain, layout):
    xs, ys, lines, operations, diagnostics = coordinates
    verify_annex_grid(xs, ys, lines)
    cells = {(row, col): [] for row, col, _, _ in _SHANGHAI_CELLS}
    boxes = {(row, col): (xs[col], xs[col + width], ys[row + height], ys[row])
             for row, col, height, width in _SHANGHAI_CELLS}
    outside = []
    advance_crossings = 0
    for operation in operations:
        text = operation["text"]
        if not text.strip():
            continue
        x, y, end = (float(operation[k]) for k in ("tx", "ty", "displaced_tx"))
        if operation["flip_sort"] != 1 or not all(map(isfinite, (x, y, end))) or end < x:
            raise CaptureError("annex_invalid_text_position")
        fragment = (y, x, end, text)
        if y > ys[0] or y < ys[-1]:
            outside.append(fragment)
            continue
        origins = [cell for cell, (left, right, low, high) in boxes.items()
                   if low < y < high and left <= x < right]
        if len(origins) != 1:
            position = (round(x, 2), round(y, 2), round(end, 2))
            raise CaptureError(f"annex_cell_position {position} origins={origins}")
        cell, = origins
        advance_crossings += end > boxes[cell][1] + 0.5
        cells[cell].append(fragment)
    texts = {cell: positioned_text(items, separate_lines=cell[1] == 4) for cell, items in cells.items()}
    normalized = {cell: compact(text) for cell, text in texts.items()}
    caption = case["annex_title"]
    outside_inventory = Counter(compact(positioned_text(outside)))
    caption_verified = (compact(plain).startswith(caption) and caption in compact(layout)
                        and any(outside_inventory == Counter(caption + dash + "5" + dash) for dash in "—–-"))
    headers_verified = tuple(normalized[(0, col)] for col in (0, 1, 3, 4)) == _SHANGHAI_HEADERS
    # A PDF glyph's advance can extend past the drawn cell edge. Assign its
    # origin to the physical cell, then verify the full cell order against the
    # independent content-stream reading.
    cell_order = caption + "".join(normalized.values())
    inventory = Counter(cell_order)
    layout_inventory = Counter(compact(layout))
    plain_inventory = Counter(compact(plain))
    anchors_verified = (normalized[(1, 0)] == "制造业智能化发展"
                        and normalized[(1, 1)] == "支持“AI+制造”样板企业梯度培育"
                        and normalized[(1, 3)].endswith("经评估后给予分档支持。")
                        and "2025年度1月1日-12月31日" in normalized[(4, 3)])
    checks = {
        "caption": caption_verified, "headers": headers_verified, "all_cells": all(normalized.values()),
        "stream_order": cell_order == compact(plain), "inventory": inventory == plain_inventory == layout_inventory,
        "scope_anchors": anchors_verified,
    }
    if not all(checks.values()):
        failure = [name for name, passed in checks.items() if not passed]
        position = next((i for i, (a, b) in enumerate(zip(cell_order, compact(plain))) if a != b), None)
        raise CaptureError(f"annex_checks {failure} first_order_difference={position} "
                           f"missing_characters={(plain_inventory - inventory).total()} "
                           f"extra_characters={(inventory - plain_inventory).total()} "
                           f"outside_characters={outside_inventory.total()} "
                           f"cell_lengths={[len(value) for value in normalized.values()]}")
    paragraphs = [case["annex_title"], " | ".join(_SHANGHAI_HEADERS), texts[(1, 0)], texts[(1, 1)]]
    paragraphs.extend(" | ".join(texts[(row, col)] for col in (direction, 3, 4))
                      for row, direction in ((1, 2), (2, 2), (3, 1), (4, 1)))
    return paragraphs, {**diagnostics, "bound_cells": len(cells), "rows": 4,
                        "advance_crossings": advance_crossings}


def compose_paragraphs(case, layout):
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
    return paragraphs, spans


def normalize(case, modes):
    plain, layout = ([without_folio(text, label) for text, label in zip(modes[mode], case["page_labels"], strict=True)]
                     for mode in ("plain", "layout"))
    if list(map(compact, plain)) != list(map(compact, layout)):
        raise CaptureError("text_order_disagrees")
    paragraphs, spans = compose_paragraphs(case, layout)
    if (compact("".join(paragraphs)) != compact("".join(plain))
            or not compact(paragraphs[-1]).endswith(compact(case["ending"]))
            or compact(case["date"]) not in compact(layout[0])):
        raise CaptureError("normalized_text_not_verified")
    return paragraphs, spans


def normalize_shanghai(case, cleaned, counts, coordinates, prose):
    plain, layout = cleaned["plain"], cleaned["layout"]
    annex_index = case["annex_page"] - 1
    if any(count != 1 for per_mode in counts.values() for count in per_mode):
        raise CaptureError("folio_not_verified")
    if any(compact(a) != compact(b) for index, (a, b) in enumerate(zip(plain, layout, strict=True))
           if index != annex_index):
        raise CaptureError("text_order_disagrees")
    if compact("".join(case["footer"])) != compact(plain[-1]):
        raise CaptureError("shanghai_edition_markers")
    annex, diagnostics = shanghai_annex(case, coordinates, plain[annex_index], layout[annex_index])
    if (not isinstance(prose, list) or len(prose) != case["prose_count"]
            or not all(isinstance(text, str) and compact(text) for text in prose)
            or compact(prose[-1]) != case["annex_intro"]):
        raise CaptureError("publisher_prose_not_verified")
    paragraphs = [case["opening"], case["document_number"], case["title"], *prose]
    pages = list(map(compact, plain[:annex_index]))
    body = "".join(pages)
    page_ends = list(accumulate(map(len, pages)))
    spans = []
    offset = 0
    for index, paragraph in enumerate(paragraphs, 1):
        text = compact(paragraph)
        if not body.startswith(text, offset):
            difference = next((i for i, (a, b) in enumerate(zip(text, body[offset:])) if a != b), None)
            raise CaptureError(f"publisher_prose_not_in_pdf paragraph={index} difference={difference}")
        start = bisect_right(page_ends, offset) + 1
        offset += len(text)
        end = bisect_left(page_ends, offset) + 1
        spans.append((start, end))
    if offset != len(body) or [(start, end) for start, end in spans if start != end] != [(2, 3), (3, 4)]:
        raise CaptureError("publisher_prose_page_spans")
    paragraphs.extend([*annex, *case["footer"]])
    spans.extend([(case["annex_page"], case["annex_page"])] * len(annex))
    spans.extend([(case["pages"], case["pages"])] * len(case["footer"]))
    return paragraphs, spans, diagnostics


def write_capture(case, path, output, paragraphs, spans, *, body_complete=True):
    from chinese_collector import edition_snapshot
    from run import load_manifest, save_manifest
    from scraper.fetcher import utcnow
    from scraper.normalize import build_record, to_markdown
    from scraper.registry import load_registry

    entry = next(e for e in load_registry() if e["id"] == case["source"])
    meta, text = build_record({"title": case.get("record_title", case["title"]), "date": case["date"],
                               "body_paragraphs": paragraphs, "document_number": case["document_number"],
                               "issuing_institution": case["publisher"]},
                              entry, case["pdf"], utcnow().isoformat(), body_complete=body_complete)
    if snapshot := edition_snapshot({"url": case["pdf"], "archive_timestamp": case.get("archive_timestamp")}):
        meta["archive_snapshot"] = snapshot
    meta.update(source_genre=case["source_genre"], attachment_parent=case["parent"],
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
    return meta


def roster_page(coordinates, expected_rows, expected_columns):
    xs, ys, lines, operations, _ = coordinates
    if (expected_columns not in {6, 7} or len(xs) != expected_columns + 1
            or len(ys) != expected_rows + 1 or len(lines) != len(xs) + len(ys)):
        raise CaptureError("roster_grid_shape")
    for axis, x1, y1, x2, y2 in lines:
        if axis == "h" and (abs(min(x1, x2) - xs[0]) > 0.6 or abs(max(x1, x2) - xs[-1]) > 0.6):
            raise CaptureError("roster_horizontal_grid_span")
        if axis == "v" and (abs(min(y1, y2) - ys[-1]) > 0.6 or abs(max(y1, y2) - ys[0]) > 0.6):
            raise CaptureError("roster_vertical_grid_span")
    cells = [[[] for _ in range(expected_columns)] for _ in range(expected_rows)]
    row_edges = [-edge for edge in ys]
    before, after = [], []
    advance_crossings = 0
    for operation in operations:
        text = operation["text"]
        if not text.strip():
            continue
        x, y, end = (float(operation[key]) for key in ("tx", "ty", "displaced_tx"))
        if operation["flip_sort"] != 1 or not all(map(isfinite, (x, y, end))) or end < x:
            raise CaptureError("roster_invalid_text_position")
        fragment = (y, x, end, text)
        if y >= ys[0] or y <= ys[-1]:
            (before if y >= ys[0] else after).append(fragment)
            continue
        column = bisect_right(xs, x) - 1
        row = bisect_right(row_edges, -y) - 1
        if not 0 <= column < len(xs) - 1 or not 0 <= row < len(ys) - 1 or abs(y - ys[row]) <= 0.1:
            raise CaptureError("roster_cell_position")
        cells[row][column].append(fragment)
        advance_crossings += end > xs[column + 1] + 0.5
    return (positioned_text(before, separate_lines=True),
            [[positioned_text(cell) for cell in row] for row in cells],
            positioned_text(after, separate_lines=True), advance_crossings)


def read_roster(case, reader):
    headers = ("服务名称", "备案编号", "应用名称", "上线编号", "序号", "属地")
    text_bytes = 0
    counts = []
    if len(reader.pages) != case["pages"]:
        raise CaptureError("roster_page_count")
    serials = []
    paragraphs, spans = [], []
    columns = case.get("columns_by_page", (6,) * case["pages"])
    for page_number, (page, row_count, column_count) in enumerate(
            zip(reader.pages, case["rows_by_page"], columns, strict=True), 1):
        plain, layout = (page.extract_text(extraction_mode=mode) or "" for mode in ("plain", "layout"))
        text_bytes += len(plain.encode("utf-8")) + len(layout.encode("utf-8"))
        if text_bytes > 3 * 1024 * 1024:
            raise CaptureError("text_size_cap")
        ordered, visual = compact(plain), compact(layout)
        xs, ys, lines, operations, diagnostics = annex_coordinates(page)
        positioned = [block for block in operations if block["text"].strip()]
        coordinate_inventory = Counter(compact("".join(block["text"] for block in positioned)))
        starts_table = page_number in case["header_pages"]
        before, rows, after, crossings = roster_page(
            (xs, ys, lines, operations, diagnostics), row_count + starts_table, column_count)
        if starts_table:
            serials.append([])
        data_rows = rows[1:] if starts_table else rows
        serials[-1].extend(compact(row[0]) for row in data_rows)
        row_order = compact(before + "".join(cell for row in rows for cell in row) + after)
        data_order = compact("".join(cell for row in data_rows for cell in row))
        table_header = [compact(cell) for cell in rows[0]] if starts_table else []
        inventory = Counter(ordered)
        if (not ordered or any(char in ordered + visual for char in ("\ufffd", "\x00"))
                or inventory != Counter(visual) or inventory != coordinate_inventory
                or inventory != Counter(row_order) or not all(cell for row in rows for cell in row)
                or not data_order or ordered.count(data_order) != 1
                or any(" | " in cell for row in rows for cell in row)
                or any(not cell or cell not in ordered for cell in table_header)
                or (table_header and table_header[:2] != ["序号", "属地"])):
            raise CaptureError(f"roster_page_fidelity_{page_number}")
        page_paragraphs = [text for text in (before, *(" | ".join(row) for row in rows), after) if text]
        paragraphs.extend(page_paragraphs)
        spans.extend([(page_number, page_number)] * len(page_paragraphs))
        horizontal_lengths = Counter(round(abs(x2 - x1), 1) for kind, x1, _, x2, _ in lines if kind == "h")
        vertical_lengths = Counter(round(abs(y2 - y1), 1) for kind, _, y1, _, y2 in lines if kind == "v")
        origins = Counter(round(float(block["tx"]), 1) for block in positioned)
        counts.append({"characters": len(ordered), "layout_characters": len(visual),
                       "same_order": ordered == visual, "same_characters": Counter(ordered) == Counter(visual),
                       "replacement_characters": ordered.count("\ufffd") + visual.count("\ufffd"),
                       "header_matches": [header in visual for header in headers],
                       "coordinate_inventory_matches": coordinate_inventory == Counter(ordered),
                       "row_inventory_matches": Counter(row_order) == coordinate_inventory,
                       "row_order_matches_stream": row_order == ordered,
                       "row_order_matches_layout": row_order == visual,
                       "data_order_in_stream": ordered.count(data_order) == 1 if data_order else False,
                       "header_cells_in_stream": all(cell in ordered for cell in table_header),
                       "header_index_and_region": table_header[:2] == ["序号", "属地"] if table_header else None,
                       "all_cells_present": all(cell for row in rows for cell in row),
                       "data_rows": len(data_rows), "header_lengths": list(map(len, table_header)),
                       "advance_crossings": crossings, "outside_characters": len(compact(before + after)),
                       "grid": {**diagnostics, "x_boundary_count": len(xs), "y_boundary_count": len(ys),
                                "x_boundaries": xs[:16], "y_boundaries": ys[:40],
                                "horizontal_lengths": horizontal_lengths.most_common(12),
                                "vertical_lengths": vertical_lengths.most_common(12),
                                "common_text_origins": origins.most_common(12)}})
    table_diagnostics = []
    for values, (first, last) in zip(serials, case["serial_ranges"], strict=True):
        numbers = [int(value) for value in values if re.fullmatch(r"[0-9]{1,5}", value)]
        if len(numbers) != len(values) or numbers != list(range(first, last + 1)):
            raise CaptureError("roster_serial_range")
        start = numbers[0] if numbers else 0
        table_diagnostics.append({"rows": len(values), "integer_serials": len(numbers),
                                  "first_serial": start, "last_serial": numbers[-1] if numbers else 0,
                                  "sequential": len(numbers) == len(values) and numbers == list(
                                      range(start, start + len(numbers)))})
    return ({"status": "roster_verified", "pages": len(counts), "page_diagnostics": counts,
             "tables": table_diagnostics}, paragraphs, spans)


def independent_pdf_text(path):
    import subprocess
    from tempfile import TemporaryDirectory

    results = []
    with TemporaryDirectory() as folder:
        for command in (("pdftotext", "-enc", "UTF-8", "-layout", str(path), "-"),
                        ("pdfimages", "-list", str(path))):
            target = Path(folder) / command[0]
            with target.open("xb") as stream:
                subprocess.run(command, stdout=stream, stderr=subprocess.PIPE, check=True, timeout=90)
            with target.open("rb") as stream:
                data = stream.read(3 * 1024 * 1024 + 1)
            if len(data) > 3 * 1024 * 1024:
                raise CaptureError("independent_text_size_cap")
            results.append(data.decode("utf-8"))
    pages = results[0].split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    images = Counter()
    for line in results[1].splitlines()[2:]:
        if not line.strip():
            continue
        match = re.match(r"^\s*(\d+)\s+\d+\s+(?:image|mask|smask)\s", line)
        if match is None or not 1 <= int(match[1]) <= len(pages):
            raise CaptureError("unexpected_image_inventory")
        images[int(match[1])] += 1
    return pages, images


def boundary_folios(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return [unicodedata.normalize("NFKC", lines[index]) for index in dict.fromkeys((0, len(lines) - 1))
            if lines and re.fullmatch(r"[0-9IVXLCDMⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+", lines[index])]


def inspect_attachment(case, reader, path, output=None):
    if not 1 <= len(reader.pages) <= 250 or case.get("pages", len(reader.pages)) != len(reader.pages):
        raise CaptureError("unexpected_page_count")
    all_pages = case.get("inspect_all_pages")
    other, images = independent_pdf_text(path) if all_pages else (None, None)
    if other is not None and len(other) != len(reader.pages):
        raise CaptureError("independent_page_count")
    numbers = (range(len(reader.pages)) if all_pages
               else sorted({0, min(9, len(reader.pages) - 1), len(reader.pages) // 2, len(reader.pages) - 1}))
    measurements, text = [], []
    for number in numbers:
        page = reader.pages[number]
        plain = page.extract_text(extraction_mode="plain") or ""
        layout = page.extract_text(extraction_mode="layout") or ""
        page_text = compact(plain)
        measurement = {"page": number + 1, "plain_characters": len(plain), "layout_characters": len(layout),
                       "same_character_inventory": Counter(compact(plain)) == Counter(compact(layout)),
                       "same_character_order": compact(plain) == compact(layout),
                       "chinese_characters": len(re.findall(r"[\u3400-\u9fff]", plain)),
                       "replacement_characters": plain.count("\ufffd") + layout.count("\ufffd")}
        if case.get("inspect_grid"):
            xs, ys, lines, _, diagnostics = annex_coordinates(page)
            spans = Counter(axis for axis, *_ in lines)
            measurement["grid"] = {"vertical_boundaries": len(xs), "horizontal_boundaries": len(ys),
                                   "segments_by_axis": dict(spans), "text_blocks": diagnostics["block_count"]}
        if other is not None:
            comparison = compact(other[number])
            missing = Counter(compact(plain)) - Counter(comparison)
            extra = Counter(comparison) - Counter(compact(plain))
            measurement.update(independent_characters=len(other[number]),
                               independent_inventory_matches=not missing and not extra,
                               independent_order_matches=compact(plain) == comparison,
                               missing_codepoints=sorted((ord(char), count) for char, count in missing.items()),
                               extra_codepoints=sorted((ord(char), count) for char, count in extra.items()),
                               independent_replacement_characters=other[number].count("\ufffd"),
                               images=images[number + 1])
            folios = [boundary_folios(value) for value in (plain, layout, other[number])]
            common = set.intersection(*(set(values) for values in folios))
            measurement.update(boundary_folios=folios, rotation=page.rotation,
                               page_width=float(page.mediabox.width), page_height=float(page.mediabox.height))
            if len(common) == 1 and all(len(values) == 1 for values in folios):
                label = common.pop()
                stripped = [compact(without_folio(value, label)) for value in (plain, layout, other[number])]
                measurement.update(shared_boundary=label, same_order_without_boundary=stripped[0] == stripped[1],
                                   independent_order_without_boundary=stripped[0] == stripped[2])
                page_text = stripped[0]
            if not measurement["same_character_inventory"]:
                rotated = page.extract_text(extraction_mode="layout", layout_mode_strip_rotated=False) or ""
                measurement.update(rotated_layout_inventory_matches=Counter(compact(plain)) == Counter(compact(rotated)),
                                   rotated_layout_order_matches=compact(plain) == compact(rotated))
        text.append(page_text)
        measurements.append(measurement)
    staged = {}
    if output is not None:
        if (not case.get("inspection_capture") or other is None
                or not all(row["independent_inventory_matches"] for row in measurements)
                or compact(case["record_title"]) not in compact(other[0])):
            raise CaptureError("incomplete_text_not_verified")
        paragraphs, spans = [], []
        for number, page in enumerate(other, 1):
            lines = [line.strip() for line in page.splitlines() if line.strip()]
            paragraphs.extend(lines)
            spans.extend([(number, number)] * len(lines))
        meta = write_capture(case, path, output, paragraphs, spans, body_complete=False)
        staged = {"staged": 1, "body_complete": False, "paragraphs": len(paragraphs), "content_hash": meta["content_hash"]}
    return {"status": "attachment_measured", "pages": len(reader.pages), "samples": measurements,
            "column_labels": dict(Counter(label for page in text for label in re.findall(r"专栏(\d+)", page))),
            "figure_labels": dict(Counter(label for page in text for label in re.findall(r"图(\d+)", page))),
            "attachment_sha256": sha256(path.read_bytes()).hexdigest(), **staged}


def extract(case, quarantine, output=None):
    from pypdf import PdfReader

    path = quarantine / "attachment.pdf"
    if case.get("pdf_sha256") and sha256(path.read_bytes()).hexdigest() != case["pdf_sha256"]:
        raise CaptureError("roster_pdf_changed" if "roster_month" in case else "pdf_changed")
    with path.open("rb") as stream:
        reader = PdfReader(stream, strict=True)
        if reader.is_encrypted:
            raise CaptureError("encrypted_pdf")
        if case.get("probe_only"):
            if output is not None and not case.get("inspection_capture"):
                raise CaptureError("inspection_not_capture")
            return inspect_attachment(case, reader, path, output)
        if "roster_month" in case:
            measurement, paragraphs, spans = read_roster(case, reader)
            staged = {}
            if output is not None:
                meta = write_capture(case, path, output, paragraphs, spans)
                staged = {"staged": 1, "paragraphs": len(paragraphs), "content_hash": meta["content_hash"],
                          "paragraphs_by_page": dict(Counter(start for start, _ in spans))}
            return {**measurement, "attachment_sha256": case["pdf_sha256"], **staged}
        if len(reader.pages) != case["pages"]:
            raise CaptureError("unexpected_page_count")
        text_by_mode = {}
        for mode in ("plain", "layout"):
            pages = [page.extract_text(extraction_mode=mode) or "" for page in reader.pages]
            if sum(len(page.encode("utf-8")) for page in pages) > 3 * 1024 * 1024:
                raise CaptureError("text_size_cap")
            text_by_mode[mode] = pages
        coordinates = annex_coordinates(reader.pages[case["annex_page"] - 1]) if "annex_page" in case else None
    modes = {mode: score_pages(case, pages) for mode, pages in text_by_mode.items()}
    if output is not None and not case.get("dashed_folios"):
        if "page_labels" not in case or any(
                m["nonempty_pages"] != case["pages"] or not m["opening_found"] or m["replacement_characters"]
                or m["anchor_hits"] != len(case["anchors"]) or m["cross_page_hits"] != len(case["cross_page"])
                for m in modes.values()):
            raise CaptureError("edition_quality_gate")
        paragraphs, spans = normalize(case, text_by_mode)
    page_modes_agree = [compact(plain) == compact(layout) for plain, layout in
                        zip(text_by_mode["plain"], text_by_mode["layout"], strict=True)]
    layout_diagnostics = {}
    if case.get("dashed_folios"):
        cleaned, counts = inspect_dashed_folios(text_by_mode)
        page_pairs = list(zip(cleaned["plain"], cleaned["layout"], strict=True))
        parent_text = (quarantine / "parent-paragraphs.json").read_bytes()
        if len(parent_text) > 64 * 1024:
            raise CaptureError("publisher_prose_size")
        paragraphs, spans, table_grid = normalize_shanghai(case, cleaned, counts, coordinates, json.loads(parent_text))
        layout_diagnostics = {
            "folio_removals": counts,
            "page_modes_agree_without_folios": [compact(a) == compact(b) for a, b in page_pairs],
            "page_character_inventories_agree": [Counter(compact(a)) == Counter(compact(b)) for a, b in page_pairs],
            "cross_page_without_folios": {mode: score_pages(case, pages)["cross_page_hits"]
                                          for mode, pages in cleaned.items()},
            "table_grid": table_grid,
            "verified_paragraphs": len(paragraphs),
            "verified_cross_page_paragraphs": sum(first != last for first, last in spans),
        }
    staged = {}
    if output is not None:
        write_capture(case, path, output, paragraphs, spans)
        staged = {"staged": 1, "paragraphs": len(paragraphs),
                  "cross_page_paragraphs": sum(first != last for first, last in spans)}
    return {"status": "measured", "pages": case["pages"], "page_modes_agree": page_modes_agree,
            "expected_anchors": len(case["anchors"]),
            "expected_cross_page": len(case["cross_page"]), "modes": modes, **layout_diagnostics, **staged}


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
        detail = {"status": "pdf_capture_failed", "phase": args.phase,
                  "reason": str(error) if isinstance(error, CaptureError) else type(error).__name__}
        if not isinstance(error, CaptureError):
            detail["frames"] = [(Path(frame.filename).name, frame.name, frame.lineno)
                                for frame in extract_tb(error.__traceback__)[-4:]]
        print(json.dumps(detail))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
