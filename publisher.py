#!/usr/bin/env python3
"""Trusted policy and control plane. Never import code from the source checkout."""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

SOURCE = "Ebenderooock/loom"
PUBLISHER = "Ebenderooock/loom-publisher"
IMAGE = "ghcr.io/ebenderooock/loom"
CI_ID = 271638826
CI_PATH = ".github/workflows/ci.yml"
NUMBER = r"(?:0|[1-9][0-9]*)"
CORE = rf"{NUMBER}\.{NUMBER}\.{NUMBER}"
SEMVER = re.compile(
    rf"^({CORE})(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
SHA = re.compile(r"^[0-9a-f]{40}$")
RUN_URL = re.compile(
    r"^https://github\.com/Ebenderooock/loom-publisher/actions/runs/[1-9][0-9]*$",
    re.IGNORECASE,
)
TARGETS = (
    ("linux", "amd64"), ("linux", "arm64"), ("linux", "armv7"),
    ("darwin", "amd64"), ("darwin", "arm64"),
    ("windows", "amd64"), ("windows", "arm64"),
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def version(value):
    match = SEMVER.fullmatch(value)
    require(match is not None, f"Invalid semver: {value!r}")
    core, pre, metadata = match.groups()
    if pre:
        require(all(not (p.isdigit() and len(p) > 1 and p[0] == "0")
                    for p in pre.split(".")), "Leading zero in prerelease")
    # Docker tag limit is 128; reserve room for arch suffixes.
    require(len(value) <= 110, "Version too long")
    return tuple(map(int, core.split("."))), pre, metadata


def ref_policy(ref):
    if ref == "refs/heads/main":
        return "main", None
    match = re.fullmatch(rf"refs/heads/(prelease|prerelease)/({CORE})", ref)
    if match:
        require(len(match[2]) <= 60, "Channel version too long")
        return match[1], match[2]
    if ref.startswith("refs/tags/v"):
        value = ref[len("refs/tags/v"):]
        version(value)
        return "release", value
    raise ValueError(f"Ref not allowed: {ref!r}")


def short_ref(ref):
    ref_policy(ref)
    return ref.split("/", 2)[2]


def image_tags(candidate):
    kind, value = ref_policy(candidate["ref"])
    short = candidate["sha"][:7]
    if kind == "main":
        return [f"main-{short}-{candidate['timestamp']}"]
    if kind != "release":
        channel = f"{kind}-{value}"
        return [f"{channel}-{short}-{candidate['ci_id']}-{candidate['ci_attempt']}",
                channel]
    return [value.replace("+", "_")]


def valid_ci(run, ref, sha):
    return (
        run.get("workflow_id") == CI_ID
        and run.get("path") == CI_PATH
        and run.get("event") == "push"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("head_sha") == sha
        and run.get("head_branch") == short_ref(ref)
        and (run.get("head_repository") or {}).get("full_name", "").lower() == SOURCE.lower()
        and type(run.get("id")) is int and run["id"] > 0
        and type(run.get("run_attempt")) is int and run["run_attempt"] > 0
        and type(run.get("check_suite_id")) is int and run["check_suite_id"] > 0
    )


def trusted_success(statuses, ref, slug):
    status = trusted_status(statuses, ref, slug)
    return status is not None and status.get("state") == "success"


def trusted_status(statuses, ref, slug):
    context = "loom-publisher/" + short_ref(ref)
    # Newest status from the expected creator wins; never accept a user's spoof.
    for status in statuses:
        if (status.get("context") == context
                and (status.get("creator") or {}).get("login") == slug + "[bot]"
                and RUN_URL.fullmatch(status.get("target_url") or "")):
            return status
    return None


def baseline_check(candidate):
    baseline = os.environ.get("PUBLISHING_NOT_BEFORE", "")
    require(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", baseline),
            "PUBLISHING_NOT_BEFORE must be a UTC timestamp (YYYY-MM-DDTHH:MM:SSZ)")
    floor = dt.datetime.fromisoformat(baseline.replace("Z", "+00:00"))
    created = dt.datetime.fromisoformat(candidate["ci_created_at"].replace("Z", "+00:00"))
    require(created >= floor, "Source CI predates activation baseline")


def release_record(api, ref):
    try:
        return api.call(source_path("releases/tags/"
                                   + urllib.parse.quote(short_ref(ref), safe="")))
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        exc.close()
        # The by-tag endpoint is documented for published releases. Include
        # unpublished drafts using this App's contents-write control token.
        matches = [release for release in api.pages(source_path("releases"))
                   if release.get("tag_name") == short_ref(ref)]
        require(len(matches) <= 1, "Multiple releases for this tag; owner investigation required")
        return matches[0] if matches else None


def require_owned_draft(release, candidate):
    require(release.get("draft") is True
            and (release.get("author") or {}).get("login") == os.environ["APP_SLUG"] + "[bot]"
            and release.get("target_commitish") == candidate["sha"]
            and release.get("tag_name") == short_ref(candidate["ref"])
            and re.fullmatch(
                r"Built by the trusted \[publisher\]\("
                r"https://github\.com/Ebenderooock/loom-publisher/actions/runs/[1-9][0-9]*\)\.",
                (release.get("body") or "").split("\n\n", 1)[0], re.IGNORECASE),
            "Existing release is immutable (only this App's matching drafts can resume)")


class API:
    def __init__(self):
        self.token = os.environ["LOOM_TOKEN"]
        require(self.token, "LOOM_TOKEN is required")

    def call(self, path, data=None, method=None, raw=None):
        require(path.startswith(("/repos/" + SOURCE + "/",
                                 "/repos/" + SOURCE + "?")),
                "API target outside source repository")
        host = "https://uploads.github.com" if raw is not None else "https://api.github.com"
        request = urllib.request.Request(
            host + path,
            data=raw if raw is not None else (
                json.dumps(data).encode() if data is not None else None),
            method=method or ("POST" if data is not None or raw is not None else "GET"),
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/octet-stream" if raw is not None
                else "application/json",
            },
        )
        # No externally supplied API/upload URLs and no redirects with credentials.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        with urllib.request.build_opener(NoRedirect).open(request, timeout=120) as response:
            body = response.read()
            return json.loads(body) if body else None

    def pages(self, path, key=None):
        separator = "&" if "?" in path else "?"
        page = 1
        while True:
            result = self.call(f"{path}{separator}per_page=100&page={page}")
            items = result[key] if key else result
            yield from items
            if len(items) < 100:
                return
            page += 1
            require(page <= 100, "Pagination safety limit exceeded; narrow repository history")


def source_path(path):
    return "/repos/" + SOURCE + "/" + path


def resolve(api, ref):
    ref_policy(ref)
    result = api.call(source_path("git/ref/" + ref[len("refs/"):]))
    obj = result["object"]
    for _ in range(6):
        require(SHA.fullmatch(obj["sha"]), "Invalid object SHA")
        if obj["type"] == "commit":
            return obj["sha"]
        require(obj["type"] == "tag", "Ref does not point to commit/tag")
        obj = api.call(source_path("git/tags/" + obj["sha"]))["object"]
    raise ValueError("Too many annotated tag indirections")


def validate_workflow(api):
    workflow = api.call(source_path("actions/workflows/ci.yml"))
    require(workflow["id"] == CI_ID and workflow["path"] == CI_PATH
            and workflow["state"] == "active", "Source CI identity changed")


def authorize(api, ref):
    kind, _ = ref_policy(ref)
    validate_workflow(api)
    sha = resolve(api, ref)
    if kind == "release":
        comparison = api.call(source_path("compare/" + sha + "...main"))
        require(comparison["status"] in ("ahead", "identical")
                and comparison["merge_base_commit"]["sha"] == sha,
                "Release commit is not on main")
    # Read all states, NOT conclusion=success: a failed rerun must revoke authority.
    runs = list(api.pages(source_path(
        f"actions/workflows/{CI_ID}/runs?event=push&head_sha={sha}"), "workflow_runs"))
    runs = [r for r in runs if r.get("head_branch") == short_ref(ref)]
    require(runs, f"No push CI for current tip of {ref}")
    run = max(runs, key=lambda r: r["id"])
    require(valid_ci(run, ref, sha), f"Latest push CI is not successful for {ref}")
    commit = api.call(source_path("commits/" + sha))
    date = commit["commit"]["committer"]["date"]
    timestamp = int(dt.datetime.fromisoformat(date.replace("Z", "+00:00")).timestamp())
    require(timestamp > 0, "Invalid source commit timestamp")
    require(resolve(api, ref) == sha, "Ref moved during authorization")
    return {"ref": ref, "sha": sha, "ci_id": run["id"],
            "ci_attempt": run["run_attempt"], "ci_created_at": run["created_at"],
            "ci_suite": run["check_suite_id"],
            "timestamp": timestamp, "date": date}


def documentation_only(api, candidate):
    if ref_policy(candidate["ref"])[0] == "release":
        return False
    suite = api.call(source_path(f"check-suites/{candidate['ci_suite']}"))
    require(suite.get("id") == candidate["ci_suite"]
            and (suite.get("app") or {}).get("slug") == "github-actions"
            and suite.get("head_branch") == short_ref(candidate["ref"])
            and suite.get("head_sha") == candidate["sha"]
            and suite.get("after") == candidate["sha"],
            "Push check-suite identity changed")
    before = suite.get("before") or ""
    require(SHA.fullmatch(before), "Missing push before SHA")
    if before == "0" * 40:
        return False
    comparison = api.call(source_path("compare/" + before + "..." + candidate["sha"]))
    require(comparison.get("status") in {"ahead", "identical"},
            "Source push is not a fast-forward")
    files = comparison.get("files")
    require(isinstance(files, list), "Missing push comparison files")
    # GitHub truncates comparison file lists at 300; never infer docs-only then.
    if len(files) >= 300:
        return False
    for file in files:
        paths = [file.get("filename")]
        if "previous_filename" in file:
            paths.append(file["previous_filename"])
        for path in paths:
            require(isinstance(path, str) and path, "Invalid changed-file path")
            if not (path.startswith("docs/") or path.endswith(".md")):
                return False
    return True


def candidates(api):
    # Releases first, newest numeric stable version first; no lookback window can
    # lose an unpublished current tip. Unsupported refs are intentionally ignored.
    refs = ["refs/heads/main"]
    refs += ["refs/heads/" + b["name"] for b in api.pages(source_path("branches"))]
    refs += ["refs/tags/" + t["name"] for t in api.pages(source_path("tags"))]
    allowed = []
    for ref in set(refs):
        try:
            kind, value = ref_policy(ref)
        except ValueError:
            continue
        allowed.append((kind, value, ref))
    return [item[2] for item in sorted(
        allowed, key=lambda item: (
            item[0] == "release",
            version(item[1])[0] if item[0] == "release" else (0, 0, 0),
            item[2]), reverse=True)]


def set_status(api, candidate, state):
    url = os.environ["PUBLISHER_RUN_URL"]
    require(RUN_URL.fullmatch(url), "Invalid publisher run URL")
    require(SHA.fullmatch(candidate["sha"]), "Invalid status commit")
    api.call(source_path("statuses/" + candidate["sha"]), {
        "state": state, "context": "loom-publisher/" + short_ref(candidate["ref"]),
        "target_url": url, "description": status_description(candidate, state),
    })


def status_description(candidate, state):
    return f"Trusted publisher: {state} (CI {candidate['ci_id']}/{candidate['ci_attempt']})"


def discover(api, explicit):
    slug = os.environ["APP_SLUG"]
    require(re.fullmatch(r"[a-z0-9-]+", slug), "Missing/invalid App slug")
    validate_workflow(api)
    # Validate baseline configuration even when no refs have eligible CI.
    baseline_check({"ci_created_at": os.environ.get("PUBLISHING_NOT_BEFORE", "")})
    for ref in [explicit] if explicit else candidates(api):
        try:
            candidate = authorize(api, ref)
            baseline_check(candidate)
            if not explicit and documentation_only(api, candidate):
                print(f"Documentation-only push: {ref}")
                continue
            if ref_policy(ref)[0] == "release":
                existing = release_record(api, ref)
                if existing:
                    require_owned_draft(existing, candidate)
        except urllib.error.HTTPError as exc:
            if explicit or exc.code != 404:
                raise
            exc.close()
            print(f"Ref disappeared: {ref}")
            continue
        except ValueError as exc:
            if explicit:
                raise
            # CI pending, missing, failed, or stale tips cannot be published.
            print(f"Not eligible: {ref}: {exc}")
            continue
        statuses = list(api.pages(source_path("commits/" + candidate["sha"] + "/statuses")))
        if trusted_success(statuses, ref, slug):
            if explicit:
                print("Already published")
            continue
        previous = trusted_status(statuses, ref, slug)
        if (not explicit and previous and previous.get("state") == "failure"
                and previous.get("description") == status_description(candidate, "failure")):
            print(f"Awaiting explicit retry or new CI attempt: {ref}")
            continue
        set_status(api, candidate, "pending")
        return candidate
    return None


def expected_assets(value):
    result = {f"loom_{value}_{system}_{arch}."
              + ("zip" if system == "windows" else "tar.gz")
              for system, arch in TARGETS}
    result.add(f"loom-source-v{value}.tar.gz")
    return result | {name + ".sbom.spdx.json" for name in result}


def validate_assets(directory, value):
    expected = expected_assets(value)
    actual = {p.name for p in directory.iterdir()}
    require(actual == expected, f"Unexpected/missing release assets: {actual ^ expected}")
    for name in sorted(expected):
        path = directory / name
        require(path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= 2**31,
                "Unsafe release asset")
        if name.endswith(".json"):
            require(path.stat().st_size <= 64 * 1024**2, "SBOM too large")
            sbom = json.loads(path.read_bytes())
            require(sbom.get("spdxVersion", "").startswith("SPDX-"), "Invalid SBOM")
    # Assets are opaque data. Never extract archives or execute their contents.
    checksums = []
    for name in sorted(expected):
        with (directory / name).open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        checksums.append(f"{digest}  {name}\n")
    (directory / "checksums.txt").write_text("".join(checksums))


OCI = "application/vnd.oci."


def validate_oci(path, architectures, release_version=None, revision=None):
    """Validate an OCI layout without extracting anything into the trusted runner."""
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 8 * 2**30,
            "Unsafe OCI artifact")
    with tarfile.open(path, "r:") as archive:
        members = {}
        total = 0
        for member in archive:
            name = member.name.rstrip("/")
            require(name not in members, "Duplicate OCI member")
            require(name in ("oci-layout", "index.json", "blobs", "blobs/sha256")
                    or re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", name),
                    "Unsafe OCI member path")
            require(member.isfile() or member.isdir(), "OCI links/devices forbidden")
            require(not member.isdir() or name in ("blobs", "blobs/sha256"),
                    "Unexpected OCI directory")
            total += member.size
            require(total <= 8 * 2**30 and len(members) < 10000, "OCI limits exceeded")
            members[name] = member

        def read(name, limit=8 * 1024**2):
            require(name in members and members[name].isfile()
                    and members[name].size <= limit, "Missing/oversize OCI JSON")
            return json.load(archive.extractfile(members[name]))

        require(read("oci-layout") == {"imageLayoutVersion": "1.0.0"}, "Invalid OCI layout")
        checked = set()

        def blob(desc, media):
            require(desc.get("mediaType") in media and not desc.get("urls")
                    and not desc.get("data"), "Unsupported/remote OCI descriptor")
            digest = desc.get("digest", "")
            require(re.fullmatch(r"sha256:[0-9a-f]{64}", digest), "Invalid OCI digest")
            name = "blobs/sha256/" + digest[7:]
            require(name in members and members[name].isfile()
                    and type(desc.get("size")) is int and members[name].size == desc["size"],
                    "OCI blob size mismatch")
            if name not in checked:
                stream = archive.extractfile(members[name])
                require(hashlib.file_digest(stream, "sha256").hexdigest() == digest[7:],
                        "OCI digest mismatch")
                checked.add(name)
            return name

        found = []

        def walk(index, depth=0):
            require(depth <= 2 and index.get("schemaVersion") == 2, "Invalid OCI index")
            require(1 <= len(index.get("manifests", [])) <= 2, "Unexpected manifest count")
            for desc in index["manifests"]:
                if desc.get("mediaType") == OCI + "image.index.v1+json":
                    walk(read(blob(desc, {OCI + "image.index.v1+json"})), depth + 1)
                    continue
                manifest = read(blob(desc, {OCI + "image.manifest.v1+json"}))
                require(manifest.get("schemaVersion") == 2
                        and not manifest.get("subject") and not manifest.get("artifactType"),
                        "Not a container image")
                config = read(blob(manifest["config"], {OCI + "image.config.v1+json"}))
                require(config.get("os") == "linux"
                        and config.get("architecture") in architectures, "Unexpected image platform")
                if release_version is not None:
                    labels = config.get("config", {}).get("Labels", {})
                    require(labels.get("org.opencontainers.image.version") == release_version
                            and labels.get("org.opencontainers.image.revision") == revision,
                            "Release image metadata mismatch")
                platform = desc.get("platform")
                if platform:
                    require(platform.get("os") == "linux"
                            and platform.get("architecture") == config["architecture"],
                            "Platform/config mismatch")
                found.append(config["architecture"])
                layers = manifest.get("layers", [])
                require(1 <= len(layers) <= 100, "Invalid layer count")
                for layer in layers:
                    blob(layer, {OCI + "image.layer.v1.tar",
                                 OCI + "image.layer.v1.tar+gzip",
                                 OCI + "image.layer.v1.tar+zstd"})

        index = read("index.json")
        require(len(index.get("manifests", [])) == 1, "OCI transport needs one root image")
        walk(index)
        require(sorted(found) == sorted(architectures), "Missing/duplicate image platform")
        require(checked == {name for name in members if name.startswith("blobs/sha256/")},
                "Unreferenced OCI blob")


def latest_allowed(value, existing):
    core, pre, _ = version(value)
    if pre:
        return False
    if existing is None:
        return True
    old_core, old_pre, _ = version(existing.removeprefix("v"))
    require(old_pre is None, "Current latest has a prerelease label; owner investigation required")
    return core > old_core or value == existing.removeprefix("v")


def registry_latest():
    # Distinguish an absent latest from auth/network failures. Never fail open.
    result = subprocess.run(
        ["skopeo", "inspect", "--config", "--override-os", "linux",
         "--override-arch", "amd64", "docker://" + IMAGE + ":latest"],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        require("manifest unknown" in result.stderr.lower()
                or "name unknown" in result.stderr.lower(),
                "Cannot establish current latest: " + result.stderr)
        return None
    config = json.loads(result.stdout)
    value = config.get("config", {}).get("Labels", {}).get("org.opencontainers.image.version")
    require(isinstance(value, str) and value, "Current latest lacks version label")
    return value


def publish_release(api, candidate, directory, advance):
    _, value = ref_policy(candidate["ref"])
    tag = short_ref(candidate["ref"])
    release = release_record(api, candidate["ref"])
    if release:
        require_owned_draft(release, candidate)
    else:
        notes = api.call(source_path("releases/generate-notes"), {
            "tag_name": tag, "target_commitish": candidate["sha"],
        })
        require(isinstance(notes.get("body"), str), "Missing generated release notes")
        # Draft first: a partial upload must not masquerade as a complete release.
        release = api.call(source_path("releases"), {
            "tag_name": tag, "target_commitish": candidate["sha"], "name": tag,
            "body": "Built by the trusted [publisher]("
                    + os.environ["PUBLISHER_RUN_URL"] + ").\n\n" + notes["body"],
            "draft": True, "prerelease": version(value)[1] is not None,
            "make_latest": "false",
        })
    release_id = release["id"]
    require(type(release_id) is int and release_id > 0, "Invalid release ID")
    assets = {asset["name"]: asset for asset in api.pages(
        source_path(f"releases/{release_id}/assets"))}
    require(set(assets) <= {path.name for path in directory.iterdir()},
            "Unexpected existing draft assets; owner investigation required")
    for path in sorted(directory.iterdir()):
        data = path.read_bytes()
        if path.name in assets:
            # GitHub's immutable server-computed asset digest, NOT artifact metadata.
            if assets[path.name].get("digest") == "sha256:" + hashlib.sha256(data).hexdigest():
                continue
            # Only our verified, unpublished draft may be replaced on retry.
            asset_id = assets[path.name]["id"]
            require(type(asset_id) is int and asset_id > 0, "Invalid asset ID")
            api.call(source_path(f"releases/assets/{asset_id}"), method="DELETE")
        api.call(source_path(f"releases/{release_id}/assets?name="
                             + urllib.parse.quote(path.name, safe="")), raw=data)
    revalidate(api, candidate)
    # Refuse a concurrent manual publication instead of modifying a public release.
    require_owned_draft(api.call(source_path(f"releases/{release_id}")), candidate)
    api.call(source_path(f"releases/{release_id}"), {
        "draft": False, "prerelease": version(value)[1] is not None,
        "make_latest": "true" if advance else "false",
    }, method="PATCH")


def revalidate(api, candidate):
    baseline_check(candidate)
    require(authorize(api, candidate["ref"]) == candidate,
            "Source ref/CI changed since discovery; refusing stale artifact")


def publish(api, candidate, directory):
    kind, value = ref_policy(candidate["ref"])
    if kind == "release":
        existing = release_record(api, candidate["ref"])
        if existing:
            require_owned_draft(existing, candidate)
    require({p.name for p in directory.iterdir()} ==
            ({"image.tar", "assets"} if kind == "release" else {"image.tar"}),
            "Unexpected artifact payload")
    validate_oci(directory / "image.tar",
                 ["amd64", "arm64"] if kind == "release" else ["amd64"],
                 value if kind == "release" else None, candidate["sha"])
    if kind == "release":
        require((directory / "assets").is_dir() and not (directory / "assets").is_symlink(),
                "Unsafe asset directory")
        validate_assets(directory / "assets", value)
    advance = False
    if kind == "release" and version(value)[1] is None:
        advance = latest_allowed(value, registry_latest())
        # Do not let discovery of historical successful runs move latest backwards.
        stable = []
        for ref in candidates(api):
            ref_kind, ref_version = ref_policy(ref)
            if ref_kind == "release" and version(ref_version)[1] is None:
                stable.append(version(ref_version)[0])
        advance = advance and version(value)[0] == max(stable)
    revalidate(api, candidate)
    tags = image_tags(candidate)
    if kind == "release":
        for arch in ("amd64", "arm64"):
            revalidate(api, candidate)
            subprocess.run(
                ["skopeo", "copy", "--override-os", "linux", "--override-arch", arch,
                 "--preserve-digests", "oci-archive:" + str(directory / "image.tar"),
                 "docker://" + IMAGE + ":" + tags[0] + "-" + arch],
                check=True,
            )
    for tag in tags:
        revalidate(api, candidate)
        subprocess.run(["skopeo", "copy", "--all", "--preserve-digests",
                        "oci-archive:" + str(directory / "image.tar"),
                        "docker://" + IMAGE + ":" + tag], check=True)
    if kind == "release":
        revalidate(api, candidate)
        if advance:
            # Recheck the registry immediately before the mutable latest write.
            advance = latest_allowed(value, registry_latest())
        if advance:
            revalidate(api, candidate)
            subprocess.run(["skopeo", "copy", "--all", "--preserve-digests",
                            "oci-archive:" + str(directory / "image.tar"),
                            "docker://" + IMAGE + ":latest"], check=True)
        revalidate(api, candidate)
        publish_release(api, candidate, directory / "assets", advance)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["discover", "publish", "failure"])
    args = parser.parse_args()
    require(os.environ.get("GITHUB_REPOSITORY", "").lower() == PUBLISHER.lower(),
            "Wrong publisher repository")
    require(os.environ.get("GITHUB_REF") == "refs/heads/main", "Publisher main only")
    require(os.environ.get("PUBLISHING_ENABLED") == "true", "Publication is not enabled")
    api = API()
    if args.command == "discover":
        candidate = discover(api, os.environ.get("SOURCE_REF", ""))
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write("candidate=" + (json.dumps(candidate, separators=(",", ":"))
                                         if candidate else "") + "\n")
        return
    candidate = json.loads(os.environ["CANDIDATE"])
    ref_policy(candidate["ref"])
    if args.command == "failure":
        set_status(api, candidate, "failure")
        return
    # Registry auth lives in a temporary private file, never in process arguments.
    require(os.environ.get("GHCR_PUBLISH_TOKEN") and os.environ.get("GHCR_USERNAME"),
            "GHCR environment credential/username missing")
    with tempfile.TemporaryDirectory() as temporary:
        authfile = Path(temporary) / "auth.json"
        os.environ["REGISTRY_AUTH_FILE"] = str(authfile)
        subprocess.run(["skopeo", "login", "--authfile", str(authfile),
                        "--username", os.environ["GHCR_USERNAME"], "--password-stdin",
                        "ghcr.io"], input=os.environ["GHCR_PUBLISH_TOKEN"].encode(), check=True)
        publish(api, candidate, Path("payload"))
    set_status(api, candidate, "success")


if __name__ == "__main__":
    main()
