"""CLI: --liveness | --backfill [--recipe id] [--cap n] | --incremental.
All paths resolve from this file; nothing depends on cwd."""
import argparse
import json
import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scraper import config
from scraper.adapters import BY_KIND
from scraper.adapters._common import AdapterError
from scraper.dedup import dedupe
from scraper.fetcher import Fetcher, utcnow
from scraper.liveness import check as liveness_check
from scraper.normalize import build_record, to_markdown
from scraper.registry import load_registry
from scraper.relevance import is_relevant


def load_manifest(path=None):
    path = path or config.MANIFEST_PATH
    if Path(path).exists():
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return {"records": {}, "skipped": {}, "attachments": [], "failures": []}


def save_manifest(manifest, path=None):
    """Read-merge-write by record_id — never clobber prior entries."""
    path = Path(path or config.MANIFEST_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        prior = json.loads(path.read_text(encoding="utf-8"))
        for rid, entry in prior.get("records", {}).items():
            manifest["records"].setdefault(rid, entry)
        for url, entry in prior.get("skipped", {}).items():
            manifest["skipped"].setdefault(url, entry)
        seen_att = {(a["url"]) for a in manifest["attachments"]}
        for a in prior.get("attachments", []):
            if a["url"] not in seen_att:
                manifest["attachments"].append(a)
        # failures loaded at start of run are already in manifest; union without duplicating
        seen_f = set()
        merged = []
        for fl in prior.get("failures", []) + manifest.get("failures", []):
            key = (fl.get("source"), fl.get("stage"), fl.get("url"), fl.get("detail"))
            if key not in seen_f:
                seen_f.add(key)
                merged.append(fl)
        manifest["failures"] = merged
    staged = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            staged = Path(output.name)
            json.dump(manifest, output, ensure_ascii=False, indent=1)
        os.replace(staged, path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def process_recipe(entry, fetcher, manifest, cap=None, incremental=False, out_dir=None, *,
                   article_parser=None):
    """Capture one recipe. Returns a per-recipe summary; every failure COUNTS."""
    out_dir = Path(out_dir or config.NORMALIZED_DIR)
    summary = {"id": entry["id"], "discovered": 0, "fetched": 0, "parsed": 0,
               "relevant": 0, "irrelevant": 0, "fetch_failures": 0, "parse_failures": 0,
               "written": 0, "exact_dupes": 0, "republications": 0, "attachments": 0,
               "revision_detected": 0, "error": None}
    adapter = BY_KIND[entry["kind"]]
    parse = article_parser or adapter.parse
    cap = min(cap or entry["backfill_cap"], entry["backfill_cap"])
    try:
        urls = adapter.discover(entry, fetcher, cap=cap)
    except AdapterError as e:
        summary["error"] = f"broken-recipe: {e}"
        manifest["failures"].append({"source": entry["id"], "stage": "discover",
                                     "detail": str(e), "at": utcnow().isoformat()})
        return summary
    urls = urls[:cap]
    summary["discovered"] = len(urls)

    known_urls = {r.get("canonical_url") for r in manifest["records"].values()}
    known_urls |= set(manifest["skipped"])
    records = []
    required_title_terms = entry["recipe"].get("required_title_terms", ())
    for url in urls:
        if incremental and url in known_urls:
            continue
        r = fetcher.fetch(url, entry["allowed_domains"], max_age_days=entry["cadence_days"])
        if r.attachment:
            manifest["attachments"].append({**r.attachment, "source": entry["id"]})
            summary["attachments"] += 1
            continue
        if not r.ok:
            summary["fetch_failures"] += 1
            manifest["failures"].append({"source": entry["id"], "stage": "fetch", "url": url,
                                         "detail": r.skip_reason or str(r.status),
                                         "at": utcnow().isoformat()})
            continue
        summary["fetched"] += 1
        try:
            parsed = parse(r.text, entry, url)
        except AdapterError as e:
            summary["parse_failures"] += 1
            manifest["failures"].append({"source": entry["id"], "stage": "parse", "url": url,
                                         "detail": str(e), "at": utcnow().isoformat()})
            continue
        summary["parsed"] += 1
        body_text = "\n".join(parsed["body_paragraphs"])
        relevant = (any(term in parsed["title"] for term in required_title_terms)
                    if required_title_terms else is_relevant(parsed["title"], body_text))
        if not relevant:
            summary["irrelevant"] += 1
            manifest["skipped"][url] = {"title": parsed["title"], "source": entry["id"],
                                        "reason": "skipped-irrelevant", "at": utcnow().isoformat()}
            continue
        summary["relevant"] += 1
        body_complete = not entry["recipe"].get("abstract_only", False)
        meta, body_md = build_record(parsed, entry, url, r.fetched_at or utcnow().isoformat(),
                                     body_complete=body_complete)
        records.append((meta, body_md))

    if entry["kind"] == "seed-doc":
        prior_hashes = {r["content_hash"] for r in manifest["records"].values()
                        if r["source"] == entry["id"]}
        for meta, _ in records:
            if prior_hashes and meta["content_hash"] not in prior_hashes:
                meta["revision"] = "revision-detected"
                summary["revision_detected"] += 1

    kept, dropped = dedupe(records)
    summary["exact_dupes"] = len(dropped)
    out_dir.mkdir(parents=True, exist_ok=True)
    for meta, body_md in kept:
        if meta["republication_of"]:
            summary["republications"] += 1
        (out_dir / f"{meta['record_id']}.md").write_text(to_markdown(meta, body_md), encoding="utf-8")
        manifest["records"][meta["record_id"]] = {
            "source": meta["source"], "canonical_url": meta["canonical_url"],
            "title_zh": meta["title_zh"], "published": meta["published"],
            "content_hash": meta["content_hash"], "docnum_hash": meta["docnum_hash"],
            "republication_of": meta["republication_of"],
            "body_complete": meta["body_complete"], "fetched": meta["fetched"],
            **({"revision": meta["revision"]} if meta.get("revision") else {}),
        }
        summary["written"] += 1
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--liveness", action="store_true")
    mode.add_argument("--backfill", action="store_true")
    mode.add_argument("--incremental", action="store_true")
    ap.add_argument("--recipe", help="limit to one recipe id")
    ap.add_argument("--cap", type=int, help="override per-recipe article cap (never above backfill_cap)")
    args = ap.parse_args(argv)

    entries = load_registry()
    if args.recipe:
        entries = [e for e in entries if e["id"] == args.recipe]
        if not entries:
            print(f"unknown recipe id: {args.recipe}")
            return 2
    active = [e for e in entries if e["status"] == "active"]
    strict_html_hosts = {e["allowed_domains"][0]: e["recipe"]["strict_html"] for e in active
                         if "strict_html" in e["recipe"]}
    fetcher = Fetcher(strict_html_hosts=strict_html_hosts)
    try:
        if args.liveness:
            reports = []
            for e in active:
                rep = liveness_check(e, fetcher)
                reports.append(rep)
                print(f"{rep['id']:>18}: {rep['verdict']:<14} links={rep['listing_links']} "
                      f"paras={rep['sample_body_paras']} {rep['detail'] or ''}")
            config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            (config.OUTPUT_DIR / "liveness_report.json").write_text(
                json.dumps({"at": utcnow().isoformat(), "reports": reports},
                           ensure_ascii=False, indent=1), encoding="utf-8")
            return 0
        manifest = load_manifest()
        summaries = []
        for e in active:
            s = process_recipe(e, fetcher, manifest, cap=args.cap,
                               incremental=args.incremental)
            summaries.append(s)
            print(f"{s['id']:>18}: written={s['written']} relevant={s['relevant']} "
                  f"irrelevant={s['irrelevant']} fetchfail={s['fetch_failures']} "
                  f"parsefail={s['parse_failures']} {s['error'] or ''}")
        save_manifest(manifest)
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        (config.OUTPUT_DIR / "run_stats.json").write_text(
            json.dumps({"at": utcnow().isoformat(), "summaries": summaries,
                        "fetcher_stats": fetcher.stats, "warnings": fetcher.warnings},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        return 0
    finally:
        fetcher.close()


if __name__ == "__main__":
    sys.exit(main())
