"""Parsed article -> normalized markdown record (YAML frontmatter + locator body).
Normalization is deterministic: locators derive from paragraph order only, so
they are stable across re-runs (they are the downstream cite)."""
import hashlib
import re

# Collective pen names file as institutional, never signed-essay.
PEN_NAMES = {"文平", "任仲平", "仲音", "钟声", "本报评论员", "新华社评论员", "本刊评论员", "编辑部"}

_HANZI_NAME_RE = re.compile(r"^[一-鿿·]{2,4}$")
_INSTITUTIONAL_HINTS = ("评论员", "编辑部", "课题组", "记者", "本报", "本刊")

_FW_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def sha1_hex(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def normalize_docnum(docnum):
    """Canonical form for cross-site matching: unified brackets, ascii digits, no spaces."""
    if not docnum:
        return None
    s = docnum.translate(_FW_DIGITS)
    s = s.replace("[", "〔").replace("]", "〕").replace("［", "〔").replace("］", "〕")
    return re.sub(r"\s+", "", s)


def is_personal_name(author):
    if not author:
        return False
    a = author.strip()
    if a in PEN_NAMES or any(h in a for h in _INSTITUTIONAL_HINTS):
        return False
    return bool(_HANZI_NAME_RE.match(a))


def decide_genre(entry, document_number, author_printed):
    if document_number:
        return "decree"
    if is_personal_name(author_printed):
        return "signed-essay"
    return entry["source_genre_default"]


def _norm_para(p):
    return re.sub(r"\s+", " ", p).strip()


def build_body(paragraphs):
    """Locator-prefixed body. Deterministic given the same paragraph list."""
    paras = [_norm_para(p) for p in paragraphs if _norm_para(p)]
    lines = [f"[P{i:03d}] {p}" for i, p in enumerate(paras, 1)]
    return "\n\n".join(lines), "\n".join(paras)


def _yaml_value(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        if not v:
            return "[]"
        return None  # handled by caller as block sequence
    s = str(v)
    if re.search(r'[:#\[\]{}"\'\n]|^\s|\s$', s) or s in ("null", "true", "false", ""):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def build_record(parsed, entry, url, fetched_at, body_complete=True, extra_attachments=None):
    """Assemble the normalized record dict + markdown text."""
    body_md, body_plain = build_body(parsed["body_paragraphs"])
    content_hash = sha1_hex(body_plain)
    docnum = normalize_docnum(parsed.get("document_number"))
    record_id = f"{entry['id']}__{content_hash[:8]}"
    attachments = list(parsed.get("attachments") or []) + list(extra_attachments or [])
    meta = {
        "record_id": record_id,
        "source": entry["id"],
        "canonical_url": url,
        "document_number": docnum,
        "title_zh": parsed["title"],
        "author_printed": parsed.get("author") or None,
        "outlet": entry["name"],
        "issuing_institution": parsed.get("issuing_institution") or None,
        "published": parsed.get("date") or None,
        "country": entry["country"],
        "language": entry["language"],
        "corpus": entry["corpus"],
        "authority_level": entry["authority_level"],
        "source_genre": decide_genre(entry, docnum, parsed.get("author")),
        "independence_cluster": entry["independence_cluster"],
        "translation_status": "original-zh",
        "body_complete": body_complete,
        "content_hash": content_hash,
        "docnum_hash": sha1_hex(docnum) if docnum else None,
        "republication_of": None,
        "attachments": attachments,
        "fetched": fetched_at,
    }
    return meta, body_md


def to_markdown(meta, body_md):
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list) and v:
            lines.append(f"{k}:")
            for item in v:
                lines.append(f'  - {{url: "{item["url"]}", content_type: "{item["content_type"]}"}}')
        else:
            lines.append(f"{k}: {_yaml_value(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body_md + "\n"


def parse_frontmatter(md_text):
    """Minimal reader for our own records (validation + manifest rebuild)."""
    m = re.match(r"^---\n(.*?)\n---\n", md_text, re.S)
    if not m:
        raise ValueError("no frontmatter")
    meta = {}
    for line in m.group(1).splitlines():
        if line.startswith("  - "):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        v = v.strip()
        if v == "null":
            meta[k] = None
        elif v in ("true", "false"):
            meta[k] = v == "true"
        elif v.startswith('"'):
            meta[k] = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        else:
            meta[k] = v
    return meta
