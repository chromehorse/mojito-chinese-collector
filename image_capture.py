"""Inspect registered newspaper graphics off-device; never certify OCR as source text."""

import argparse
from base64 import b64encode
from collections import defaultdict
from contextlib import redirect_stderr, redirect_stdout
import csv
from hashlib import sha1, sha256
import json
import os
from pathlib import Path
import struct
import subprocess
from time import monotonic
from urllib.parse import urljoin, urlsplit
import warnings
import zlib


HOST = "paper.people.com.cn"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PREVIEW_BYTES = 3 * 1024 * 1024
MAX_PIXELS = 4_000_000
CASES = {
    "safety_jan05": ("202601/05", "30129344", "39c693c3-a05f-4c9a-862f-abefe7dcda9c",
                     "6e0788d34b27e624500ef30bee45e10e49f5e2e3", "加强安全监管，促进人工智能健康发展（法治聚焦）", "本报记者金歆"),
    "industry_jan22": ("202601/22", "30134727", "e84276df-3f11-45e8-a7e7-d1600c2e92d5",
                       "30c647fdd56c33e5736b9ecaf72b56f6e2be29e2", "人工智能产业亮点纷呈（权威发布）", "本报记者王政、刘温馨"),
    "patents_jan24": ("202601/24", "30135371", "9bf19652-ea7a-47a9-ab3d-d765955758f3",
                      "d53a170baf0e03656775c9fd23ba7f777dce740c", "人工智能专利有效量居全球前列（权威发布）", "本报记者谷业凯"),
    "models_feb02": ("202602/02", "30137411", "c46f9fd7-5186-4a95-abc7-1346e6206ca8",
                     "a13d5edc7eb4fdf08190aa85bff50ce8b9ccbd69", "人工智能大模型要敢于持续“摸高”（“咖”说科技）", "闫俊杰"),
    "agents_feb14": ("202602/14", "30140908", "2f3b3fd1-13d3-447a-8f54-4987a2613966",
                     "5e46299d6273934935d2c9c6ad2025a028ec76fd", "AI开始“社交”，对人类意味着什么？（瞰前沿）", "本报记者孙亚慧"),
    "companions_mar02": ("202603/02", "30142947", "768e459d-7f26-4960-8d6b-d8a77e400929",
                         "d8464e30f3e0cc21d924bed70fc4d40f910736f6", "用更智慧的方式打开AI陪伴（创新谈）", "王伟怡"),
    "payments_mar17": ("202603/17", "30145546", "52e7f91d-c716-4aff-bc02-ac1d3f865e63",
                       "bb1c0b0f96d4dbdc8da7e0c28e1957dfe87761a3", "当支付遇到AI，我们期待什么？（财经观）", "王观"),
}


class CaptureError(ValueError):
    pass


def edition(name):
    day, article, picture, body_hash, title, author = CASES[name]
    return {"url": f"https://{HOST}/rmrb/pc/content/{day}/content_{article}.html",
            "image": f"https://{HOST}/rmrb/pc/pic/{day}/{picture}.jpg.2",
            "body_hash": body_hash, "title": title, "author": author,
            "published": f"{day[:4]}-{day[4:6]}-{day[-2:]}"}


def fetch(name, quarantine):
    import httpx
    from pd_epaper import ENTRY, PrintFetcher, parse_collection_article
    from scraper.adapters._common import clean_tree
    from scraper.normalize import build_body

    case = edition(name)
    fetcher = PrintFetcher(cache_dir=Path("/tmp/image-parent"), strict_html_hosts={HOST: {}},
                           transport=httpx.HTTPTransport(trust_env=False))
    try:
        parent = fetcher.fetch(case["url"], [HOST])
        if not parent.ok or parent.final_url != case["url"]:
            raise CaptureError("parent_not_available")
        entry = {**ENTRY, "recipe": {**ENTRY["recipe"]}}
        parsed = parse_collection_article(parent.text, entry, case["url"])
        _, original_body = build_body(parsed["body_paragraphs"])
        if (sha1(original_body.encode("utf-8")).hexdigest() != case["body_hash"]
                or parsed["title"] != case["title"] or parsed["date"] != case["published"]
                or parsed["author"] != case["author"]):
            raise CaptureError("parent_edition_changed")
        images = clean_tree(parent.text).css("#ozoom img")
        if len(images) != 1 or urljoin(case["url"], images[0].attributes.get("src", "")) != case["image"]:
            raise CaptureError("publisher_image_not_verified")
        target = urlsplit(case["image"])
        if (target.scheme != "https" or target.netloc != HOST or target.query or target.fragment
                or not fetcher._robots_allows(case["image"])):
            raise CaptureError("image_origin_or_crawl_policy")
        fetcher._polite_wait(case["image"])
        path = quarantine / (name + ".jpg")
        size, digest, deadline = 0, sha256(), monotonic() + 60
        with fetcher._client.stream("GET", case["image"], follow_redirects=False,
                                    headers={"Accept": "image/jpeg", "Accept-Encoding": "identity"}) as response:
            mime = response.headers.get("content-type", "").partition(";")[0].strip().lower()
            if response.status_code != 200 or mime not in {"image/jpeg", "application/octet-stream"}:
                raise CaptureError("unexpected_image_response")
            with path.open("xb") as stream:
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    if size == 0 and not chunk.startswith(b"\xff\xd8\xff"):
                        raise CaptureError("invalid_jpeg_header")
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES or monotonic() > deadline:
                        raise CaptureError("image_size_or_time_cap")
                    digest.update(chunk)
                    stream.write(chunk)
        if not size:
            raise CaptureError("empty_image")
        caption = clean_tree(images[0].attributes.get("data-original-title", "")).text(separator=" ", strip=True)
        metadata = {**case, "image_sha256": digest.hexdigest(), "fetched": parent.fetched_at,
                    "credit": caption, "entry": entry}
        (quarantine / (name + ".json")).write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        return {"status": "downloaded", "images": 1, "bytes": size}
    finally:
        fetcher.close()


def validate_preview(data):
    if len(data) > MAX_PREVIEW_BYTES or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise CaptureError("preview_type_or_size")
    offset, kinds, dimensions, compressed = 8, [], None, []
    while offset < len(data):
        if offset + 12 > len(data):
            raise CaptureError("preview_chunk_boundary")
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        end = offset + 12 + length
        if end > len(data):
            raise CaptureError("preview_chunk_size")
        kind, value = data[offset + 4:offset + 8], data[offset + 8:end - 4]
        if kind not in {b"IHDR", b"IDAT", b"IEND"} or zlib.crc32(kind + value) != struct.unpack(">I", data[end - 4:end])[0]:
            raise CaptureError("preview_metadata_or_checksum")
        if kind == b"IHDR":
            if kinds or length != 13:
                raise CaptureError("preview_header")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", value)
            if (not 1 <= width <= 5000 or not 1 <= height <= 5000 or width * height > MAX_PIXELS
                    or (depth, color, compression, filtering, interlace) != (8, 2, 0, 0, 0)):
                raise CaptureError("preview_pixel_format")
            dimensions = (width, height)
        elif kind == b"IDAT":
            compressed.append(value)
        elif kind == b"IEND" and (length or end != len(data)):
            raise CaptureError("preview_ending")
        kinds.append(kind)
        offset = end
    if not dimensions or kinds[-1:] != [b"IEND"] or b"IDAT" not in kinds:
        raise CaptureError("preview_incomplete")
    row_bytes = dimensions[0] * 3 + 1
    expected = row_bytes * dimensions[1]
    decoder = zlib.decompressobj()
    raster = decoder.decompress(b"".join(compressed), expected + 1)
    if (len(raster) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
            or any(raster[offset] > 4 for offset in range(0, expected, row_bytes))):
        raise CaptureError("preview_raster_size_or_filters")
    return dimensions


def ocr_lines(path, mode, folder, dimensions):
    output = folder / f"ocr-{mode}.tsv"
    with output.open("xb") as stream, (folder / f"ocr-{mode}.stderr").open("xb") as errors:
        subprocess.run(["tesseract", str(path), "stdout", "-l", "chi_sim+eng", "--psm", str(mode), "tsv"],
                       stdout=stream, stderr=errors, check=True, timeout=60)
    if output.stat().st_size > 1024 * 1024:
        raise CaptureError("ocr_size_cap")
    grouped = defaultdict(list)
    with output.open(encoding="utf-8") as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
            if row["level"] != "5" or not row["text"].strip():
                continue
            x, y, width, height = (int(row[key]) for key in ("left", "top", "width", "height"))
            if not (0 <= x < dimensions[0] and 0 <= y < dimensions[1]
                    and 0 < width <= dimensions[0] - x and 0 < height <= dimensions[1] - y):
                raise CaptureError("ocr_coordinates")
            key = tuple(row[key] for key in ("page_num", "block_num", "par_num", "line_num"))
            grouped[key].append((x, y, width, height, float(row["conf"]), row["text"]))
    return [f"Candidate OCR, mode {mode}, bounds {min(w[0] for w in words)},{min(w[1] for w in words)},"
            f"{max(w[0]+w[2] for w in words)},{max(w[1]+w[3] for w in words)}, "
            f"minimum confidence {min(w[4] for w in words):.1f}: " + " ".join(w[5] for w in words)
            for words in grouped.values()]


def inspect(name, quarantine, output):
    from PIL import Image, ImageOps
    from scraper.normalize import build_record, to_markdown

    metadata_path = quarantine / (name + ".json")
    if metadata_path.stat().st_size > 128 * 1024:
        raise CaptureError("parent_metadata_size")
    meta = json.loads(metadata_path.read_text(encoding="utf-8"))
    case = edition(name)
    if any(meta.get(key) != value for key, value in case.items()):
        raise CaptureError("parent_binding")
    path = quarantine / (name + ".jpg")
    if path.stat().st_size > MAX_IMAGE_BYTES or sha256(path.read_bytes()).hexdigest() != meta["image_sha256"]:
        raise CaptureError("image_changed")
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    with Image.open(path, formats=["JPEG"]) as source:
        if (source.format != "JPEG" or getattr(source, "n_frames", 1) != 1 or source.width * source.height > MAX_PIXELS
                or not 1 <= source.width <= 5000 or not 1 <= source.height <= 5000):
            raise CaptureError("unsupported_image_dimensions")
        source.verify()
    folder = output.parent / ("image-work-" + name)
    folder.mkdir()
    preview = folder / "preview.png"
    with Image.open(path, formats=["JPEG"]) as source:
        source.load()
        rgb = ImageOps.exif_transpose(source).convert("RGB")
        clean = Image.new("RGB", rgb.size)
        clean.paste(rgb)
        clean.save(preview, format="PNG", exif=b"", icc_profile=None, pnginfo=None)
    data = preview.read_bytes()
    dimensions = validate_preview(data)
    candidates = [line for mode in (11, 6) for line in ocr_lines(preview, mode, folder, dimensions)]
    paragraphs = ["INCOMPLETE IMAGE INSPECTION. Candidate OCR is derived text, not a verified article edition.",
                  "Original article body hash: " + case["body_hash"], "Original image URL: " + case["image"],
                  "Original image SHA-256: " + meta["image_sha256"],
                  "Decoded RGB preview SHA-256: " + sha256(data).hexdigest(),
                  f"Decoded preview dimensions: {dimensions[0]} x {dimensions[1]}",
                  "Publisher image credit: " + (meta["credit"] or "not printed"), *candidates,
                  "![Isolated decoded publisher graphic](data:image/png;base64," + b64encode(data).decode("ascii") + ")"]
    parsed = {"title": case["title"], "date": case["published"], "author": case["author"],
              "document_number": None, "body_paragraphs": paragraphs, "attachments": []}
    record, body = build_record(parsed, meta["entry"], case["url"], meta["fetched"], body_complete=False)
    markdown = to_markdown(record, body).encode("utf-8")
    if len(markdown) > 5 * 1024 * 1024:
        raise CaptureError("inspection_record_size")
    target = output / "_normalized" / (record["record_id"] + ".md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(markdown)
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"records": {}}
    fields = "source canonical_url title_zh published content_hash docnum_hash republication_of body_complete fetched".split()
    manifest["records"][record["record_id"]] = {key: record[key] for key in fields}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return {"status": "inspection_staged", "images": 1, "width": dimensions[0], "height": dimensions[1],
            "preview_bytes": len(data), "ocr_lines": len(candidates), "body_complete": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("fetch", "inspect"))
    parser.add_argument("case", choices=(*CASES, "all"))
    parser.add_argument("quarantine", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.phase == "inspect" and args.output is None:
            raise CaptureError("inspection_output_required")
        names = list(CASES) if args.case == "all" else [args.case]
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            result = [(fetch(name, args.quarantine) if args.phase == "fetch"
                       else inspect(name, args.quarantine, args.output)) for name in names]
        print(json.dumps({"status": "ok", "results": result}))
        return 0
    except Exception as error:
        print(json.dumps({"status": "image_inspection_failed", "phase": args.phase,
                          "reason": str(error) if isinstance(error, CaptureError) else type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
