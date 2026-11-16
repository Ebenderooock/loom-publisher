import contextlib
import hashlib
import io
import json
import pathlib
import textwrap
import unittest
import urllib.error
import urllib.parse
from unittest import mock


WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/verify-credential.yml"
CODE = compile(
    textwrap.dedent(
        WORKFLOW.read_text().split("python3 - <<'PY'\n", 1)[1].rsplit("\n          PY", 1)[0]
    ),
    str(WORKFLOW),
    "exec",
)


class Response(io.BytesIO):
    def __init__(self, value=None, status=200, headers=None):
        super().__init__(json.dumps(value).encode())
        self.status = status
        self.headers = headers or {}


class CredentialCheckTests(unittest.TestCase):
    def execute(self, responses):
        output = io.StringIO()
        with (
            mock.patch.dict(
                "os.environ",
                {"REGISTRY_USERNAME": "publisher", "REGISTRY_PASSWORD": "fake-secret"},
                clear=True,
            ),
            mock.patch("urllib.request.urlopen", side_effect=responses) as requests,
            contextlib.redirect_stdout(output),
        ):
            exec(CODE, {"__name__": "__main__"})
        return requests.call_args_list, output.getvalue()

    def identity(self, owner="publisher", scopes="write:packages, read:packages"):
        return Response({"login": owner}, headers={"X-OAuth-Scopes": scopes})

    def test_authorized_zero_byte_probe_is_completed_without_publishing(self):
        for location, expected in [
            ("/v2/ebenderooock/loom/blobs/upload/19.test-session?state=test",
             "https://ghcr.io/v2/ebenderooock/loom/blobs/upload/19.test-session?state=test"),
            ("/v2/ebenderooock/loom/blobs/uploads/test-session",
             "https://ghcr.io/v2/ebenderooock/loom/blobs/uploads/test-session"),
            ("test-session",
             "https://ghcr.io/v2/ebenderooock/loom/blobs/uploads/test-session"),
        ]:
            with self.subTest(location=location):
                requests, output = self.execute([
                    self.identity(),
                    Response({"token": "fake-registry-token"}),
                    Response(status=202, headers={"Location": location}),
                    Response(status=201),
                ])
                self.assertEqual(
                    [call.args[0].get_method() for call in requests],
                    ["GET", "GET", "POST", "PUT"],
                )
                self.assertEqual(requests[2].args[0].data, b"")
                self.assertEqual(requests[3].args[0].data, b"")
                actual = urllib.parse.urlsplit(requests[3].args[0].full_url)
                expected = urllib.parse.urlsplit(expected)
                self.assertEqual((actual.scheme, actual.netloc, actual.path),
                                 (expected.scheme, expected.netloc, expected.path))
                query = urllib.parse.parse_qs(actual.query)
                self.assertEqual(query.pop("digest"),
                                 ["sha256:" + hashlib.sha256(b"").hexdigest()])
                self.assertEqual(query, urllib.parse.parse_qs(expected.query))
                self.assertIn("Zero-byte probe blob completed", output)
                self.assertNotIn("fake-secret", output)
                self.assertNotIn("fake-registry-token", output)

    def test_wrong_owner_and_broader_scopes_fail_before_registry_access(self):
        for owner, scopes, message in [
            ("wrong-owner", "write:packages", "PAT owner does not match"),
            ("publisher", "repo, write:packages", "not broader scopes"),
            ("publisher", "read:packages", "not broader scopes"),
        ]:
            with self.subTest(owner=owner, scopes=scopes):
                with self.assertRaisesRegex(SystemExit, message):
                    self.execute([self.identity(owner, scopes)])

    def test_registry_write_denial_is_an_explicit_failure(self):
        with self.assertRaisesRegex(SystemExit, "HTTP 403"):
            self.execute([
                self.identity(),
                Response({"token": "fake-registry-token"}),
                urllib.error.HTTPError(
                    "https://ghcr.io/v2/ebenderooock/loom/blobs/uploads/",
                    403, "Forbidden", {}, None,
                ),
            ])

    def test_completion_cannot_forward_credentials_to_another_host(self):
        with self.assertRaisesRegex(SystemExit, "unexpected upload URL"):
            self.execute([
                self.identity(),
                Response({"token": "fake-registry-token"}),
                Response(status=202, headers={
                    "Location": "https://untrusted.invalid/v2/ebenderooock/loom/blobs/uploads/session",
                }),
            ])

    def test_completion_failure_is_not_reported_as_success(self):
        with self.assertRaisesRegex(SystemExit, "did not complete"):
            self.execute([
                self.identity(),
                Response({"token": "fake-registry-token"}),
                Response(status=202, headers={
                    "Location": "/v2/ebenderooock/loom/blobs/uploads/session",
                }),
                Response(status=202),
            ])


if __name__ == "__main__":
    unittest.main()
