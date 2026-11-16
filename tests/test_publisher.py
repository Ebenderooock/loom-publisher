import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import zipfile

import build
import publisher as p


SHA = "a" * 40
OTHER = "b" * 40
URL = "https://github.com/Ebenderooock/loom-publisher/actions/runs/123"


def changelog_file(text):
    return {"type": "file", "encoding": "base64",
            "content": base64.encodebytes(text.encode()).decode()}


def run(ref="refs/heads/main", **changes):
    result = {
        "id": 100, "run_attempt": 2, "check_suite_id": 111,
        "workflow_id": p.CI_ID, "path": p.CI_PATH,
        "event": "push", "status": "completed", "conclusion": "success",
        "head_sha": SHA, "head_branch": p.short_ref(ref),
        "head_repository": {"full_name": p.SOURCE},
        "created_at": "2026-01-02T03:04:05Z",
    }
    result.update(changes)
    return result


class FakeAPI:
    def __init__(self, ref="refs/heads/main"):
        self.ref = ref
        self.tip = SHA
        self.runs = [run(ref)]
        self.statuses = []
        self.writes = []
        self.comparison = {"status": "ahead", "merge_base_commit": {"sha": SHA}}
        self.diff = {"status": "ahead", "files": [{"filename": "internal/main.go"}]}
        self.workflow = {"id": p.CI_ID, "path": p.CI_PATH, "state": "active"}
        self.changelog = changelog_file(f"## [{p.ref_policy(ref)[1]}] - 2026-01-02\n\n- Fixes.")

    def call(self, path, data=None, method=None, raw=None):
        path = path.removeprefix(p.source_path(""))
        if data is not None:
            self.writes.append((path, data))
            return data
        if path == "actions/workflows/ci.yml":
            return self.workflow
        if path == "contents/CHANGELOG.md?ref=" + SHA:
            return self.changelog
        if path.startswith("git/ref/"):
            return {"object": {"sha": self.tip, "type": "commit"}}
        if path == "commits/" + SHA:
            return {"commit": {"committer": {"date": "2026-01-02T03:04:05Z"}}}
        if path.startswith("compare/"):
            return self.comparison if path.endswith("...main") else self.diff
        if path.startswith("check-suites/"):
            return {"id": 111, "app": {"slug": "github-actions"},
                    "head_branch": p.short_ref(self.ref), "head_sha": SHA,
                    "before": OTHER, "after": SHA}
        raise AssertionError(path)

    def pages(self, path, key=None):
        if "/runs?" in path:
            return iter(self.runs)
        if path.endswith("/statuses"):
            return iter(self.statuses)
        if path.endswith("/branches"):
            return iter([{"name": "main"}, {"name": "prerelease/1.2.3"},
                         {"name": "prelease/1.2.3"}, {"name": "evil"}])
        if path.endswith("/tags"):
            return iter([{"name": "v1.2.3"}, {"name": "v2.0.0"}, {"name": "garbage"}])
        raise AssertionError(path)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        baseline = patch.dict(os.environ, PUBLISHING_NOT_BEFORE="2026-01-01T00:00:00Z")
        baseline.start()
        self.addCleanup(baseline.stop)

    def test_allowed_refs(self):
        for ref, expected in {
            "refs/heads/main": ("main", None),
            "refs/heads/prelease/0.5.0": ("prelease", "0.5.0"),
            "refs/heads/prerelease/0.5.0": ("prerelease", "0.5.0"),
            "refs/tags/v1.2.3-rc.1+build.2": ("release", "1.2.3-rc.1+build.2"),
        }.items():
            self.assertEqual(p.ref_policy(ref), expected)

    def test_rejects_bad_refs(self):
        for ref in (
            "main", SHA, "refs/heads/Main", "refs/heads/prelease/01.2.3",
            "refs/heads/prerelease/1.2.3/evil", "refs/heads/prelease/1.2.3-rc",
            "refs/tags/1.2.3", "refs/tags/v1.2", "refs/tags/v01.2.3",
            "refs/tags/v1.2.3-01", "refs/tags/v1.2.3+",
            "refs/tags/v1.2.3\n", "refs/tags/v1.2.3;echo evil",
            "refs/tags/v1.2.3-" + "x" * 110, "refs/heads/main\n",
        ):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                p.ref_policy(ref)

    def test_main_tag(self):
        candidate = p.authorize(FakeAPI(), "refs/heads/main")
        tags = p.image_tags(candidate)
        self.assertEqual(tags, [f"main-aaaaaaa-{candidate['timestamp']}"])
        self.assertNotIn("latest", tags)

    def test_channels_are_isolated(self):
        for kind in ("prelease", "prerelease"):
            ref = f"refs/heads/{kind}/0.5.0"
            tags = p.image_tags(p.authorize(FakeAPI(ref), ref))
            self.assertEqual(tags, [f"{kind}-0.5.0-aaaaaaa-100-2", f"{kind}-0.5.0"])
            self.assertNotIn("main", tags)
            self.assertNotIn("latest", tags)

    def test_build_metadata_safe_docker_tags(self):
        ref = "refs/tags/v1.2.3-rc.1+abc.2"
        self.assertEqual(p.image_tags(p.authorize(FakeAPI(ref), ref)),
                         ["1.2.3-rc.1_abc.2"])

    def test_latest_never_regresses_or_accepts_prerelease(self):
        for new, old, expected in (
            ("1.2.3", None, True), ("1.2.3-rc.1", None, False),
            ("1.2.3", "1.2.4", False), ("1.2.3", "v2.0.0", False),
            ("2.0.0", "1.99.99", True), ("1.2.3+two", "1.2.3+one", False),
            ("1.2.3", "1.2.3", True),
        ):
            self.assertEqual(p.latest_allowed(new, old), expected)
        for old in ("garbage", "", "1.2.3-rc.1"):
            with self.assertRaises(ValueError):
                p.latest_allowed("2.0.0", old)

    def test_ci_requires_successful_push_and_exact_identity(self):
        self.assertTrue(p.valid_ci(run(), "refs/heads/main", SHA))
        for change in (
            {"event": "pull_request"}, {"event": "workflow_dispatch"},
            {"status": "queued"}, {"status": "in_progress"},
            {"conclusion": "skipped"}, {"conclusion": "failure"},
            {"conclusion": "cancelled"}, {"conclusion": None},
            {"workflow_id": p.CI_ID + 1}, {"path": ".github/workflows/evil.yml"},
            {"head_sha": OTHER}, {"head_branch": "prelease/0.1.0"},
            {"head_repository": {"full_name": "attacker/loom"}},
            {"head_repository": None},
            {"run_attempt": 0}, {"id": "123"},
        ):
            with self.subTest(change=change):
                self.assertFalse(p.valid_ci(run(**change), "refs/heads/main", SHA))

    def test_status_creator_context_url_and_newest_state(self):
        status = {"creator": {"login": "publisher[bot]"}, "state": "success",
                  "context": "loom-publisher/main", "target_url": URL}
        self.assertTrue(p.trusted_success([status], "refs/heads/main", "publisher"))
        for change in (
            {"creator": {"login": "attacker"}}, {"state": "pending"},
            {"creator": None},
            {"state": "failure"}, {"context": "loom-publisher/v1.2.3"},
            {"target_url": URL + "/jobs/1"}, {"target_url": URL + "?spoof=true"},
            {"target_url": URL.replace("github.com", "github.com.evil")},
            {"target_url": URL.replace("loom-publisher", "loom")},
        ):
            with self.subTest(change=change):
                self.assertFalse(p.trusted_success([status | change], "refs/heads/main", "publisher"))
        self.assertFalse(p.trusted_success(
            [status | {"state": "pending"}, status], "refs/heads/main", "publisher"))
        self.assertTrue(p.trusted_success(
            [status | {"creator": {"login": "attacker"}}, status],
            "refs/heads/main", "publisher"))

    def test_authorizes_current_tip(self):
        candidate = p.authorize(FakeAPI(), "refs/heads/main")
        self.assertEqual(candidate["sha"], SHA)
        self.assertEqual(candidate["ci_attempt"], 2)

    def test_failed_or_pending_newer_run_revokes_old_success(self):
        for state in ("queued", "in_progress", "completed"):
            api = FakeAPI()
            api.runs.append(run(id=101, status=state, conclusion="failure"))
            with self.assertRaisesRegex(ValueError, "not successful"):
                p.authorize(api, "refs/heads/main")

    def test_failed_rerun_revokes_authority(self):
        api = FakeAPI()
        api.runs = [run(run_attempt=3, conclusion="failure")]
        with self.assertRaises(ValueError):
            p.authorize(api, "refs/heads/main")

    def test_wrong_workflow_identity_fails(self):
        api = FakeAPI()
        api.workflow["id"] = 42
        with self.assertRaises(ValueError):
            p.authorize(api, "refs/heads/main")

    def test_releases_must_be_on_main(self):
        ref = "refs/tags/v1.2.3"
        for comparison in (
            {"status": "diverged", "merge_base_commit": {"sha": OTHER}},
            {"status": "behind", "merge_base_commit": {"sha": OTHER}},
            {"status": "ahead", "merge_base_commit": {"sha": OTHER}},
        ):
            api = FakeAPI(ref)
            api.comparison = comparison
            with self.assertRaises(ValueError):
                p.authorize(api, ref)

    def test_ref_moving_during_authorization_fails(self):
        api = FakeAPI()
        original = api.call

        def moving(path, **kwargs):
            result = original(path, **kwargs)
            if path == p.source_path("commits/" + SHA):
                api.tip = OTHER
            return result

        api.call = moving
        with self.assertRaisesRegex(ValueError, "moved"):
            p.authorize(api, "refs/heads/main")

    def test_docs_filter_uses_whole_push_diff_and_both_rename_paths(self):
        api = FakeAPI()
        candidate = p.authorize(api, "refs/heads/main")
        for files, expected in (
            ([{"filename": "README.md"}, {"filename": "docs/example.yaml"}], True),
            ([{"filename": "docs/readme.md", "previous_filename": "internal/main.go"}], False),
            ([{"filename": "README.md"}, {"filename": "web/package.json"}], False),
            ([{"filename": "README.md"}] * 300, False),
        ):
            api.diff["files"] = files
            self.assertEqual(p.documentation_only(api, candidate), expected)

    def test_docs_only_push_does_not_authorize_an_automatic_build(self):
        api = FakeAPI()
        api.diff["files"] = [{"filename": "docs/deployment.md"}]
        with patch("publisher.candidates", return_value=["refs/heads/main"]):
            with patch.dict(os.environ, APP_SLUG="publisher", PUBLISHER_RUN_URL=URL):
                self.assertIsNone(p.discover(api, ""))
                self.assertEqual(api.writes, [])
                self.assertIsNotNone(p.discover(api, "refs/heads/main"))

    def test_prepublication_revalidation_rejects_rerun_and_moved_tip(self):
        api = FakeAPI()
        candidate = p.authorize(api, "refs/heads/main")
        api.runs[0]["run_attempt"] += 1
        with self.assertRaisesRegex(ValueError, "changed"):
            p.revalidate(api, candidate)
        api.tip = OTHER
        with self.assertRaises(ValueError):
            p.revalidate(api, candidate)

    def test_discovery_skips_only_trusted_success(self):
        api = FakeAPI()
        with patch.dict(os.environ, APP_SLUG="publisher", PUBLISHER_RUN_URL=URL):
            candidate = p.discover(api, "refs/heads/main")
        self.assertEqual(candidate["sha"], SHA)
        self.assertEqual(api.writes[-1][1]["state"], "pending")
        api.statuses = [{"context": "loom-publisher/main", "state": "success",
                         "creator": {"login": "publisher[bot]"}, "target_url": URL}]
        with patch.dict(os.environ, APP_SLUG="publisher"):
            self.assertIsNone(p.discover(api, "refs/heads/main"))

    def test_candidate_list_includes_both_spellings_and_newest_tags_first(self):
        refs = p.candidates(FakeAPI())
        self.assertEqual(refs[:2], ["refs/tags/v2.0.0", "refs/tags/v1.2.3"])
        self.assertIn("refs/heads/prelease/1.2.3", refs)
        self.assertIn("refs/heads/prerelease/1.2.3", refs)
        self.assertNotIn("refs/heads/evil", refs)

    def test_registry_lookup_fails_closed(self):
        for error in ("unauthorized", "timeout", "TLS error"):
            with patch("publisher.subprocess.run", return_value=subprocess.CompletedProcess(
                    [], 1, "", error)), self.assertRaises(ValueError):
                p.registry_latest()
        with patch("publisher.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 1, "", "manifest unknown")):
            self.assertIsNone(p.registry_latest())
        with patch("publisher.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, '{"config": {}}', "")), self.assertRaises(ValueError):
            p.registry_latest()

    def test_baseline_is_mandatory_and_blocks_history(self):
        for baseline in ("", "yesterday", "2026-01-01", "2026-01-03T00:00:00Z"):
            with patch.dict(os.environ, PUBLISHING_NOT_BEFORE=baseline):
                with self.assertRaises(ValueError):
                    p.baseline_check({"ci_created_at": "2026-01-02T03:04:05Z"})

    def test_existing_releases_immutable_except_matching_app_drafts(self):
        candidate = p.authorize(FakeAPI("refs/tags/v1.2.3"), "refs/tags/v1.2.3")
        draft = {"draft": True, "author": {"login": "publisher[bot]"},
                 "target_commitish": SHA, "tag_name": "v1.2.3",
                 "body": "Built by the trusted [publisher](" + URL + ")."}
        with patch.dict(os.environ, APP_SLUG="publisher"):
            p.require_owned_draft(draft, candidate)
            for change in (
                {"draft": False}, {"author": {"login": "attacker"}},
                {"target_commitish": OTHER}, {"tag_name": "v1.2.2"},
                {"body": "https://evil.invalid"},
            ):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    p.require_owned_draft(draft | change, candidate)

    def test_existing_public_release_not_dispatched(self):
        api = FakeAPI("refs/tags/v1.2.3")
        original = api.call

        def existing(path, **kwargs):
            if "/releases/tags/" in path:
                return {"draft": False}
            return original(path, **kwargs)

        api.call = existing
        with patch.dict(os.environ, APP_SLUG="publisher", PUBLISHER_RUN_URL=URL):
            with self.assertRaisesRegex(ValueError, "immutable"):
                p.discover(api, "refs/tags/v1.2.3")
        self.assertEqual(api.writes, [])

    def test_auto_discovery_does_not_repeat_failed_attempt(self):
        api = FakeAPI()
        candidate = p.authorize(api, "refs/heads/main")
        api.statuses = [{
            "context": "loom-publisher/main", "state": "failure",
            "creator": {"login": "publisher[bot]"}, "target_url": URL,
            "description": p.status_description(candidate, "failure"),
        }]
        with patch("publisher.candidates", return_value=["refs/heads/main"]):
            with patch.dict(os.environ, APP_SLUG="publisher", PUBLISHER_RUN_URL=URL):
                self.assertIsNone(p.discover(api, ""))
                self.assertEqual(api.writes, [])
                self.assertIsNotNone(p.discover(api, "refs/heads/main"))
                api.runs[0]["run_attempt"] += 1
                self.assertIsNotNone(p.discover(api, ""))

    def test_existing_public_release_is_skipped_during_poll(self):
        api = FakeAPI("refs/tags/v1.2.3")
        with patch("publisher.candidates", return_value=["refs/tags/v1.2.3"]):
            with patch("publisher.release_record", return_value={"draft": False}):
                with patch.dict(os.environ, APP_SLUG="publisher"):
                    self.assertIsNone(p.discover(api, ""))
        self.assertEqual(api.writes, [])

    def test_release_lookup_includes_drafts(self):
        draft = {"tag_name": "v1.2.3", "draft": True}

        class DraftAPI:
            def call(self, path):
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)

            def pages(self, path):
                return iter([draft])

        self.assertEqual(p.release_record(DraftAPI(), "refs/tags/v1.2.3"), draft)

    def test_discovery_does_not_swallow_permission_failure(self):
        api = FakeAPI()
        error = urllib.error.HTTPError("", 403, "Forbidden", {}, None)
        self.addCleanup(error.close)
        with patch("publisher.candidates", return_value=["refs/heads/main"]):
            with patch("publisher.authorize", side_effect=error):
                with patch.dict(os.environ, APP_SLUG="publisher"):
                    with self.assertRaises(urllib.error.HTTPError):
                        p.discover(api, "")

    def test_api_pagination_includes_later_pages(self):
        with patch.dict(os.environ, LOOM_TOKEN="test-only-not-a-real-token"):
            api = p.API()
        with patch.object(api, "call", side_effect=[list(range(100)), [100]]) as call:
            result = list(api.pages(p.source_path("commits/" + SHA + "/statuses")))
        self.assertEqual(len(result), 101)
        self.assertTrue(call.call_args_list[1].args[0].endswith("per_page=100&page=2"))

    def test_api_cannot_send_app_token_outside_source(self):
        with patch.dict(os.environ, LOOM_TOKEN="test-only-not-a-real-token"):
            api = p.API()
        for path in ("/repos/attacker/loom/releases", "https://evil.invalid/",
                     "/repos/" + p.SOURCE + "-evil/releases"):
            with self.assertRaisesRegex(ValueError, "outside source"):
                api.call(path)

    def test_activation_and_publisher_ref_gates_precede_credentials(self):
        for ref, enabled in (("refs/heads/main", ""), ("refs/heads/evil", "true"),
                             ("refs/tags/main", "true")):
            with patch.dict(os.environ, GITHUB_REPOSITORY=p.PUBLISHER,
                            GITHUB_REF=ref, PUBLISHING_ENABLED=enabled):
                with patch("sys.argv", ["publisher.py", "discover"]):
                    with patch("publisher.API") as api, self.assertRaises(ValueError):
                        p.main()
                    api.assert_not_called()


def oci_members(architectures=("amd64",), remote=False):
    members = {"oci-layout": b'{"imageLayoutVersion":"1.0.0"}'}

    def blob(data, media):
        content = data if isinstance(data, bytes) else json.dumps(data).encode()
        digest = hashlib.sha256(content).hexdigest()
        members["blobs/sha256/" + digest] = content
        return {"mediaType": p.OCI + media, "digest": "sha256:" + digest, "size": len(content)}

    manifests = []
    for arch in architectures:
        config = blob({"os": "linux", "architecture": arch, "config": {"Labels": {
            "org.opencontainers.image.version": "1.2.3",
            "org.opencontainers.image.revision": SHA,
        }}}, "image.config.v1+json")
        layer = blob(b"opaque layer: never extract me", "image.layer.v1.tar+gzip")
        if remote:
            layer["urls"] = ["https://evil.invalid/layer"]
        manifests.append(blob({"schemaVersion": 2, "config": config, "layers": [layer]},
                              "image.manifest.v1+json"))
    if len(manifests) > 1:
        manifests = [blob({"schemaVersion": 2, "manifests": manifests}, "image.index.v1+json")]
    members["index.json"] = json.dumps({"schemaVersion": 2, "manifests": manifests}).encode()
    return members


def write_oci(path, members, link=None):
    with tarfile.open(path, "w") as archive:
        for name, content in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            if name == link:
                member.type = tarfile.SYMTYPE
                member.linkname = "/tmp/evil"
                member.size = 0
                archive.addfile(member)
            else:
                archive.addfile(member, io.BytesIO(content))


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "image.tar"

    def test_valid_single_platform(self):
        write_oci(self.path, oci_members())
        p.validate_oci(self.path, ["amd64"])

    def test_valid_multiarch_and_labels(self):
        write_oci(self.path, oci_members(("amd64", "arm64")))
        p.validate_oci(self.path, ["amd64", "arm64"], "1.2.3", SHA)
        with self.assertRaisesRegex(ValueError, "metadata mismatch"):
            p.validate_oci(self.path, ["amd64", "arm64"], "1.2.4", SHA)

    def test_wrong_platform_and_duplicate_arch_rejected(self):
        for architectures in (("arm64",), ("amd64", "amd64")):
            write_oci(self.path, oci_members(architectures))
            with self.assertRaises(ValueError):
                p.validate_oci(self.path, ["amd64"])

    def test_traversal_and_links_rejected(self):
        for name in ("../publisher.py", "/etc/passwd", "blobs/sha256/../evil"):
            write_oci(self.path, oci_members() | {name: b"bad"})
            with self.assertRaises(ValueError):
                p.validate_oci(self.path, ["amd64"])
        write_oci(self.path, oci_members(), link="index.json")
        with self.assertRaises(ValueError):
            p.validate_oci(self.path, ["amd64"])

    def test_tampered_blob_rejected(self):
        members = oci_members()
        name = next(n for n in members if n.startswith("blobs/"))
        members[name] = b"x" * len(members[name])
        write_oci(self.path, members)
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            p.validate_oci(self.path, ["amd64"])

    def test_external_descriptors_and_unreferenced_blobs_rejected(self):
        write_oci(self.path, oci_members(remote=True))
        with self.assertRaises(ValueError):
            p.validate_oci(self.path, ["amd64"])
        write_oci(self.path, oci_members() | {"blobs/sha256/" + "f" * 64: b"extra"})
        with self.assertRaisesRegex(ValueError, "Unreferenced"):
            p.validate_oci(self.path, ["amd64"])

    def test_assets_allowlist_and_trusted_checksums(self):
        directory = Path(self.tmp.name) / "assets"
        directory.mkdir()
        expected = p.expected_assets("1.2.3")
        self.assertEqual(len(expected), 16)  # 7 binaries + source, each with SBOM.
        for name in expected:
            (directory / name).write_bytes(
                b'{"spdxVersion": "SPDX-2.3"}' if name.endswith(".json") else b"opaque")
        p.validate_assets(directory, "1.2.3")
        checksum = (directory / "checksums.txt").read_text()
        self.assertEqual(len(checksum.splitlines()), 16)
        self.assertIn(hashlib.sha256(b"opaque").hexdigest(), checksum)
        # A supplied checksum/script is not trusted, nor silently accepted.
        with self.assertRaisesRegex(ValueError, "Unexpected/missing"):
            p.validate_assets(directory, "1.2.3")

    def test_asset_symlinks_rejected(self):
        directory = Path(self.tmp.name) / "assets"
        directory.mkdir()
        for name in p.expected_assets("1.2.3"):
            (directory / name).symlink_to("/etc/hosts")
        with self.assertRaises(ValueError):
            p.validate_assets(directory, "1.2.3")


class ReleaseNotesTests(unittest.TestCase):
    def setUp(self):
        self.candidate = {"ref": "refs/tags/v0.4.2", "sha": SHA}
        self.api = Mock()

    def notes(self, changelog, value="0.4.2"):
        self.api.call.return_value = changelog_file(changelog)
        return p.release_notes(self.api, self.candidate | {"ref": "refs/tags/v" + value})

    def test_exact_section_at_source_sha_preserves_markdown(self):
        body = "### Added\n\n- Detailed feature.\n\n### Fixed\n\n- Import fixes."
        self.assertEqual(self.notes(
            "# Changelog\n\n## [Unreleased]\n\n- Future.\n\n"
            "## [0.4.20] - 2026-01-03\n\n- Wrong patch.\n\n"
            "## [0.4.2] - 2026-01-02\n\n" + body
            + "\n\n## Migration notes\n\nNot this version.\n"), body)
        self.api.call.assert_called_once_with(p.source_path("contents/CHANGELOG.md?ref=" + SHA))
        for boundary in ("##", "## ", "## Other notes"):
            with self.subTest(boundary=boundary):
                self.assertEqual(self.notes(
                    "## [0.4.2]\n- Fix.\n" + boundary + "\nNot release notes."), "- Fix.")

    def test_version_heading_forms_and_exact_prerelease_metadata(self):
        for heading in ("## [0.4.2]", "## 0.4.2", "## 0.4.2 - 2026-01-02",
                        "  ## [0.4.2] - 2026-01-02 ##"):
            with self.subTest(heading=heading):
                self.assertEqual(self.notes(heading + "\r\n\r\n- Fix.\r\n"), "- Fix.")
        value = "0.4.2-rc.1+build.2"
        self.assertEqual(self.notes(
            "## [0.4.2]\nStable.\n## [0.4.2-rc.1+build.2]\nCandidate.\n"
            "## [0.4.2-rc.1+build.3]\nOther.", value), "Candidate.")
        for other in ("0.4.20", "0.4.2-rc.1", "0.4.2+build.2", "Unreleased", "v0.4.2"):
            with self.subTest(other=other), self.assertRaisesRegex(ValueError, "Missing"):
                self.notes(f"## [{other}]\n\n- Not this release.")

    def test_missing_empty_and_duplicate_sections_fail_closed(self):
        for text, error in (
            ("# Changelog\n", "Missing"),
            ("## [Unreleased]\n- Future.\n", "Missing"),
            ("## [0.4.2]###\n- Not an exact version heading.", "Missing"),
            ("## [0.4.2]\n \n## [0.4.1]\n- Old.", "Empty"),
            ("## [0.4.2]\n### Fixed\n\n<!-- TODO -->\n", "Empty"),
            ("## [0.4.2]\n- One.\n## [0.4.2]\n- Two.", "Duplicate"),
        ):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, error):
                self.notes(text)

    def test_fenced_headings_and_commands_remain_inert_data(self):
        body = "### Usage\n\n```sh\n## [0.4.1]\n$(do-not-run)\n```\n\n- Fix."
        self.assertEqual(self.notes(
            "~~~markdown\n## [0.4.2]\n- Example only.\n~~~\n\n"
            "## [0.4.2]\n" + body + "\n## [0.4.1]\n- Old."), body)

    def test_relative_documentation_links_resolve_at_source_sha(self):
        base = f"https://github.com/{p.SOURCE}/blob/{SHA}/"
        self.assertEqual(self.notes(
            "## [0.4.2]\n"
            '- [Guide](docs/guide.md#setup "Setup")\n'
            "- [Readme](./README.md) ![Image](/docs/image.png)\n"
            "- [Section](#details) [External](https://example.com/doc)\n"
            "- [Mail](mailto:maintainer@example.com) [CDN](//example.com/doc)\n"
            "- [Reference][guide]\n\n[guide]: <docs/guide.md> \"Guide\"\n"
            "```\n[Example](docs/example.md)\n```"),
            f'- [Guide]({base}docs/guide.md#setup "Setup")\n'
            f"- [Readme]({base}README.md) ![Image]({base}docs/image.png)\n"
            f"- [Section]({base}CHANGELOG.md#details) [External](https://example.com/doc)\n"
            "- [Mail](mailto:maintainer@example.com) [CDN](//example.com/doc)\n"
            f'- [Reference][guide]\n\n[guide]: <{base}docs/guide.md> "Guide"\n'
            "```\n[Example](docs/example.md)\n```")

    def test_missing_unreadable_or_invalid_file_has_no_fallback(self):
        error = urllib.error.HTTPError("", 404, "Not Found", {}, None)
        self.addCleanup(error.close)
        self.api.call.side_effect = error
        with self.assertRaises(urllib.error.HTTPError):
            p.release_notes(self.api, self.candidate)
        self.api.call.assert_called_once()
        self.api.call.side_effect = None
        for file in (
            {"type": "file", "encoding": "none", "content": ""},
            {"type": "dir", "encoding": "base64", "content": ""},
            {"type": "file", "encoding": "base64", "content": "%%%"},
            {"type": "file", "encoding": "base64", "content": "/w=="},
        ):
            self.api.call.return_value = file
            with self.subTest(file=file), self.assertRaises(ValueError):
                p.release_notes(self.api, self.candidate)


class PublicationTests(unittest.TestCase):
    def test_release_notes_remain_data_beneath_the_publisher_marker(self):
        candidate = p.authorize(FakeAPI("refs/tags/v1.2.3"), "refs/tags/v1.2.3")
        notes = "## Changes\n\n* Fix media imports."
        marker = "Built by the trusted [publisher](" + URL + ")."
        draft = {"id": 5, "draft": True, "author": {"login": "publisher[bot]"},
                 "target_commitish": SHA, "tag_name": "v1.2.3",
                 "body": marker + "\n\n" + notes}
        api = Mock()
        api.pages.return_value = iter([])
        api.call.side_effect = [{"id": 5}, None, draft, None]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "checksums.txt").write_text("checksum data")
            with patch.dict(os.environ, APP_SLUG="publisher", PUBLISHER_RUN_URL=URL):
                with patch("publisher.release_record", return_value=None):
                    with patch("publisher.revalidate"):
                        p.publish_release(api, candidate, directory, False, notes)
                p.require_owned_draft(draft, candidate)
        body = api.call.call_args_list[0].args[1]["body"]
        self.assertEqual(body, marker + "\n\n" + notes)
        self.assertEqual(api.call.call_args_list[-1].args[1]["body"], body)
        self.assertTrue(api.call.call_args_list[0].args[1]["draft"])
        self.assertFalse(api.call.call_args_list[-1].args[1]["draft"])

    def test_owned_draft_retry_refreshes_notes_after_upload_and_revalidation(self):
        candidate = {"ref": "refs/tags/v1.2.3", "sha": SHA}
        draft = {"id": 5, "draft": True, "author": {"login": "publisher[bot]"},
                 "target_commitish": SHA, "tag_name": "v1.2.3",
                 "body": "Built by the trusted [publisher](" + URL + ").\n\nOld generated notes."}
        api = Mock()
        api.pages.return_value = iter([])
        api.call.side_effect = [None, draft, None]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "checksums.txt").write_text("checksum data")
            with patch.dict(os.environ, APP_SLUG="publisher", PUBLISHER_RUN_URL=URL):
                with patch("publisher.release_record", return_value=draft):
                    with patch("publisher.revalidate") as revalidate:
                        api.attach_mock(revalidate, "revalidate")
                        p.publish_release(api, candidate, Path(tmp), True, "- Changelog notes.")
        self.assertEqual([call[0] for call in api.mock_calls],
                         ["pages", "call", "revalidate", "call", "call"])
        path, data = api.call.call_args.args
        self.assertEqual(path, p.source_path("releases/5"))
        self.assertEqual(api.call.call_args.kwargs, {"method": "PATCH"})
        self.assertEqual(data, {"body": draft["body"].split("\n\n")[0] + "\n\n- Changelog notes.",
                                "draft": False, "prerelease": False, "make_latest": "true"})

    def test_draft_recovery_keeps_asset_digest_and_ownership_guards(self):
        candidate = {"ref": "refs/tags/v1.2.3", "sha": SHA}
        draft = {"id": 5, "draft": True, "author": {"login": "publisher[bot]"},
                 "target_commitish": SHA, "tag_name": "v1.2.3",
                 "body": "Built by the trusted [publisher](" + URL + ")."}
        digest = "sha256:" + hashlib.sha256(b"checksum data").hexdigest()
        for existing_digest in (digest, "sha256:" + "0" * 64):
            with self.subTest(digest=existing_digest), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "checksums.txt").write_bytes(b"checksum data")
                api = Mock()
                api.pages.return_value = iter([
                    {"id": 9, "name": "checksums.txt", "digest": existing_digest}])
                api.call.side_effect = ([draft, None] if existing_digest == digest
                                        else [None, None, draft, None])
                with patch.dict(os.environ, APP_SLUG="publisher", PUBLISHER_RUN_URL=URL):
                    with patch("publisher.release_record", return_value=draft):
                        with patch("publisher.revalidate"):
                            p.publish_release(api, candidate, Path(tmp), False, "- Notes.")
                calls = api.call.call_args_list
                if existing_digest != digest:
                    self.assertEqual(calls[0].args, (p.source_path("releases/assets/9"),))
                    self.assertEqual(calls[0].kwargs, {"method": "DELETE"})
                    self.assertEqual(calls[1].kwargs, {"raw": b"checksum data"})
                self.assertEqual(calls[-2].args, (p.source_path("releases/5"),))
                self.assertFalse(calls[-1].args[1]["draft"])

        for change in ({"draft": False}, {"author": {"login": "attacker"}},
                       {"target_commitish": OTHER}, {"tag_name": "v1.2.2"},
                       {"body": "Unowned draft"}):
            with self.subTest(change=change):
                api = Mock()
                with patch.dict(os.environ, APP_SLUG="publisher"):
                    with patch("publisher.release_record", return_value=draft | change):
                        with self.assertRaisesRegex(ValueError, "immutable"):
                            p.publish(api, candidate, Path("not-even-downloaded"))
                api.call.assert_not_called()
                api.pages.assert_not_called()

    def test_concurrent_publication_blocks_notes_update_and_finalization(self):
        candidate = {"ref": "refs/tags/v1.2.3", "sha": SHA}
        draft = {"id": 5, "draft": True, "author": {"login": "publisher[bot]"},
                 "target_commitish": SHA, "tag_name": "v1.2.3",
                 "body": "Built by the trusted [publisher](" + URL + ")."}
        api = Mock()
        api.pages.return_value = iter([])
        api.call.return_value = draft | {"draft": False}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, APP_SLUG="publisher", PUBLISHER_RUN_URL=URL):
                with patch("publisher.release_record", return_value=draft):
                    with patch("publisher.revalidate"):
                        with self.assertRaisesRegex(ValueError, "immutable"):
                            p.publish_release(api, candidate, Path(tmp), False, "- Notes.")
        api.call.assert_called_once_with(p.source_path("releases/5"))

    def test_invalid_notes_block_all_publication_writes_including_draft_retry(self):
        candidate = {"ref": "refs/tags/v1.2.3", "sha": SHA}
        draft = {"id": 5, "draft": True, "author": {"login": "publisher[bot]"},
                 "target_commitish": SHA, "tag_name": "v1.2.3",
                 "body": "Built by the trusted [publisher](" + URL + ").\n\nOld notes."}
        error = urllib.error.HTTPError("", 404, "Not Found", {}, None)
        self.addCleanup(error.close)
        for existing in (None, draft):
            for content in ("## [Unreleased]\n- Future.", "## [1.2.3]\n\n", error):
                with self.subTest(existing=existing, content=content):
                    api = Mock()
                    if isinstance(content, str):
                        api.call.return_value = changelog_file(content)
                    else:
                        api.call.side_effect = content
                    with patch.dict(os.environ, APP_SLUG="publisher"):
                        with patch("publisher.release_record", return_value=existing):
                            with patch("publisher.subprocess.run") as command:
                                with self.assertRaises((ValueError, urllib.error.HTTPError)):
                                    p.publish(api, candidate, Path("not-even-downloaded"))
                    command.assert_not_called()
                    api.call.assert_called_once_with(
                        p.source_path("contents/CHANGELOG.md?ref=" + SHA))

    def test_fixed_destinations_and_latest_policy(self):
        for ref, current_latest, expected_tags, latest_release in (
            ("refs/heads/main", None, ["main-aaaaaaa-1767323045"], False),
            ("refs/heads/prelease/1.2.3", None,
             ["prelease-1.2.3-aaaaaaa-100-2", "prelease-1.2.3"], False),
            ("refs/heads/prerelease/1.2.3", None,
             ["prerelease-1.2.3-aaaaaaa-100-2", "prerelease-1.2.3"], False),
            ("refs/tags/v2.0.0", "1.2.3",
             ["2.0.0-amd64", "2.0.0-arm64", "2.0.0", "latest"], True),
            ("refs/tags/v1.2.3", "2.0.0",
             ["1.2.3-amd64", "1.2.3-arm64", "1.2.3"], False),
            ("refs/tags/v2.0.0-rc.1+build.2", "1.2.3",
             ["2.0.0-rc.1_build.2-amd64", "2.0.0-rc.1_build.2-arm64",
              "2.0.0-rc.1_build.2"], False),
        ):
            with self.subTest(ref=ref), tempfile.TemporaryDirectory() as tmp:
                api = FakeAPI(ref)
                candidate = p.authorize(api, ref)
                directory = Path(tmp)
                (directory / "image.tar").write_bytes(b"opaque")
                if ref.startswith("refs/tags/"):
                    (directory / "assets").mkdir()
                with patch.dict(os.environ, PUBLISHING_NOT_BEFORE="2026-01-01T00:00:00Z"):
                    with patch("publisher.validate_oci"), patch("publisher.validate_assets"):
                        with patch("publisher.release_record", return_value=None):
                            with patch("publisher.registry_latest", return_value=current_latest):
                                with patch("publisher.subprocess.run") as command:
                                    with patch("publisher.publish_release") as release:
                                        p.publish(api, candidate, directory)
                self.assertEqual([call.args[0][-1] for call in command.call_args_list],
                                 ["docker://" + p.IMAGE + ":" + tag for tag in expected_tags])
                if ref.startswith("refs/tags/"):
                    self.assertEqual(release.call_args.args[-2], latest_release)
                    self.assertEqual(release.call_args.args[-1], "- Fixes.")
                else:
                    release.assert_not_called()

    def test_existing_public_release_refused_before_any_registry_write(self):
        candidate = p.authorize(FakeAPI("refs/tags/v1.2.3"), "refs/tags/v1.2.3")
        with patch("publisher.release_record", return_value={"draft": False}):
            with patch("publisher.subprocess.run") as command:
                with self.assertRaisesRegex(ValueError, "immutable"):
                    p.publish(FakeAPI(), candidate, Path("not-even-downloaded"))
                command.assert_not_called()


class BuildPackagingTests(unittest.TestCase):
    def test_seven_archives_curated_embedded_source_and_sboms(self):
        """Exercise actual packaging; mock only external source/tool execution."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            output = root / "payload"
            output.mkdir()
            for name in ("LICENSE", "README.md", "CHANGELOG.md"):
                (source / name).write_text(name)
            for platform in ("linux", "macos", "windows"):
                directory = source / "packaging" / platform
                directory.mkdir(parents=True)
                (directory / "install.txt").write_text(platform)
            (source / "docs").mkdir()
            (source / "docs/guide.md").write_text("guide")
            (source / "web/dist").mkdir(parents=True)
            (source / "web/dist/index.html").write_text("embedded UI")
            candidate = p.authorize(FakeAPI("refs/tags/v1.2.3"), "refs/tags/v1.2.3")
            compilations = []

            def fake_run(*args, **kwargs):
                if args[:2] == ("go", "build"):
                    compilations.append(kwargs["env"])
                    Path(args[args.index("-o") + 1]).write_bytes(b"binary")
                elif args[0] == "fake-syft":
                    Path(args[-1].removeprefix("spdx-json=")).write_text(
                        '{"spdxVersion": "SPDX-2.3"}')
                elif args[0] == "docker":
                    write_oci(output / "image.tar", oci_members(("amd64", "arm64")))

            def fake_git(command, **kwargs):
                self.assertEqual(command[:2], ["git", "archive"])
                with tarfile.open(fileobj=kwargs["stdout"], mode="w") as archive:
                    item = tarfile.TarInfo("go.mod")
                    item.size = 6
                    archive.addfile(item, io.BytesIO(b"module"))

            with patch.multiple(build, ROOT=root, SOURCE=source, OUTPUT=output):
                with patch("build.run", side_effect=fake_run):
                    with patch("build.subprocess.run", side_effect=fake_git):
                        with patch.dict(os.environ, SYFT_CMD="fake-syft"):
                            build.release(candidate, "1.2.3")
            self.assertEqual(len(compilations), 7)
            self.assertEqual({(env["GOOS"], env["GOARCH"]) for env in compilations},
                             {("linux", "amd64"), ("linux", "arm64"), ("linux", "arm"),
                              ("darwin", "amd64"), ("darwin", "arm64"),
                              ("windows", "amd64"), ("windows", "arm64")})
            assets = output / "assets"
            self.assertEqual({path.name for path in assets.iterdir()}, p.expected_assets("1.2.3"))
            with tarfile.open(assets / "loom-source-v1.2.3.tar.gz") as archive:
                self.assertIn("loom-1.2.3/web/dist/index.html", archive.getnames())
                self.assertIn("loom-1.2.3/go.mod", archive.getnames())
            with zipfile.ZipFile(assets / "loom_1.2.3_windows_arm64.zip") as archive:
                self.assertIn("loom.exe", archive.namelist())
                self.assertIn("packaging/windows/install.txt", archive.namelist())
            p.validate_assets(assets, "1.2.3")
            p.validate_oci(output / "image.tar", ["amd64", "arm64"], "1.2.3", SHA)


class WorkflowBoundaryTests(unittest.TestCase):
    def test_pins_and_no_automatic_write(self):
        root = Path(__file__).resolve().parents[1]
        for path in (root / ".github/workflows").glob("*.yml"):
            text = path.read_text()
            for action in re.findall(r"^\s*- uses:\s*(\S+)", text, re.MULTILINE):
                self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")
            self.assertNotRegex(text, r"(?m)^\s*(?:packages|contents):\s*write")
            self.assertNotIn("persist-credentials: true", text)
            self.assertNotIn("pull_request_target", text)

    def test_build_is_unprivileged_and_all_privilege_jobs_are_environment_gated(self):
        text = (Path(__file__).resolve().parents[1] / ".github/workflows/publish.yml").read_text()
        build = text.split("\n  build:\n")[1].split("\n  publish:\n")[0]
        for forbidden in ("environment:", "GHCR_PUBLISH_TOKEN", "LOOM_APP_PRIVATE_KEY",
                          "create-github-app-token", "secrets.GITHUB_TOKEN", "cache: true"):
            self.assertNotIn(forbidden, build)
        for name in ("discover", "publish"):
            job = text.split(f"\n  {name}:\n")[1].split("\n  build:\n")[0]
            self.assertIn("environment: production-publish", job)
        self.assertIn("vars.GHCR_PUBLISH_USERNAME", text)
        self.assertNotIn("github.actor", text)
        self.assertIn("needs.build.result != 'success'", text)
        self.assertIn("retention-days: 1", text)
        self.assertNotIn("cache: true", text)
        self.assertNotRegex(text, r"runs-on: (?!ubuntu-latest)")


if __name__ == "__main__":
    unittest.main()
