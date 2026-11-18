"""Dedup: exact content-hash collapse + document-number canonicalization.
Same docnum across sites -> the issuing-institution copy is canonical,
the others get republication_of."""

# docnum prefix -> independence_cluster of the issuing institution
PREFIX_CLUSTER = [
    ("国办发", "state-council"),
    ("国发", "state-council"),
    ("工信部", "miit"),
    ("网信", "cac"),
    ("国科发", "most"),
]


def issuing_cluster(docnum):
    if not docnum:
        return None
    for prefix, cluster in PREFIX_CLUSTER:
        if docnum.startswith(prefix):
            return cluster
    return None


def dedupe(records):
    """records: list of (meta, body_md) in capture order. Returns (kept, dropped_exact).
    Mutates meta['republication_of'] on docnum republications."""
    kept, dropped = [], []
    by_content = {}
    for meta, body in records:
        if meta["content_hash"] in by_content:
            dropped.append(meta)
            continue
        by_content[meta["content_hash"]] = meta
        kept.append((meta, body))

    by_docnum = {}
    for meta, _ in kept:
        if meta["docnum_hash"]:
            by_docnum.setdefault(meta["docnum_hash"], []).append(meta)
    for group in by_docnum.values():
        if len(group) < 2:
            continue
        cluster = issuing_cluster(group[0]["document_number"])
        canonical = next((m for m in group if m["independence_cluster"] == cluster), group[0])
        for m in group:
            if m is not canonical:
                m["republication_of"] = canonical["record_id"]
    return kept, dropped
