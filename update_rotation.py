#!/usr/bin/env python3
"""Owner-controlled public trust-root proposals; never accept update private keys."""

import argparse
import base64
import json
import os
import re
import subprocess
import time
import urllib.parse

import publisher as p
import release_start as r

TITLE = "Rotate update signing key"
CHECKLIST = """Recommended signing-key lifetime: one year.

- [ ] Generate the new key offline: `openssl genpkey -algorithm ed25519 -out loom-update.pem`.
- [ ] Keep the private key offline; extract its raw 32-byte public key as standard base64.
- [ ] Run **Propose update trust key** with the public key and a unique key id.
- [ ] Merge and release the Loom PR that trusts both old and new keys.
- [ ] After that release, add the new public key to the publisher's accepted verifier set
      through a reviewed PR, then swap `production-publish/LOOM_UPDATE_SIGNING_KEY`.
- [ ] Verify a normal protected publication signed with the new key.
- [ ] One or two releases later, remove the old public key from Loom through a reviewed PR,
      only when supported installations have the new trust root.
- [ ] Remove the old publisher verifier when old signed releases no longer need verification.

Never put update private keys in CI inputs, issues, PRs, logs or commits.
See README.md for the overlap and compromise procedures.
"""


def guard(workflow, reminder=False):
    p.require(os.environ.get("GITHUB_REPOSITORY") == p.PUBLISHER
              and os.environ.get("GITHUB_REPOSITORY_ID") == r.PUBLISHER_ID
              and os.environ.get("GITHUB_REPOSITORY_OWNER_ID") == r.OWNER_ID,
              "Wrong rotation repository/owner identity")
    p.require(os.environ.get("GITHUB_REF") == r.MAIN
              and os.environ.get("GITHUB_WORKFLOW_REF") ==
              p.PUBLISHER + f"/.github/workflows/{workflow}@{r.MAIN}",
              "Rotation requires its protected-main workflow")
    if reminder and os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        return
    r.owner_intent()


def public_key(value):
    p.require(isinstance(value, str), "Public key must be base64 text")
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError:
        raise ValueError("Public key must be strict standard base64") from None
    p.require(len(raw) == 32 and base64.b64encode(raw).decode("ascii") == value,
              "Public key must be canonical base64 of exactly 32 bytes")
    return value


def inputs():
    key = public_key(os.environ.get("UPDATE_PUBLIC_KEY", ""))
    key_id = os.environ.get("UPDATE_KEY_ID", "")
    p.require(re.fullmatch(r"[a-z][a-z0-9-]{0,39}", key_id),
              "Key id must match [a-z][a-z0-9-]{0,39}")
    return key_id, key


def app_permissions():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    p.require(os.environ.get("LOOM_APP_ID") == r.APP_ID, "Unexpected rotation App")
    try:
        key = serialization.load_pem_private_key(
            os.environ.get("LOOM_APP_PRIVATE_KEY", "").encode("ascii"), password=None)
    except (ValueError, TypeError):
        raise ValueError("Invalid GitHub App private key") from None
    p.require(isinstance(key, rsa.RSAPrivateKey), "GitHub App key must be RSA")

    def encoded(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")

    now = int(time.time())
    unsigned = encoded(b'{"alg":"RS256","typ":"JWT"}') + b"." + encoded(json.dumps(
        {"iat": now - 60, "exp": now + 300, "iss": int(r.APP_ID)},
        separators=(",", ":")).encode())
    jwt = unsigned + b"." + encoded(key.sign(unsigned, padding.PKCS1v15(), hashes.SHA256()))
    # App-authenticated read checks the installation's already-approved permissions.
    installation = p.API(jwt.decode()).call(p.source_path("installation"))
    p.require(installation.get("app_id") == int(r.APP_ID), "Unexpected source installation")
    permissions = installation.get("permissions") or {}
    p.require(permissions.get("contents") == "write"
              and permissions.get("pull_requests") == "write",
              "Existing Loom App installation needs Contents and Pull requests write. "
              "Ask the owner to review/approve those permissions; no permissions were granted.")


def reminder():
    def gh(*args):
        return json.loads(subprocess.run(
            ["gh", *args], check=True, capture_output=True, text=True).stdout)

    existing = gh("issue", "list", "--repo", p.PUBLISHER, "--state", "open",
                  "--search", f'in:title "{TITLE}"', "--limit", "100", "--json", "title,url")
    if any(issue["title"] == TITLE for issue in existing):
        print("An open rotation issue already exists; no duplicate created.")
        return
    # Workflow uses only the publisher repository's issues-scoped GITHUB_TOKEN.
    issue = gh("api", f"repos/{p.PUBLISHER}/issues", "--method", "POST",
               "-f", "title=" + TITLE, "-f", "body=" + CHECKLIST)
    print(issue["html_url"])


def integration_branch(api, requested):
    if requested:
        p.require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", requested)
                  and ".." not in requested and "//" not in requested
                  and not requested.endswith(("/", ".", ".lock")),
                  "Invalid integration base branch")
        return requested
    branches = []
    for branch in api.pages(p.source_path("branches")):
        name = branch["name"]
        if re.fullmatch(rf"prelease/{p.CORE}", name):
            branches.append((p.version(name.split("/")[1])[0], name))
    p.require(branches, "No prelease/X.Y.Z integration branch; supply base_branch explicitly")
    return max(branches)[1]


def add_key(raw, key_id, key):
    def unique(pairs):
        result = {}
        for name, value in pairs:
            p.require(name not in result, "Duplicate trust-root key id")
            result[name] = value
        return result

    p.require(len(raw) <= 65536, "Trust-root file too large")
    keys = json.loads(raw, object_pairs_hook=unique)
    p.require(isinstance(keys, dict), "Trust root must be a JSON object")
    for value in keys.values():
        public_key(value)
    p.require(key_id not in keys, "Key id already exists; never replace an existing key")
    p.require(key not in keys.values(), "Public key already trusted")
    p.require(len(keys) < 4, "Rotation trust-root overlap is limited to four keys")
    keys[key_id] = key
    return (json.dumps(keys, indent=2) + "\n").encode()


def propose(api):
    key_id, key = inputs()
    base = integration_branch(api, os.environ.get("BASE_BRANCH", ""))
    ref = api.call(p.source_path("git/ref/heads/" + urllib.parse.quote(base, safe="")))
    sha = ref["object"]["sha"]
    p.require(ref["object"]["type"] == "commit" and p.SHA.fullmatch(sha), "Invalid base commit")
    path = "internal/updates/trusted-keys.json"
    file = api.call(p.source_path(f"contents/{path}?ref={sha}"))
    p.require(file.get("type") == "file" and file.get("encoding") == "base64",
              "Missing source trust root")
    raw = base64.b64decode(file["content"].replace("\n", ""), validate=True)
    updated = add_key(raw, key_id, key)
    commit = api.call(p.source_path("git/commits/" + sha))
    tree = api.call(p.source_path("git/trees"), {
        "base_tree": commit["tree"]["sha"],
        "tree": [{"path": path, "mode": "100644", "type": "blob",
                  "content": updated.decode()}],
    })
    new_commit = api.call(p.source_path("git/commits"), {
        "message": f"Trust update signing key {key_id}\n\n"
                   "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>",
        "tree": tree["sha"], "parents": [sha],
    })
    branch = "update-trust/" + key_id
    api.call(p.source_path("git/refs"), {"ref": "refs/heads/" + branch,
                                       "sha": new_commit["sha"]})
    pr = api.call(p.source_path("pulls"), {
        "title": f"Trust update signing key {key_id}", "head": branch, "base": base,
        "body": "Adds one public Ed25519 key without removing existing trust roots.\n\n"
                "Review, merge and release this overlapping-trust build before changing "
                "the publisher's active signing key. No update private key was handled.",
    })
    print(pr["html_url"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["reminder", "guard", "permissions", "propose"])
    command = parser.parse_args().command
    if command == "reminder":
        guard("update-key-reminder.yml", reminder=True)
        reminder()
    else:
        guard("propose-update-key.yml")
        inputs()
        if command == "permissions":
            app_permissions()
        elif command == "propose":
            propose(p.API())


if __name__ == "__main__":
    main()
