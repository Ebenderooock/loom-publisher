import contextlib
import io
import json
import pathlib
import textwrap
import unittest
import urllib.error
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

    def test_authorization_is_verified_without_transferring_blob_data(self):
        requests, output = self.execute([
            self.identity(),
            Response({"token": "fake-registry-token"}),
            Response(status=202),
        ])
        self.assertEqual(
            [call.args[0].get_method() for call in requests],
            ["GET", "GET", "POST"],
        )
        self.assertEqual(requests[2].args[0].data, b"")
        self.assertEqual(requests[2].args[0].full_url,
                         "https://ghcr.io/v2/ebenderooock/loom/blobs/uploads/")
        self.assertIn("cleanup is left to the registry", output)
        self.assertIn("End-to-end publication is not checked", output)
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

    def test_unexpected_registry_response_is_not_reported_as_authorized(self):
        with self.assertRaisesRegex(SystemExit, "did not authorize"):
            self.execute([
                self.identity(),
                Response({"token": "fake-registry-token"}),
                Response(status=200),
            ])


if __name__ == "__main__":
    unittest.main()
