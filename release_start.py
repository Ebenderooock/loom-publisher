#!/usr/bin/env python3
"""Owner-only API control plane; never check out or execute source code."""

import argparse
import json
import os
from pathlib import Path
import time
import urllib.error

import publisher as p

OWNER_ID = "4662239"
PUBLISHER_ID = "1459424406"
SOURCE_ID = 1230254566
APP_ID = "5789004"
MAIN = "refs/heads/main"
START_WORKFLOW = p.PUBLISHER + "/.github/workflows/start-release.yml@" + MAIN
PUBLISH_WORKFLOW = p.PUBLISHER + "/.github/workflows/publish.yml@" + MAIN


def owner_intent():
    p.require(os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch",
              "Release start requires explicit workflow_dispatch")
    p.require(os.environ.get("GITHUB_ACTOR_ID") == OWNER_ID
              and os.environ.get("GITHUB_ACTOR") == "Ebenderooock"
              and os.environ.get("GITHUB_TRIGGERING_ACTOR") == "Ebenderooock",
              "Only the repository owner may start or rerun a release")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    p.require((event.get("sender") or {}).get("id") == int(OWNER_ID),
              "Dispatch sender must be the repository owner, not an App")


def publisher_guard():
    p.require(os.environ.get("GITHUB_REPOSITORY") == p.PUBLISHER
              and os.environ.get("GITHUB_REPOSITORY_ID") == PUBLISHER_ID
              and os.environ.get("GITHUB_REPOSITORY_OWNER_ID") == OWNER_ID,
              "Wrong publisher repository/owner identity")
    p.require(os.environ.get("GITHUB_REF") == MAIN, "Publisher main only")
    workflow = os.environ.get("GITHUB_WORKFLOW_REF")
    p.require(workflow in (START_WORKFLOW, PUBLISH_WORKFLOW),
              "Untrusted caller workflow; only protected main entry points are allowed")
    if workflow == START_WORKFLOW:
        owner_intent()
    else:
        p.require(os.environ.get("GITHUB_EVENT_NAME") in ("schedule", "workflow_dispatch"),
                  "Unsupported publisher event")
        p.require(not os.environ.get("EXPECTED_SOURCE_SHA"),
                  "Pinned release initiation requires the owner-only Start release caller")


def start_guard():
    publisher_guard()
    p.require(os.environ.get("GITHUB_WORKFLOW_REF") == START_WORKFLOW,
              "Release initiation requires Start release on main")
    p.require(os.environ.get("LOOM_APP_ID") == APP_ID, "Unexpected release App identity")
    p.require(os.environ.get("PUBLISHING_ENABLED") == "true", "Publication is not enabled")
    return release_ref(os.environ.get("VERSION", ""))


def release_ref(value):
    p.version(value)
    return "refs/tags/v" + value


def source_identity(api):
    repository = api.call("/repos/" + p.SOURCE + "?")
    p.require(repository.get("id") == SOURCE_ID
              and repository.get("default_branch") == "main",
              "Source repository identity/default branch changed")


def main_tip(api):
    source_identity(api)
    result = api.call(p.source_path("git/ref/heads/main"))
    obj = result.get("object") or {}
    p.require(result.get("ref") == MAIN and obj.get("type") == "commit"
              and p.SHA.fullmatch(obj.get("sha", "")),
              "Invalid source main ref/commit identity")
    return obj["sha"]


def readiness_report(message, summary=False):
    print(message, flush=True)
    if summary and os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(message + "\n\n")


def wait_main_ci(api, timeout=1800, interval=20):
    started = time.monotonic()
    deadline = started + timeout
    sha = main_tip(api)
    workflow_url = f"https://github.com/{p.SOURCE}/actions/workflows/ci.yml"
    last = workflow_url
    observed = (0, 0)
    recovery = ("Inspect source push CI, then rerun Start release as owner with the same "
                "version once CI passes. No tag was created by this inspection.")
    readiness_report(f"Pinned {MAIN} at `{sha}`; waiting up to {timeout}s for authoritative "
                     f"push CI: {workflow_url}", summary=True)
    while True:
        p.require(main_tip(api) == sha,
                  f"Source main moved from pinned {sha}; refusing to repin. "
                  f"Review current main before retrying: {last}")
        p.validate_workflow(api)
        runs = [run for run in api.pages(p.source_path(
            f"actions/workflows/{p.CI_ID}/runs?event=push&head_sha={sha}"), "workflow_runs")
                if run.get("head_branch") == p.short_ref(MAIN)]
        state = "missing (no main push CI run yet)"
        run = None
        if runs:
            # Validate identity independently of readiness, including pending list/detail data.
            for item in runs:
                p.require(p.valid_ci(item | {"status": "completed", "conclusion": "success"},
                                     MAIN, sha),
                          f"Invalid main push CI identity metadata: {workflow_url}")
            latest = max(runs, key=lambda item: (item["id"], item["run_attempt"]))
            last = f"https://github.com/{p.SOURCE}/actions/runs/{latest['id']}"
            run = api.call(p.source_path(f"actions/runs/{latest['id']}"))
            p.require(p.valid_ci(run | {"status": "completed", "conclusion": "success"}, MAIN, sha)
                      and run["id"] == latest["id"]
                      and run["run_attempt"] >= latest["run_attempt"],
                      f"Invalid main push CI run/attempt identity metadata: {last}")
            identity = (run["id"], run["run_attempt"])
            p.require(identity >= observed,
                      f"Main push CI run/attempt regressed from {observed}: {last}")
            observed = identity
            status = run.get("status")
            p.require(status in ("queued", "in_progress", "pending", "requested",
                                 "waiting", "action_required", "completed"),
                      f"Invalid main push CI status metadata: {last}")
            state = f"{status}, attempt {run['run_attempt']}"
            if status == "completed":
                state += f", conclusion {run.get('conclusion')}"
        elapsed = time.monotonic() - started
        readiness_report(f"Main push CI: {state}; elapsed {elapsed:.0f}s/{timeout}s; "
                         f"{MAIN} at {sha}: {last}")
        p.require(main_tip(api) == sha,
                  f"Source main moved from pinned {sha}; refusing to repin: {last}")
        if run and run["status"] == "completed" and run.get("conclusion") != "success":
            message = f"Latest main push CI completed without success ({state}): {last}. {recovery}"
            readiness_report(message, summary=True)
            raise ValueError(message)
        if time.monotonic() >= deadline:
            message = f"Timed out waiting for main push CI ({state}): {last}. {recovery}"
            readiness_report(message, summary=True)
            raise ValueError(message)
        if run and p.valid_ci(run, MAIN, sha):
            candidate = p.authorize(api, MAIN)
            p.require(candidate["sha"] == sha and candidate["ci_id"] == run["id"]
                      and candidate["ci_attempt"] == run["run_attempt"],
                      f"Source main/CI changed during readiness authorization: {last}")
            p.baseline_check(candidate)
            readiness_report(f"Main push CI ready after {elapsed:.0f}s at `{sha}`: {last}",
                             summary=True)
            return candidate
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


def source_snapshot(api, ref, expected=None):
    p.ref_policy(ref)
    p.require(ref.startswith("refs/tags/v"), "Release start requires a version tag")
    source_identity(api)
    candidate = p.authorize(api, MAIN)
    p.baseline_check(candidate)
    if expected is not None:
        p.require(candidate == expected, "Source main/CI changed since release approval")
    p.release_notes(api, candidate | {"ref": ref})
    return candidate


def tag_message(ref, sha):
    return (f"Owner-initiated Loom {p.short_ref(ref)}\n"
            f"Source: {sha}\nPublisher: https://github.com/{p.PUBLISHER}\n")


def matching_tag(api, ref, sha):
    try:
        result = api.call(p.source_path("git/ref/" + ref.removeprefix("refs/")))
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        exc.close()
        return False
    obj = result["object"]
    p.require(result.get("ref") == ref and obj.get("type") == "tag"
              and p.SHA.fullmatch(obj.get("sha", "")),
              "Conflicting existing tag: an annotated release-start tag is required")
    tag = api.call(p.source_path("git/tags/" + obj["sha"]))
    p.require(tag.get("tag") == p.short_ref(ref)
              and (tag.get("object") or {}).get("type") == "commit"
              and (tag.get("object") or {}).get("sha") == sha
              and tag.get("message") == tag_message(ref, sha),
              "Conflicting existing tag: target or release-start annotation differs")
    return True


def create_tag(api, ref, expected):
    p.require(isinstance(expected, dict) and p.SHA.fullmatch(expected.get("sha", "")),
              "Missing approved source snapshot")
    source_snapshot(api, ref, expected)
    sha = expected["sha"]
    if matching_tag(api, ref, sha):
        print(f"Resuming immutable {ref} at {sha}; no tag update")
        return
    obj = api.call(p.source_path("git/tags"), {
        "tag": p.short_ref(ref), "message": tag_message(ref, sha),
        "object": sha, "type": "commit",
    })
    p.require(p.SHA.fullmatch(obj.get("sha", "")), "Invalid annotated tag object SHA")
    # Validate immediately before the only ref-creation write. No update/delete API.
    source_snapshot(api, ref, expected)
    try:
        api.call(p.source_path("git/refs"), {
            "ref": ref, "sha": obj["sha"],
        })
    except urllib.error.HTTPError as exc:
        if exc.code != 422:
            raise
        exc.close()
        # A concurrent matching creation is safe; never force or replace a ref.
        p.require(matching_tag(api, ref, sha), "Tag creation rejected; no matching tag")
    p.require(matching_tag(api, ref, sha), "Created tag failed verification")


def wait_ci(api, ref, sha, timeout=1800, interval=20):
    deadline = time.monotonic() + timeout
    last = f"https://github.com/{p.SOURCE}/actions/workflows/ci.yml"
    print(f"Waiting up to {timeout}s for {ref} at {sha}: {last}. "
          "The tag will be preserved on failure or timeout.", flush=True)
    while True:
        p.validate_workflow(api)
        p.require(matching_tag(api, ref, sha), "Release tag disappeared")
        runs = [r for r in api.pages(p.source_path(
            f"actions/workflows/{p.CI_ID}/runs?event=push&head_sha={sha}"), "workflow_runs")
                if r.get("event") == "push" and r.get("head_sha") == sha
                and r.get("head_branch") == p.short_ref(ref)]
        if runs:
            p.require(all(type(r.get("id")) is int and r["id"] > 0 for r in runs),
                      "Invalid CI run identity")
            latest = max(runs, key=lambda r: (r["id"], r.get("run_attempt", 0)))
            run = api.call(p.source_path(f"actions/runs/{latest['id']}"))
            last = f"https://github.com/{p.SOURCE}/actions/runs/{latest['id']}"
            p.require(run.get("id") == latest["id"]
                      and type(run.get("run_attempt")) is int
                      and run["run_attempt"] >= latest.get("run_attempt", 0),
                      f"CI run/attempt identity changed: {last}")
            print(f"Tag push CI attempt {run['run_attempt']}: {last}", flush=True)
            if run.get("status") == "completed":
                p.require(p.valid_ci(run, ref, sha), f"Latest tag push CI failed authorization: {last}")
                candidate = p.authorize(api, ref)
                p.require(candidate["sha"] == sha and candidate["ci_id"] == run["id"]
                          and candidate["ci_attempt"] == run["run_attempt"],
                          f"Tag CI changed during authorization: {last}")
                p.baseline_check(candidate)
                return last
        p.require(time.monotonic() < deadline,
                  f"Timed out waiting for exact tag push CI: {last}. "
                  "Tag preserved; inspect CI and rerun Start release with the same version.")
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


def verify_publication_input(api):
    publisher_guard()
    expected = os.environ.get("EXPECTED_SOURCE_SHA", "")
    if os.environ.get("GITHUB_WORKFLOW_REF") != START_WORKFLOW:
        return
    p.require(p.SHA.fullmatch(expected), "Owner-started publication requires a pinned source SHA")
    ref = os.environ.get("SOURCE_REF", "")
    snapshot = source_snapshot(api, ref)
    p.require(snapshot["sha"] == expected, "Source main moved before publication")
    p.require(matching_tag(api, ref, expected), "Pinned release tag is missing")
    candidate = p.authorize(api, ref)
    p.baseline_check(candidate)
    p.require(candidate["sha"] == expected, "Release tag changed before publication")


def output(name, value):
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        stream.write(f"{name}={value}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("guard", "publisher-guard", "inspect",
                                           "tag", "wait", "verify"))
    command = parser.parse_args().command
    if command == "publisher-guard":
        publisher_guard()
        if os.environ.get("GITHUB_WORKFLOW_REF") == START_WORKFLOW:
            p.require(p.SHA.fullmatch(os.environ.get("EXPECTED_SOURCE_SHA", "")),
                      "Owner-started publication requires a pinned source SHA")
        return
    if command == "verify":
        verify_publication_input(p.API())
        return
    ref = start_guard()
    if command == "guard":
        return
    api = p.API()
    if command == "inspect":
        snapshot = source_snapshot(api, ref, wait_main_ci(api))
        # Reject conflicts before requesting a contents-write token.
        matching_tag(api, ref, snapshot["sha"])
        output("snapshot", json.dumps(snapshot, separators=(",", ":")))
        output("sha", snapshot["sha"])
        output("source_ref", ref)
    elif command == "tag":
        create_tag(api, ref, json.loads(os.environ["SOURCE_SNAPSHOT"]))
    else:
        sha = os.environ["EXPECTED_SOURCE_SHA"]
        p.require(p.SHA.fullmatch(sha), "Invalid pinned source SHA")
        output("ci_url", wait_ci(api, ref, sha))


if __name__ == "__main__":
    main()
