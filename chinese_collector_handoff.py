"""Move normalized collector records through the dedicated private intake repo."""

import argparse
from base64 import b64encode
from hashlib import sha1
import json
import os
from pathlib import Path
import re
from shlex import join as shell_join
from shutil import which
import subprocess
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
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


def complete_paper_byline(current, incoming):
    """Fill a missing printed reporter in an otherwise identical e-paper edition."""
    old_header, separator, old_body = current.decode("utf-8").partition("\n---\n")
    new_header, _, new_body = incoming.decode("utf-8").partition("\n---\n")
    authors = [line for line in new_header.splitlines() if line.startswith("author_printed:")]
    author = re.fullmatch(r"author_printed: ([\u3400-\u9fff]{2,4})", authors[0]) if len(authors) == 1 else None
    if (not separator or old_body != new_body or not author
            or old_header.splitlines().count("author_printed: null") != 1
            or old_header.splitlines().count("source_genre: institutional-commentary") != 1
            or new_header.splitlines().count("source_genre: signed-essay") != 1):
        return None
    varying = ("author_printed:", "source_genre:", "fetched:")
    old_fixed = [line for line in old_header.splitlines() if not line.startswith(varying)]
    new_fixed = [line for line in new_header.splitlines() if not line.startswith(varying)]
    if old_fixed != new_fixed:
        return None
    header = old_header.replace("\nauthor_printed: null\n", f"\nauthor_printed: {author[1]}\n", 1)
    header = header.replace("\nsource_genre: institutional-commentary\n", "\nsource_genre: signed-essay\n", 1)
    return (header + separator + old_body).encode("utf-8")


def completed_capture(current, incoming):
    old_header, separator, old_body = current.decode("utf-8").replace("\r\n", "\n").partition("\n---\n")
    new_header, new_separator, new_body = incoming.decode("utf-8").replace("\r\n", "\n").partition("\n---\n")
    if (not separator or not new_separator or old_body != new_body
            or old_header.splitlines().count("body_complete: false") != 1
            or new_header.splitlines().count("body_complete: true") != 1):
        return False
    varying = ("body_complete:", "fetched:")
    return ([line for line in old_header.splitlines() if not line.startswith(varying)]
            == [line for line in new_header.splitlines() if not line.startswith(varying)])


def merge_batch(incoming, repository, *, include_incomplete=False):
    """Retain distinct editions and return the exact paths for one commit."""
    path = repository / MANIFEST
    existing = decode_manifest(bounded_read(path)) if path.exists() else {}
    current_urls = {row["canonical_url"] for row in existing.values()}
    added = []
    for rid, row in decode_manifest(bounded_read(incoming / MANIFEST)).items():
        if (row.get("body_complete") is not True
                and not (include_incomplete and row.get("body_complete") is False)
                or row.get("republication_of")):
            continue
        relative = f"{NORMALIZED}/{rid}.md"
        data = bounded_read(incoming / relative)
        check_body(data, row)
        target = repository / relative
        if rid in existing:
            if any(row[key] != existing[rid][key] for key in ("canonical_url", "content_hash")):
                raise ValueError("A recorded edition identifier has a conflicting binding.")
            if existing[rid].get("body_complete") is False and row.get("body_complete") is True:
                if not completed_capture(bounded_read(target), data):
                    raise ValueError("Completing an edition cannot change its text or fixed metadata.")
                target.write_bytes(data)
                existing[rid] = row
                added.append(relative)
                continue
            if row["source"] == "pd_epaper" and (corrected := complete_paper_byline(bounded_read(target), data)):
                target.write_bytes(corrected)
                added.append(relative)
            continue
        if row["canonical_url"] in current_urls:
            row = {**row, "revision": "revision-detected"}
        current_urls.add(row["canonical_url"])
        existing[rid] = row
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


def git(args, *, cwd=None, token=None, ssh_config=None, output=False):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_") and key not in {"INTAKE_UPLOAD_TOKEN", "MOJITO_INTAKE_READ_TOKEN"}}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    if token is not None:
        auth = b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        env.update(GIT_CONFIG_COUNT="3", GIT_CONFIG_KEY_0="http.https://github.com/.extraHeader",
                   GIT_CONFIG_VALUE_0=f"Authorization: Basic {auth}",
                   GIT_CONFIG_KEY_1="http.followRedirects", GIT_CONFIG_VALUE_1="false",
                   GIT_CONFIG_KEY_2="credential.helper", GIT_CONFIG_VALUE_2="")
    if ssh_config is not None:
        ssh = which("ssh")
        if ssh is None:
            raise ValueError("OpenSSH is required for deploy-key access.")
        env["GIT_SSH_COMMAND"] = shell_join([Path(ssh).as_posix(), "-F", ssh_config.as_posix()])
        env["GIT_SSH_VARIANT"] = "ssh"
    result = subprocess.run(["git", "-c", f"core.hooksPath={os.devnull}", *args], cwd=cwd, env=env,
                            stdout=subprocess.PIPE if output else subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, check=False, timeout=180)
    if result.returncode:
        raise ValueError(f"Git {args[0]} failed.")
    return result.stdout


def clone(repository, branch, destination, token, *, checkout, ssh_config=None):
    options = ["--depth=1", "--no-tags", "--single-branch", "--branch", branch]
    if not checkout:
        options.append("--no-checkout")
    url = (f"ssh://git@mojito-intake/{repository}.git" if ssh_config
           else f"https://github.com/{repository}.git")
    git(["clone", *options, "--", url, str(destination)], token=token, ssh_config=ssh_config)


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


def pull_snapshot(repository, token, staging, *, ssh_config=None):
    if not INTAKE_REPO.fullmatch(repository):
        raise ValueError("Expected owner/mojito-chinese-intake.")
    branch = "main" if ssh_config else verify_private_repo(repository, token)
    staging = staging.resolve()
    if staging.exists():
        raise ValueError("Choose a new staging directory for the download.")
    staging.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="mojito-intake-", dir=staging.parent) as work:
        clone_path = Path(work) / "repo"
        clone(repository, branch, clone_path, token, checkout=False, ssh_config=ssh_config)
        candidate = Path(work) / "snapshot"
        count = stage_snapshot(clone_path, candidate)
        candidate.rename(staging)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("check-write", "push", "pull"))
    parser.add_argument("--include-incomplete", action="store_true",
                        help="Retain explicitly incomplete converted text for private inspection; it remains ineligible for import.")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--ssh-config", type=Path, help="local read-only deploy key configuration, pull only")
    args = parser.parse_args()
    pushing = args.operation != "pull"
    if pushing and args.ssh_config:
        parser.error("--ssh-config is for pull only")
    token = os.environ.pop("INTAKE_UPLOAD_TOKEN" if pushing else "MOJITO_INTAKE_READ_TOKEN", "")
    if not token and not args.ssh_config:
        print(json.dumps({"status": "missing_token"}))
        return 1
    phase = "destination"
    try:
        directory = args.directory.resolve()
        if pushing:
            branch = verify_private_repo(args.repository, token)
            with TemporaryDirectory(prefix="mojito-intake-") as tmp:
                repository = Path(tmp) / "repo"
                phase = "clone"
                clone(args.repository, branch, repository, token, checkout=True)
                if args.operation == "check-write":
                    phase = "write_access"
                    git(["push", "--dry-run", "origin", f"HEAD:refs/heads/{branch}"], cwd=repository, token=token)
                    count = 0
                else:
                    phase = "validate_batch"
                    changed = merge_batch(directory, repository, include_incomplete=args.include_incomplete)
                    if changed:
                        phase = "commit"
                        git(["add", "--", MANIFEST, *changed], cwd=repository)
                        git(["-c", "user.name=mojito-intake", "-c",
                             "user.email=mojito-intake@users.noreply.github.com",
                             "commit", "-m", "Import normalized public sources"], cwd=repository)
                        phase = "push"
                        git(["push", "origin", f"HEAD:refs/heads/{branch}"], cwd=repository, token=token)
                    count = len(changed)
        else:
            phase = "read_snapshot"
            ssh_config = args.ssh_config.resolve(strict=True) if args.ssh_config else None
            count = pull_snapshot(args.repository, None if ssh_config else token, directory, ssh_config=ssh_config)
        print(json.dumps({"status": "ok", "documents": count}))
        return 0
    except HTTPError as error:
        print(json.dumps({"status": "handoff_failed", "phase": phase, "http_status": error.code}))
        return 1
    except Exception as error:
        # This command can run in a public workflow. Keep paths, URLs and credentials out of logs.
        print(json.dumps({"status": "handoff_failed", "phase": phase, "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
