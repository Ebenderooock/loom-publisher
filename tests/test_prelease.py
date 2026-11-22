import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote

import publisher as p


SHA = "a" * 40
OTHER = "b" * 40
URL = "https://github.com/Ebenderooock/loom-publisher/actions/runs/123"
CANDIDATE = {"ref": "refs/heads/prelease/1.2.3", "sha": SHA,
             "ci_id": 100, "ci_attempt": 2}


def release(number, sha=OTHER, ref="prelease", version="1.2.3", draft=False):
    return {"id": number, "tag_name": f"v{version}-pre.{number}",
            "target_commitish": sha, "draft": draft, "prerelease": True,
            "author": {"login": "publisher[bot]"},
            "body": f"Built by the trusted [publisher]({URL}).\n\n"
                    f"Source: refs/heads/{ref}/{version} @ {sha}"}


class MemoryAPI:
    def __init__(self):
        self.releases = []
        self.tags = {}
        self.assets = {}
        self.writes = []
        self.fail_upload = False
        self.bad_digest = False

    def pages(self, path, key=None):
        if path == p.source_path("releases"):
            return iter(list(self.releases))
        if path == p.source_path("tags"):
            return iter([{"name": name} for name in self.tags])
        if "/assets" in path:
            release_id = int(path.split("/releases/")[1].split("/")[0])
            return iter(list(self.assets.get(release_id, {}).values()))
        raise AssertionError(path)

    def call(self, path, data=None, method=None, raw=None):
        path = path.removeprefix(p.source_path(""))
        if path.startswith("git/ref/tags/"):
            return {"object": {"type": "commit", "sha": self.tags[path[13:]]}}
        self.writes.append((path, data, method, raw))
        if path == "git/refs":
            name = data["ref"].removeprefix("refs/tags/")
            if name in self.tags:
                raise ValueError("tag exists")
            self.tags[name] = data["sha"]
            return {}
        if path == "releases":
            item = data | {"id": max([r["id"] for r in self.releases] + [10]) + 1,
                           "author": {"login": "publisher[bot]"}}
            self.releases.append(item)
            return item.copy()
        if "/assets?name=" in path:
            if self.fail_upload:
                raise RuntimeError("upload failed")
            release_id = int(path.split("/")[1])
            name = unquote(path.split("?name=")[1])
            digest = "0" * 64 if self.bad_digest else hashlib.sha256(raw).hexdigest()
            self.assets.setdefault(release_id, {})[name] = {
                "id": len(self.assets.get(release_id, {})) + 1,
                "name": name, "digest": "sha256:" + digest}
            return {}
        if path.startswith("releases/assets/") and method == "DELETE":
            asset_id = int(path.split("/")[-1])
            for assets in self.assets.values():
                for name, asset in list(assets.items()):
                    if asset["id"] == asset_id:
                        del assets[name]
                        return None
            raise AssertionError(path)
        if path.startswith("git/refs/tags/") and method == "DELETE":
            name = path.removeprefix("git/refs/tags/")
            del self.tags[name]
            return None
        if path.startswith("releases/"):
            release_id = int(path.split("/")[1])
            item = next(r for r in self.releases if r["id"] == release_id)
            if method == "PATCH":
                item.update(data)
            elif method == "DELETE":
                self.releases.remove(item)
            return item.copy()
        raise AssertionError(path)


class PreleaseTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"APP_SLUG": "publisher", "PUBLISHER_RUN_URL": URL})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.validation = patch("publisher.revalidate")
        self.validation.start()
        self.addCleanup(self.validation.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        for name in ("update-manifest.json", "update-manifest.json.sig", "archive"):
            (self.directory / name).write_bytes(name.encode())

    def test_both_spellings_monotonic_draft_publication_retention_and_retry(self):
        for alias in ("prelease", "prerelease"):
            with self.subTest(alias=alias):
                api = MemoryAPI()
                api.releases = [release(i) for i in range(1, 5)] + [
                    release(7, version="2.0.0"), release(8, draft=True),
                    release(9) | {"author": {"login": "other"}}]
                api.tags = {r["tag_name"]: r["target_commitish"] for r in api.releases}
                api.tags["v1.2.3-pre.12"] = OTHER  # deleted release tag
                candidate = CANDIDATE | {"ref": f"refs/heads/{alias}/1.2.3"}
                p.publish_prelease(api, candidate, self.directory, 3)
                current = next(r for r in api.releases if r["target_commitish"] == SHA)
                self.assertEqual(current["tag_name"], "v1.2.3-pre.13")
                self.assertFalse(current["draft"])
                self.assertEqual(api.tags[current["tag_name"]], SHA)
                self.assertEqual({r["tag_name"] for r in api.releases},
                                 {"v1.2.3-pre.3", "v1.2.3-pre.4", "v1.2.3-pre.8",
                                  "v1.2.3-pre.9", "v1.2.3-pre.13", "v2.0.0-pre.7"})
                self.assertNotIn("v1.2.3-pre.1", api.tags)
                self.assertNotIn("v1.2.3-pre.2", api.tags)
                self.assertIn("v1.2.3-pre.12", api.tags)
                before = len(api.writes)
                p.publish_prelease(api, candidate, self.directory, 3)
                self.assertEqual(len(api.writes), before)

    def test_failed_upload_and_digest_leave_draft_and_resume(self):
        for failure in ("upload", "digest"):
            with self.subTest(failure=failure):
                api = MemoryAPI()
                api.fail_upload = failure == "upload"
                api.bad_digest = failure == "digest"
                with self.assertRaises((RuntimeError, ValueError)):
                    p.publish_prelease(api, CANDIDATE, self.directory, 3)
                self.assertEqual(len(api.releases), 1)
                self.assertTrue(api.releases[0]["draft"])
                api.fail_upload = api.bad_digest = False
                p.publish_prelease(api, CANDIDATE, self.directory, 3)
                self.assertEqual(len(api.releases), 1)
                self.assertFalse(api.releases[0]["draft"])

    def test_moved_tag_and_duplicate_source_fail_closed(self):
        api = MemoryAPI()
        api.releases = [release(1, SHA)]
        api.tags["v1.2.3-pre.1"] = OTHER
        with self.assertRaisesRegex(ValueError, "tag does not point"):
            p.publish_prelease(api, CANDIDATE, self.directory, 3)
        self.assertEqual(api.writes, [])
        api.releases.append(release(2, SHA))
        with self.assertRaisesRegex(ValueError, "Multiple prereleases"):
            p.publish_prelease(api, CANDIDATE, self.directory, 3)

    def test_retention_refuses_to_delete_release_with_moved_tag(self):
        api = MemoryAPI()
        api.releases = [release(i) for i in range(1, 5)]
        api.tags = {r["tag_name"]: r["target_commitish"] for r in api.releases}
        api.tags["v1.2.3-pre.4"] = SHA
        with self.assertRaisesRegex(ValueError, "tag does not point"):
            p.publish_prelease(api, CANDIDATE, self.directory, 1)
        self.assertIn("v1.2.3-pre.4", api.tags)
        self.assertTrue(any(r["tag_name"] == "v1.2.3-pre.4" for r in api.releases))

    def test_new_ci_attempt_replaces_only_recognized_old_draft_assets(self):
        api = MemoryAPI()
        api.releases = [release(1, SHA, draft=True)]
        api.tags["v1.2.3-pre.1"] = SHA
        old = "loom_1.2.3-pre.100.1_linux_amd64.tar.gz"
        api.assets[1] = {old: {"id": 5, "name": old, "digest": "sha256:" + "0" * 64}}
        p.publish_prelease(api, CANDIDATE, self.directory, 3)
        self.assertNotIn(old, api.assets[1])
        self.assertFalse(api.releases[0]["draft"])

    def test_unrecognized_draft_asset_is_not_deleted(self):
        api = MemoryAPI()
        api.releases = [release(1, SHA, draft=True)]
        api.tags["v1.2.3-pre.1"] = SHA
        name = "loom_1.2.3-pre.100.1_surprise.txt"
        api.assets[1] = {name: {"id": 5, "name": name, "digest": "sha256:" + "0" * 64}}
        with self.assertRaisesRegex(ValueError, "Unexpected existing draft assets"):
            p.publish_prelease(api, CANDIDATE, self.directory, 3)
        self.assertEqual(api.writes, [])

    def test_retention_configuration_rejects_invalid_values(self):
        for value in ("0", "-1", "3.0", "10000", " 3", "01"):
            with patch.dict(os.environ, PRELEASE_RETENTION=value):
                with self.assertRaises(ValueError):
                    p.prelease_retention()
        with patch.dict(os.environ, PRELEASE_RETENTION=""):
            self.assertEqual(p.prelease_retention(), 3)
        with patch.dict(os.environ, PRELEASE_RETENTION="5"):
            self.assertEqual(p.prelease_retention(), 5)


if __name__ == "__main__":
    unittest.main()
