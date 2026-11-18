"""Load and validate _sources.json. Data, not code."""
import json
from urllib.parse import urlsplit

from scraper.config import SOURCES_PATH

KINDS = {"issue-archive", "listing", "policy-db", "journal-toc", "seed-doc", "policy-api"}
CORPORA = {"academic", "thinktank", "government", "commentary", "industry"}
AUTHORITY_LEVELS = {"central-doctrinal", "regulator", "state-research", "academic", "commercial"}
SOURCE_GENRES = {"decree", "institutional-commentary", "signed-essay", "research-paper", "news"}
# annex v1.1: rung-a-failed = no discoverable endpoint after bundle analysis +
# convention probes; needs-container-runtime = Rung C required but no isolated runtime
STATUSES = {"active", "blocked", "requires-js", "broken-recipe",
            "rung-a-failed", "needs-container-runtime"}

REQUIRED_FIELDS = {
    "id": str, "name": str, "kind": str, "list_url": str,
    "allowed_domains": list, "country": str, "language": str,
    "corpus": str, "authority_level": str, "source_genre_default": str,
    "independence_cluster": str, "cadence_days": int, "backfill_cap": int,
    "status": str, "recipe": dict,
}
OPTIONAL_FIELDS = {"status_evidence": str}  # e.g. "403 on 2026-07-17"

_ENUM_CHECKS = [
    ("kind", KINDS), ("corpus", CORPORA), ("authority_level", AUTHORITY_LEVELS),
    ("source_genre_default", SOURCE_GENRES), ("status", STATUSES),
]


class RegistryError(ValueError):
    pass


def validate_entry(entry):
    if not isinstance(entry, dict):
        raise RegistryError(f"entry is not an object: {entry!r}")
    known = set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS)
    unknown = set(entry) - known
    if unknown:
        raise RegistryError(f"{entry.get('id', '?')}: unknown fields {sorted(unknown)}")
    for field, typ in REQUIRED_FIELDS.items():
        if field not in entry:
            raise RegistryError(f"{entry.get('id', '?')}: missing field {field}")
        if not isinstance(entry[field], typ):
            raise RegistryError(f"{entry.get('id', '?')}: field {field} must be {typ.__name__}")
    for field, allowed in _ENUM_CHECKS:
        if entry[field] not in allowed:
            raise RegistryError(f"{entry['id']}: {field}={entry[field]!r} not in {sorted(allowed)}")
    if not entry["allowed_domains"] or not all(isinstance(d, str) for d in entry["allowed_domains"]):
        raise RegistryError(f"{entry['id']}: allowed_domains must be a non-empty list of strings")
    recipe = entry["recipe"]
    terms = recipe.get("required_title_terms")
    if terms is not None and (not isinstance(terms, list) or not terms
                              or not all(isinstance(term, str) and term.strip() for term in terms)):
        raise RegistryError(f"{entry['id']}: required_title_terms must be a nonempty list of strings")
    strict = recipe.get("strict_html")
    if strict is not None:
        url = urlsplit(entry["list_url"])
        if (not isinstance(strict, dict) or url.scheme != "https" or url.netloc != url.hostname
                or entry["allowed_domains"] != [url.hostname]):
            raise RegistryError(f"{entry['id']}: strict_html requires one exact HTTPS host")
        path = strict.get("robots_missing_path")
        marker = strict.get("robots_missing_marker")
        if (path is not None or marker is not None) and (
                not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
                or not isinstance(marker, str) or not marker.strip()):
            raise RegistryError(f"{entry['id']}: strict_html requires a path and text for a soft 404")
    return entry


def load_registry(path=None):
    path = path or SOURCES_PATH
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise RegistryError("_sources.json must be a list")
    entries = [validate_entry(e) for e in data]
    ids = [e["id"] for e in entries]
    if len(ids) != len(set(ids)):
        raise RegistryError("duplicate recipe ids")
    return entries


def save_registry(entries, path=None):
    path = path or SOURCES_PATH
    for e in entries:
        validate_entry(e)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
        f.write("\n")
