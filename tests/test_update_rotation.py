import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import publisher as p
import release_start as r
import update_rotation as rotation

KEY = base64.b64encode(b"a" * 32).decode()
OLD = base64.b64encode(b"b" * 32).decode()
SHA = "a" * 40
ROOT = Path(__file__).resolve().parents[1]


class RotationTests(unittest.TestCase):
    def test_public_inputs_reject_private_keys_noncanonical_base64_and_unsafe_ids(self):
        self.assertEqual(rotation.public_key(KEY), KEY)
        for value in ("-----BEGIN PRIVATE KEY-----", "", KEY + "\n", KEY[:-1],
                      base64.b64encode(b"a" * 31).decode(), KEY[:-2] + "F="):
            with self.subTest(value=value), self.assertRaises(ValueError):
                rotation.public_key(value)
        for key_id in ("", "../bad", "Key", "a b", "a;bad", "a" * 41):
            with patch.dict(os.environ, UPDATE_PUBLIC_KEY=KEY, UPDATE_KEY_ID=key_id):
                with self.assertRaises(ValueError):
                    rotation.inputs()

    def test_addition_preserves_old_keys_and_rejects_conflicts_or_invalid_roots(self):
        raw = ('{"old":"' + OLD + '"}').encode()
        self.assertEqual(json.loads(rotation.add_key(raw, "new", KEY)), {"old": OLD, "new": KEY})
        for raw in (b"[]", b'{"old":"invalid"}',
                    ('{"old":"' + OLD + '","old":"' + OLD + '"}').encode(),
                    json.dumps({"new": OLD}).encode(), json.dumps({"old": KEY}).encode(),
                    json.dumps({f"old{i}": OLD for i in range(4)}).encode()):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                rotation.add_key(raw, "new", KEY)

    def test_current_integration_is_numeric_not_lexicographic_and_override_is_explicit(self):
        api = Mock()
        api.pages.return_value = [
            {"name": "prelease/0.4.9"}, {"name": "prelease/0.4.10"},
            {"name": "prelease/01.4.10"}, {"name": "main"}]
        self.assertEqual(rotation.integration_branch(api, ""), "prelease/0.4.10")
        self.assertEqual(rotation.integration_branch(api, "main"), "main")
        for branch in ("../main", "foo//bar", "a.lock", "main\n", "foo/", "foo."):
            with self.assertRaises(ValueError):
                rotation.integration_branch(api, branch)
        api.pages.return_value = []
        with self.assertRaisesRegex(ValueError, "No prelease"):
            rotation.integration_branch(api, "")

    def test_reminder_skips_existing_open_issue(self):
        result = subprocess.CompletedProcess([], 0, json.dumps(
            [{"title": rotation.TITLE, "url": "https://github.com/issue"}]), "")
        with patch("update_rotation.subprocess.run", return_value=result) as command:
            rotation.reminder()
        command.assert_called_once()
        with patch("update_rotation.subprocess.run", side_effect=[
            subprocess.CompletedProcess([], 0, "[]", ""),
            subprocess.CompletedProcess([], 0, '{"html_url":"https://github.com/new"}', ""),
        ]) as command:
            rotation.reminder()
        args = command.call_args.args[0]
        self.assertIn(f"repos/{p.PUBLISHER}/issues", args)
        self.assertIn("body=" + rotation.CHECKLIST, args)

    def test_installation_permission_gate_uses_short_lived_app_jwt_and_grants_nothing(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        for permissions, allowed in (
            ({"contents": "write", "pull_requests": "write"}, True),
            ({"contents": "write"}, False), ({"contents": "read", "pull_requests": "write"}, False),
        ):
            api = Mock()
            api.call.return_value = {"app_id": int(r.APP_ID), "permissions": permissions}
            with patch.dict(os.environ, LOOM_APP_ID=r.APP_ID, LOOM_APP_PRIVATE_KEY=pem):
                with patch("update_rotation.p.API", return_value=api) as constructor:
                    if allowed:
                        rotation.app_permissions()
                    else:
                        with self.assertRaisesRegex(ValueError, "no permissions were granted"):
                            rotation.app_permissions()
            api.call.assert_called_once_with(p.source_path("installation"))
            jwt = constructor.call_args.args[0]
            header, payload, signature = jwt.split(".")
            key.public_key().verify(
                base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
                (header + "." + payload).encode(), padding.PKCS1v15(), hashes.SHA256())
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            self.assertEqual(claims["iss"], int(r.APP_ID))
            self.assertEqual(claims["exp"] - claims["iat"], 360)

    def test_proposal_creates_only_additive_trust_file_on_requested_base(self):
        api = Mock()
        api.call.side_effect = [
            {"object": {"sha": SHA, "type": "commit"}},
            {"type": "file", "encoding": "base64", "content": base64.b64encode(
                json.dumps({"old": OLD}).encode()).decode()},
            {"tree": {"sha": "b" * 40}}, {"sha": "c" * 40}, {"sha": "d" * 40}, {},
            {"html_url": "https://github.com/Ebenderooock/loom/pull/1"},
        ]
        with patch.dict(os.environ, UPDATE_PUBLIC_KEY=KEY, UPDATE_KEY_ID="new-2027",
                        BASE_BRANCH="prelease/0.4.6"):
            rotation.propose(api)
        calls = api.call.call_args_list
        entry = calls[3].args[1]["tree"]
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0]["path"], "internal/updates/trusted-keys.json")
        self.assertEqual(json.loads(entry[0]["content"]), {"old": OLD, "new-2027": KEY})
        self.assertEqual(calls[4].args[1]["parents"], [SHA])
        self.assertEqual(calls[-1].args[1]["base"], "prelease/0.4.6")
        self.assertEqual(calls[-1].args[1]["head"], "update-trust/new-2027")

    def test_guards_reject_nonmain_wrong_workflow_and_nonowner_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = Path(tmp) / "event.json"
            event.write_text(json.dumps({"sender": {"id": int(r.OWNER_ID)}}))
            env = {
                "GITHUB_REPOSITORY": p.PUBLISHER, "GITHUB_REPOSITORY_ID": r.PUBLISHER_ID,
                "GITHUB_REPOSITORY_OWNER_ID": r.OWNER_ID, "GITHUB_REF": r.MAIN,
                "GITHUB_WORKFLOW_REF": p.PUBLISHER + "/.github/workflows/propose-update-key.yml@" + r.MAIN,
                "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR_ID": r.OWNER_ID,
                "GITHUB_ACTOR": "Ebenderooock", "GITHUB_TRIGGERING_ACTOR": "Ebenderooock",
                "GITHUB_EVENT_PATH": str(event),
            }
            with patch.dict(os.environ, env):
                rotation.guard("propose-update-key.yml")
            for change in ({"GITHUB_REF": "refs/heads/evil"}, {"GITHUB_ACTOR_ID": "123"},
                           {"GITHUB_WORKFLOW_REF": "wrong"}, {"GITHUB_EVENT_NAME": "schedule"}):
                with patch.dict(os.environ, env | change), self.assertRaises(ValueError):
                    rotation.guard("propose-update-key.yml")

    def test_workflow_credentials_are_guarded_and_never_accept_update_private_keys(self):
        text = (ROOT / ".github/workflows/propose-update-key.yml").read_text()
        self.assertIn("environment: production-publish", text)
        self.assertNotIn("LOOM_UPDATE_SIGNING_KEY", text)
        self.assertLess(text.index("python3 update_rotation.py permissions"),
                        text.index("actions/create-github-app-token@"))
        self.assertIn("permission-pull-requests: write", text)
        self.assertNotIn("${{ inputs.public_key }}", text.split("\njobs:", 1)[1])
        reminder = (ROOT / ".github/workflows/update-key-reminder.yml").read_text()
        self.assertIn('cron: "0 9 23 11 *"', reminder)
        self.assertIn("workflow_dispatch:", reminder)
        self.assertNotIn("secrets.", reminder)
