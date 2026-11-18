"""Move normalized collector records through the dedicated private intake repo."""

import argparse
from base64 import b64encode
from hashlib import sha1
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_FILE_BYTES = 5 * 1024 * 1024  # Same bound as the Mojito text importer.
INTAKE_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/mojito-chinese-intake\Z")
RECORD_ID = re.compile(r"[a-z0-9_-]+__[0-9a-f]{8}\Z")
MANIFEST = "output/manifest.json"
NORMALIZED = "output/_normalized"
FIELDS = frozenset("source canonical_url title_zh published content_hash docnum_hash "
                   "republication_of body_complete fetched revision".split())


def bounded_read(path):
    if not path.is_file() or path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError("Expected a regular file inside the handoff directory.")
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("A handoff file exceeds the 5 MB limit.")
    return data


def decode_manifest(data):
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), dict):
        raise ValueError("Expected the collector's records object.")
    for record_id, row in payload["records"].items():
        if not RECORD_ID.fullmatch(record_id) or not isinstance(row, dict) or row.keys() - FIELDS:
            raise ValueError("Unexpected collector record metadata.")
        url = urlsplit(row["canonical_url"])
        if (url.scheme != "https" or not url.hostname or url.netloc != url.hostname
                or not row["title_zh"] or not re.fullmatch(r"[0-9a-f]{40}", row["content_hash"])
                or record_id != f"{row['source']}__{row['content_hash'][:8]}"):
            raise ValueError("Invalid collector source URL or record binding.")
    return payload["records"]


def check_body(data, row):
    text = data.decode("utf-8-sig").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise ValueError("Missing normalized source header.")
    front, end, doc = text[4:].partition("\n---\n")
    urls = {f"canonical_url: {row['canonical_url']}",
            f"canonical_url: {json.dumps(row['canonical_url'], ensure_ascii=False)}"}
    if not end or not urls.intersection(front.splitlines()):
        raise ValueError("Normalized source URL differs from the manifest.")
    paragraphs = []
    for index, block in enumerate(doc.strip().split("\n\n"), 1):
        locator = f"[P{index:03d}] "
        if not block.startswith(locator) or "\n" in block or not block[len(locator):].strip():
            raise ValueError("Invalid normalized source paragraph locator.")
        paragraphs.append(block[len(locator):])
    if sha1("\n".join(paragraphs).encode("utf-8")).hexdigest() != row["content_hash"]:
        raise ValueError("Normalized source hash differs from the manifest.")


def write_manifest(root, records):
    data = (json.dumps({"records": records}, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("The handoff manifest exceeds the Mojito reader's 5 MB limit.")
    path = root / MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def merge_batch(incoming, repository):
    """Retain distinct editions and return the exact paths for one commit."""
    path = repository / MANIFEST
    existing = decode_manifest(bounded_read(path)) if path.exists() else {}
    current_urls = {row["canonical_url"] for row in existing.values()}
    added = []
    for rid, row in decode_manifest(bounded_read(incoming / MANIFEST)).items():
        if row.get("body_complete") is not True or row.get("republication_of"):
            continue
        relative = f"{NORMALIZED}/{rid}.md"
        data = bounded_read(incoming / relative)
        check_body(data, row)
        if rid in existing:
            if any(row[key] != existing[rid][key] for key in ("canonical_url", "content_hash")):
                raise ValueError("A recorded edition identifier has a conflicting binding.")
            continue
        if row["canonical_url"] in current_urls:
            row = {**row, "revision": "revision-detected"}
        current_urls.add(row["canonical_url"])
        existing[rid] = row
        target = repository / relative
        if target.parent.resolve() != target.parent.absolute():
            raise ValueError("Unexpected linked directory in the intake repository.")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
        added.append(relative)
    if added:
        write_manifest(repository, existing)
    return added


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def verify_private_repo(repository, token):
    if not INTAKE_REPO.fullmatch(repository):
        raise ValueError("Expected owner/mojito-chinese-intake.")
    request = Request(f"https://api.github.com/repos/{repository}", headers={
        "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
    })
    with build_opener(NoRedirect()).open(request, timeout=30) as response:
        raw = response.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("GitHub returned oversized repository metadata.")
    meta = json.loads(raw)
    if meta.get("private") is not True or meta.get("full_name", "").lower() != repository.lower():
        raise ValueError("The intake destination must be the designated private repository.")
    return meta["default_branch"]


def git(args, *, cwd=None, token=None, output=False):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_") and key not in {"INTAKE_UPLOAD_TOKEN", "MOJITO_INTAKE_READ_TOKEN"}}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    if token is not None:
        auth = b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        env.update(GIT_CONFIG_COUNT="3", GIT_CONFIG_KEY_0="http.https://github.com/.extraHeader",
                   GIT_CONFIG_VALUE_0=f"Authorization: Basic {auth}",
                   GIT_CONFIG_KEY_1="http.followRedirects", GIT_CONFIG_VALUE_1="false",
                   GIT_CONFIG_KEY_2="credential.helper", GIT_CONFIG_VALUE_2="")
    result = subprocess.run(["git", "-c", f"core.hooksPath={os.devnull}", *args], cwd=cwd, env=env,
                            stdout=subprocess.PIPE if output else subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, check=False, timeout=180)
    if result.returncode:
        raise ValueError(f"Git {args[0]} failed.")
    return result.stdout


def clone(repository, branch, destination, token, *, checkout):
    options = ["--depth=1", "--no-tags", "--single-branch", "--branch", branch]
    if not checkout:
        options.append("--no-checkout")
    git(["clone", *options, "--", f"https://github.com/{repository}.git", str(destination)], token=token)


def stage_snapshot(repository, staging):
    """Copy just the fixed manifest and required Git blobs. Never check out repository files."""

    def blob(relative):
        spec = f"HEAD:{relative}"
        if (git(["cat-file", "-t", spec], cwd=repository, output=True).strip() != b"blob"
                or int(git(["cat-file", "-s", spec], cwd=repository, output=True)) > MAX_FILE_BYTES):
            raise ValueError("Expected a bounded document blob.")
        return git(["cat-file", "blob", spec], cwd=repository, output=True)

    records = decode_manifest(blob(MANIFEST))
    folder = staging / NORMALIZED
    folder.mkdir(parents=True, exist_ok=False)
    for rid, row in records.items():
        path = f"{NORMALIZED}/{rid}.md"
        data = blob(path)
        check_body(data, row)
        (staging / path).write_bytes(data)
    write_manifest(staging, records)
    return len(records)


def pull_snapshot(repository, token, staging):
    branch = verify_private_repo(repository, token)
    staging = staging.resolve()
    if staging.exists():
        raise ValueError("Choose a new staging directory for the download.")
    staging.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="mojito-intake-", dir=staging.parent) as work:
        clone_path = Path(work) / "repo"
        clone(repository, branch, clone_path, token, checkout=False)
        candidate = Path(work) / "snapshot"
        count = stage_snapshot(clone_path, candidate)
        candidate.rename(staging)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("push", "pull"))
    parser.add_argument("--repository", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    pushing = args.operation == "push"
    token = os.environ.pop("INTAKE_UPLOAD_TOKEN" if pushing else "MOJITO_INTAKE_READ_TOKEN", "")
    if not token:
        print(json.dumps({"status": "missing_token"}))
        return 1
    try:
        directory = args.directory.resolve()
        if pushing:
            branch = verify_private_repo(args.repository, token)
            with TemporaryDirectory(prefix="mojito-intake-") as tmp:
                repository = Path(tmp) / "repo"
                clone(args.repository, branch, repository, token, checkout=True)
                changed = merge_batch(directory, repository)
                if changed:
                    git(["add", "--", MANIFEST, *changed], cwd=repository)
                    git(["-c", "user.name=mojito-intake", "-c", "user.email=mojito-intake@users.noreply.github.com",
                         "commit", "-m", "Import normalized public sources"], cwd=repository)
                    git(["push", "origin", f"HEAD:refs/heads/{branch}"], cwd=repository, token=token)
                count = len(changed)
        else:
            count = pull_snapshot(args.repository, token, directory)
        print(json.dumps({"status": "ok", "documents": count}))
        return 0
    except Exception:
        # This command can run in a public workflow. Keep paths, URLs and credentials out of logs.
        print(json.dumps({"status": "handoff_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
