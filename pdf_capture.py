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
    from chinese_collector import miit_pdf_links
    from scraper.adapters._common import clean_tree, collect_attachment_links, complete_body_paragraphs, first_text
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
        if selector := case.get("prose_selector"):
            paragraphs = complete_body_paragraphs(tree, selector)
            markers = [i for i, text in enumerate(paragraphs) if compact(text) == case["annex_intro"]]
            if markers != [case["prose_count"] - 1]:
                raise CaptureError("publisher_prose_boundary_not_verified")
            (quarantine / "parent-paragraphs.json").write_text(
                json.dumps(paragraphs[:case["prose_count"]], ensure_ascii=False), encoding="utf-8")
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


def write_capture(case, path, output, paragraphs, spans):
    from run import load_manifest, save_manifest
    from scraper.fetcher import utcnow
    from scraper.normalize import build_record, to_markdown
    from scraper.registry import load_registry

    entry = next(e for e in load_registry() if e["id"] == case["source"])
    meta, text = build_record({"title": case.get("record_title", case["title"]), "date": case["date"],
                               "body_paragraphs": paragraphs, "document_number": case["document_number"],
                               "issuing_institution": case["publisher"]},
                              entry, case["pdf"], utcnow().isoformat())
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
