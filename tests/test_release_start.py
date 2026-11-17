import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import patch
import urllib.error

import publisher as p
import release_start as r
from test_publisher import FakeAPI, SHA, OTHER, changelog_file, run


REF = "refs/tags/v0.4.2"
TAG_SHA = "c" * 40
ROOT = Path(__file__).resolve().parents[1]


def not_found():
    return urllib.error.HTTPError("https://api.github.com/", 404, "Not Found", {}, None)


class ReleaseAPI(FakeAPI):
    def __init__(self):
        super().__init__()
        self.tag_ref = None
        self.tag_object = None
        self.changelog = changelog_file("## [0.4.2] - 2026-11-17\n\n- Release fixes.")
        self.runs.append(run(REF, id=200))
        self.repository = {"id": r.SOURCE_ID, "default_branch": "main"}
        self.calls = []
        self.ref_error = None

    def install_tag(self):
        self.tag_ref = {"ref": REF, "object": {"type": "tag", "sha": TAG_SHA}}
        self.tag_object = {"tag": "v0.4.2", "object": {"type": "commit", "sha": SHA},
                           "message": r.tag_message(REF, SHA)}

    def call(self, path, data=None, method=None, raw=None):
        self.calls.append((path, data, method))
        endpoint = path.removeprefix(p.source_path(""))
        if path == "/repos/" + p.SOURCE + "?":
            return self.repository
        if endpoint == "git/ref/heads/main":
            return {"ref": r.MAIN, "object": {"type": "commit", "sha": self.tip}}
        if endpoint == "git/ref/tags/v0.4.2":
            if self.ref_error:
                raise self.ref_error
            if not self.tag_ref:
                raise not_found()
            return self.tag_ref
        if endpoint == "git/tags/" + TAG_SHA:
            return self.tag_object
        if endpoint == "git/tags":
            self.writes.append((endpoint, data))
            self.tag_object = {"tag": data["tag"], "message": data["message"],
                               "object": {"type": data["type"], "sha": data["object"]}}
            return {"sha": TAG_SHA}
        if endpoint == "git/refs":
            self.writes.append((endpoint, data))
            self.tag_ref = {"ref": data["ref"], "object": {"type": "tag", "sha": data["sha"]}}
            return self.tag_ref
        if endpoint.startswith("actions/runs/"):
            return max((item for item in self.runs if item["id"] == int(endpoint.split("/")[-1])),
                       key=lambda item: item["run_attempt"])
        return super().call(path, data, method, raw)

    def pages(self, path, key=None):
        if "/runs?" in path:
            return iter([item for item in self.runs if item["event"] == "push"])
        return super().pages(path, key)


class ReleaseStartTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        event = Path(self.temporary.name) / "event.json"
        event.write_text(json.dumps({"sender": {"id": int(r.OWNER_ID)}}))
        self.env = {
            "GITHUB_REPOSITORY": p.PUBLISHER,
            "GITHUB_REPOSITORY_ID": r.PUBLISHER_ID,
            "GITHUB_REPOSITORY_OWNER_ID": r.OWNER_ID,
            "GITHUB_REF": r.MAIN,
            "GITHUB_WORKFLOW_REF": r.START_WORKFLOW,
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_ACTOR_ID": r.OWNER_ID,
            "GITHUB_ACTOR": "Ebenderooock",
            "GITHUB_TRIGGERING_ACTOR": "Ebenderooock",
            "GITHUB_EVENT_PATH": str(event),
            "LOOM_APP_ID": r.APP_ID,
            "PUBLISHING_ENABLED": "true",
            "PUBLISHING_NOT_BEFORE": "2026-01-01T00:00:00Z",
            "VERSION": "0.4.2",
            "GITHUB_OUTPUT": str(Path(self.temporary.name) / "output"),
        }
        environment = patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.api = ReleaseAPI()

    def test_owner_dispatch_on_publisher_main_is_required(self):
        self.assertEqual(r.start_guard(), REF)
        for change in (
            {"GITHUB_REPOSITORY": "attacker/loom-publisher"},
            {"GITHUB_REPOSITORY_ID": "42"},
            {"GITHUB_REPOSITORY_OWNER_ID": "42"},
            {"GITHUB_REF": "refs/heads/feature"},
            {"GITHUB_REF": "refs/tags/v0.4.2"},
            {"GITHUB_EVENT_NAME": "schedule"},
            {"GITHUB_EVENT_NAME": "workflow_call"},
            {"GITHUB_EVENT_NAME": "repository_dispatch"},
            {"GITHUB_ACTOR_ID": "42"},
            {"GITHUB_ACTOR": "dispatcher[bot]"},
            {"GITHUB_TRIGGERING_ACTOR": "dispatcher[bot]"},
            {"GITHUB_WORKFLOW_REF": r.PUBLISH_WORKFLOW},
            {"LOOM_APP_ID": "42"},
            {"PUBLISHING_ENABLED": "false"},
        ):
            with self.subTest(change=change), patch.dict(os.environ, change):
                with self.assertRaises(ValueError):
                    r.start_guard()

    def test_app_dispatch_cannot_be_laundered_by_owner_rerun(self):
        event = Path(os.environ["GITHUB_EVENT_PATH"])
        event.write_text(json.dumps({"sender": {"id": 42}}))
        with self.assertRaisesRegex(ValueError, "sender"):
            r.start_guard()

    def test_denied_reruns_never_construct_an_app_api_client(self):
        with patch.dict(os.environ, GITHUB_TRIGGERING_ACTOR="dispatcher[bot]"):
            for command in ("guard", "inspect", "tag", "wait"):
                with self.subTest(command=command), patch(
                        "sys.argv", ["release_start.py", command]), patch("release_start.p.API") as api:
                    with self.assertRaisesRegex(ValueError, "owner"):
                        r.main()
                    api.assert_not_called()

    def test_reusable_entry_point_rejects_untrusted_callers_and_missing_pin(self):
        for change in (
            {"GITHUB_WORKFLOW_REF": p.PUBLISHER + "/.github/workflows/evil.yml@" + r.MAIN},
            {"GITHUB_WORKFLOW_REF": r.START_WORKFLOW.replace(r.MAIN, "refs/heads/evil")},
            {"GITHUB_REPOSITORY": "attacker/loom-publisher"},
            {"EXPECTED_SOURCE_SHA": ""},
        ):
            with self.subTest(change=change), patch.dict(os.environ, change):
                with patch("sys.argv", ["release_start.py", "publisher-guard"]):
                    with self.assertRaises(ValueError), patch("release_start.p.API") as api:
                        r.main()
                    api.assert_not_called()
        with patch.dict(os.environ, EXPECTED_SOURCE_SHA=SHA):
            r.publisher_guard()

    def test_existing_schedule_and_manual_entry_points_remain_allowed(self):
        for event in ("schedule", "workflow_dispatch"):
            with patch.dict(os.environ, GITHUB_WORKFLOW_REF=r.PUBLISH_WORKFLOW,
                            GITHUB_EVENT_NAME=event, GITHUB_ACTOR_ID="42",
                            GITHUB_ACTOR="dispatcher[bot]", GITHUB_TRIGGERING_ACTOR="dispatcher[bot]"):
                r.publisher_guard()
                with patch.dict(os.environ, EXPECTED_SOURCE_SHA=SHA):
                    with self.assertRaises(ValueError):
                        r.publisher_guard()

    def test_version_input_uses_existing_strict_semver_without_shell_evaluation(self):
        for value in ("0.4.2", "1.2.3-rc.1+build.2"):
            self.assertEqual(r.release_ref(value), "refs/tags/v" + value)
        for value in ("v0.4.2", "01.2.3", "1.2.3-01", "0.4.2\n",
                      "0.4.2;touch /tmp/unsafe", "$(whoami)", "1.2.3+" + "a" * 110):
            with self.subTest(value=value), self.assertRaises(ValueError):
                r.release_ref(value)

    def test_snapshot_requires_current_main_green_push_ci_and_pinned_repository(self):
        expected = r.source_snapshot(self.api, REF)
        self.assertEqual(expected["sha"], SHA)
        for change in ({"event": "workflow_dispatch"}, {"conclusion": "failure"},
                       {"status": "in_progress"}, {"head_repository": {"full_name": "evil/loom"}}):
            api = ReleaseAPI()
            api.runs[0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                r.source_snapshot(api, REF)
        for change in ({"id": 1}, {"default_branch": "other"}):
            self.api.repository.update(change)
            with self.assertRaises(ValueError):
                r.source_snapshot(self.api, REF)

    def test_changelog_missing_empty_duplicate_or_unreadable_blocks_tag_writes(self):
        for text in ("## [Unreleased]\n- Fix.", "## [0.4.2]\n",
                     "## [0.4.2]\n- Fix.\n## [0.4.2]\n- More."):
            api = ReleaseAPI()
            api.changelog = changelog_file(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                r.create_tag(api, REF, p.authorize(api, r.MAIN))
            self.assertEqual(api.writes, [])
        self.api.changelog = {"type": "file", "encoding": "none"}
        with self.assertRaises(ValueError):
            r.source_snapshot(self.api, REF)

    def test_creates_only_annotated_tag_and_no_forced_update(self):
        snapshot = r.source_snapshot(self.api, REF)
        r.create_tag(self.api, REF, snapshot)
        self.assertEqual([path for path, _ in self.api.writes], ["git/tags", "git/refs"])
        self.assertEqual(self.api.writes[0][1], {
            "tag": "v0.4.2", "message": r.tag_message(REF, SHA), "object": SHA, "type": "commit"})
        self.assertEqual(self.api.writes[1][1], {"ref": REF, "sha": TAG_SHA})
        self.assertTrue(r.matching_tag(self.api, REF, SHA))

    def test_cli_inspection_emits_exact_snapshot_and_ref_for_reusable_pipeline(self):
        with patch("sys.argv", ["release_start.py", "inspect"]), patch(
                "release_start.p.API", return_value=self.api), patch(
                "release_start.wait_main_ci", wraps=r.wait_main_ci) as waiting:
            r.main()
        waiting.assert_called_once_with(self.api)
        outputs = dict(line.split("=", 1) for line in
                       Path(os.environ["GITHUB_OUTPUT"]).read_text().splitlines())
        self.assertEqual(outputs["source_ref"], REF)
        self.assertEqual(outputs["sha"], SHA)
        self.assertEqual(json.loads(outputs["snapshot"]), p.authorize(self.api, r.MAIN))
        self.assertEqual(self.api.writes, [])

    def wait_main(self, api=None, on_sleep=None, timeout=31):
        clock = [0.0]
        self.sleeps = []

        def sleep(seconds):
            self.sleeps.append(seconds)
            clock[0] += seconds
            if on_sleep:
                on_sleep()

        with patch("release_start.time.monotonic", side_effect=lambda: clock[0]), patch(
                "release_start.time.sleep", side_effect=sleep):
            return r.wait_main_ci(api or self.api, timeout=timeout, interval=20)

    def test_main_inspection_waits_for_pinned_ci_and_reports_progress_and_summary(self):
        summary = Path(self.temporary.name) / "summary"
        for status in ("missing", "queued", "pending", "in_progress"):
            api = ReleaseAPI()
            api.runs = [] if status == "missing" else [run(status=status, conclusion=None)]

            def complete():
                api.runs = [run()]

            with self.subTest(status=status), patch.dict(
                    os.environ, GITHUB_STEP_SUMMARY=str(summary)), patch(
                    "sys.stdout", new_callable=io.StringIO) as log:
                candidate = self.wait_main(api, complete)
                self.assertEqual(candidate, p.authorize(api, r.MAIN))
                self.assertEqual(self.sleeps, [20])
                self.assertIn(f"Main push CI: {status}", log.getvalue())
                self.assertIn("elapsed 20s/31s", log.getvalue())
                self.assertIn("/actions/runs/100", log.getvalue())
                self.assertIn(SHA, summary.read_text())
                self.assertIn("ready after 20s", summary.read_text())
                self.assertEqual(api.writes, [])

    def test_main_completed_failure_is_immediate_actionable_and_not_a_timeout(self):
        for conclusion in ("failure", "cancelled", "skipped", None):
            api = ReleaseAPI()
            api.runs[0]["conclusion"] = conclusion
            with self.subTest(conclusion=conclusion), self.assertRaisesRegex(
                    ValueError, "completed without success.*actions/runs/100.*same version"):
                self.wait_main(api)
            self.assertEqual(self.sleeps, [])
            self.assertEqual(api.writes, [])

    def test_main_missing_or_running_timeout_is_bounded_and_no_tag_is_created(self):
        for status in ("missing", "queued", "in_progress"):
            api = ReleaseAPI()
            api.runs = [] if status == "missing" else [
                run(status=status, conclusion=None)]
            link = "actions/workflows/ci.yml" if status == "missing" else "actions/runs/100"
            with self.subTest(status=status), self.assertRaisesRegex(
                    ValueError, f"Timed out.*{status}.*{link}.*No tag was created"):
                self.wait_main(api)
            self.assertEqual(self.sleeps, [20, 11])
            self.assertEqual(api.writes, [])

    def test_main_wait_rejects_movement_instead_of_repinning_to_green_ci(self):
        self.api.runs[0].update(status="in_progress", conclusion=None)

        def move():
            self.api.tip = OTHER
            self.api.runs[0].update(head_sha=OTHER, status="completed", conclusion="success")

        with self.assertRaisesRegex(ValueError, "main moved.*refusing to repin"):
            self.wait_main(on_sleep=move)
        self.assertEqual(self.api.writes, [])

    def test_main_wait_never_accepts_an_observed_attempt_regression(self):
        self.api.runs = [run(run_attempt=3, status="in_progress", conclusion=None)]

        def stale_success():
            self.api.runs = [run(run_attempt=2)]

        with self.assertRaisesRegex(ValueError, "attempt regressed.*actions/runs/100"):
            self.wait_main(on_sleep=stale_success)
        self.assertEqual(self.api.writes, [])

    def test_main_wait_rejects_untrusted_list_and_detail_even_while_running(self):
        for location in ("list", "detail"):
            for change in (
                {"workflow_id": 42}, {"path": "evil.yml"}, {"event": "workflow_dispatch"},
                {"head_sha": OTHER}, {"head_repository": {"full_name": "evil/loom"}},
                {"run_attempt": 0}, {"check_suite_id": 0}, {"id": "100"},
            ):
                api = ReleaseAPI()
                api.runs = [run(status="in_progress", conclusion=None)]
                original_call = api.call
                if location == "list":
                    # Return invalid metadata despite the API's push/SHA query filters.
                    api.pages = lambda *_: iter([api.runs[0] | change])
                else:
                    def detail(path, *args, **kwargs):
                        result = original_call(path, *args, **kwargs)
                        return result | change if "/actions/runs/" in path else result
                    api.call = detail
                with self.subTest(location=location, change=change), self.assertRaisesRegex(
                        ValueError, "identity metadata.*https://github.com/"):
                    self.wait_main(api)
                self.assertEqual(self.sleeps, [])
                self.assertEqual(api.writes, [])

    def test_main_wait_uses_latest_run_and_detail_attempt_not_stale_success(self):
        for latest in (
            run(id=101, status="in_progress", conclusion=None),
            run(id=100, run_attempt=3, status="in_progress", conclusion=None),
            run(id=101, conclusion="failure"),
            run(id=100, run_attempt=3, conclusion="failure"),
        ):
            api = ReleaseAPI()
            api.runs.append(latest)
            original = api.pages
            # A rerun may already be visible in detail before the list refreshes.
            if latest["id"] == 100:
                api.pages = lambda *args: iter(list(original(*args))[:1])
            expected = "Timed out" if latest["status"] == "in_progress" else "without success"
            with self.subTest(latest=latest), self.assertRaisesRegex(
                    ValueError, expected + ".*actions/runs/10"):
                self.wait_main(api)
            self.assertEqual(api.writes, [])

    def test_main_wait_rechecks_authorization_and_rejects_invalid_ref_identity(self):
        for change in ({"ref": "refs/heads/evil"},
                       {"object": {"type": "tag", "sha": SHA}}):
            original = self.api.call

            def detail(path, *args, **kwargs):
                result = original(path, *args, **kwargs)
                return result | change if path.endswith("git/ref/heads/main") else result

            with self.subTest(change=change), patch.object(self.api, "call", side_effect=detail):
                with self.assertRaisesRegex(ValueError, "main ref/commit identity"):
                    self.wait_main()
        newer = p.authorize(self.api, r.MAIN) | {"ci_attempt": 3}
        with patch("release_start.p.authorize", return_value=newer):
            with self.assertRaisesRegex(ValueError, "changed during readiness authorization"):
                self.wait_main()
        self.assertEqual(self.api.writes, [])

    def test_tag_and_publication_rechecks_never_wait(self):
        with patch("release_start.wait_main_ci", side_effect=AssertionError("must not wait")):
            snapshot = r.source_snapshot(self.api, REF)
            r.create_tag(self.api, REF, snapshot)
            with patch.dict(os.environ, EXPECTED_SOURCE_SHA=SHA, SOURCE_REF=REF):
                r.verify_publication_input(self.api)

    def test_matching_immutable_annotated_tag_resumes_without_writes(self):
        self.api.install_tag()
        r.create_tag(self.api, REF, r.source_snapshot(self.api, REF))
        self.assertEqual(self.api.writes, [])

    def test_conflicting_lightweight_target_name_or_annotation_never_changes_tag(self):
        for part, key, value in (
            ("ref", "ref", "refs/tags/v0.4.3"),
            ("ref", "object", {"type": "commit", "sha": SHA}),
            ("tag", "object", {"type": "commit", "sha": OTHER}),
            ("tag", "object", {"type": "tag", "sha": TAG_SHA}),
            ("tag", "tag", "v0.4.3"),
            ("tag", "message", "unrelated annotation"),
        ):
            api = ReleaseAPI()
            api.install_tag()
            (api.tag_ref if part == "ref" else api.tag_object)[key] = value
            with self.subTest(part=part, key=key), self.assertRaisesRegex(ValueError, "Conflicting"):
                r.create_tag(api, REF, r.source_snapshot(api, REF))
            self.assertEqual(api.writes, [])

    def test_non_404_tag_lookup_error_is_not_treated_as_absent(self):
        self.api.ref_error = urllib.error.HTTPError("https://api.github.com/", 403, "", {}, None)
        self.addCleanup(self.api.ref_error.close)
        with self.assertRaises(urllib.error.HTTPError):
            r.create_tag(self.api, REF, r.source_snapshot(self.api, REF))
        self.assertEqual(self.api.writes, [])

    def test_main_or_ci_movement_before_tag_creation_fails_without_writes(self):
        snapshot = r.source_snapshot(self.api, REF)
        self.api.runs[0]["run_attempt"] += 1
        with self.assertRaisesRegex(ValueError, "changed"):
            r.create_tag(self.api, REF, snapshot)
        self.assertEqual(self.api.writes, [])
        self.api = ReleaseAPI()
        snapshot = r.source_snapshot(self.api, REF)
        original = self.api.call

        def move(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == p.source_path("git/tags"):
                self.api.tip = OTHER
            return result

        self.api.call = move
        with self.assertRaises(ValueError):
            r.create_tag(self.api, REF, snapshot)
        self.assertEqual([path for path, _ in self.api.writes], ["git/tags"])
        self.assertIsNone(self.api.tag_ref)

    def test_concurrent_matching_creation_is_idempotent_but_conflict_fails(self):
        for matches in (True, False):
            api = ReleaseAPI()
            original = api.call

            def race(path, *args, **kwargs):
                if path == p.source_path("git/refs"):
                    api.install_tag()
                    if not matches:
                        api.tag_object["object"]["sha"] = OTHER
                    raise urllib.error.HTTPError("https://api.github.com/", 422, "", {}, None)
                return original(path, *args, **kwargs)

            api.call = race
            snapshot = r.source_snapshot(api, REF)
            if matches:
                r.create_tag(api, REF, snapshot)
            else:
                with self.assertRaisesRegex(ValueError, "Conflicting"):
                    r.create_tag(api, REF, snapshot)
            self.assertEqual([path for path, _ in api.writes], ["git/tags"])

    def test_ci_wait_selects_latest_push_run_not_old_success_or_dispatch(self):
        self.api.install_tag()
        self.api.runs += [run(REF, id=201, status="in_progress", conclusion=None),
                          run(REF, id=999, event="workflow_dispatch"),
                          run(REF, id=998, event="pull_request")]

        def complete(_):
            self.api.runs[-3].update(status="completed", conclusion="success")

        with patch("release_start.time.sleep", side_effect=complete) as sleep:
            url = r.wait_ci(self.api, REF, SHA, timeout=1)
        self.assertTrue(url.endswith("/201"))
        sleep.assert_called_once()

    def test_latest_failed_run_or_rerun_cannot_use_older_success(self):
        for latest in (run(REF, id=201, conclusion="failure"),
                       run(REF, id=200, run_attempt=3, conclusion="failure")):
            api = ReleaseAPI()
            api.install_tag()
            api.runs.append(latest)
            with self.subTest(latest=latest), self.assertRaisesRegex(ValueError, "actions/runs/20"):
                r.wait_ci(api, REF, SHA, timeout=0)
            self.assertEqual(api.writes, [])

    def test_ci_wait_rejects_wrong_repository_workflow_or_attempt(self):
        for change in (
            {"head_repository": {"full_name": "evil/loom"}},
            {"path": "evil.yml"}, {"workflow_id": 42}, {"head_sha": OTHER},
            {"run_attempt": 0}, {"event": "workflow_dispatch"},
        ):
            api = ReleaseAPI()
            api.install_tag()
            original = api.call

            def detail(path, *args, **kwargs):
                result = original(path, *args, **kwargs)
                return result | change if "/actions/runs/" in path else result

            api.call = detail
            with self.subTest(change=change), self.assertRaises(ValueError):
                r.wait_ci(api, REF, SHA, timeout=0)

    def test_ci_wait_timeout_links_and_preserves_tag(self):
        for runs in ([], [run(REF, id=200, status="queued", conclusion=None)]):
            api = ReleaseAPI()
            api.install_tag()
            api.runs = runs
            with self.subTest(runs=runs), self.assertRaisesRegex(
                    ValueError, "Timed out.*https://github.com/Ebenderooock/loom/actions/.*Tag preserved"):
                r.wait_ci(api, REF, SHA, timeout=0)
            self.assertIsNotNone(api.tag_ref)
            self.assertEqual(api.writes, [])

    def test_ci_wait_polling_respects_timeout_without_oversleep(self):
        self.api.install_tag()
        self.api.runs = []
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        with patch("release_start.time.monotonic", side_effect=lambda: clock[0]), patch(
                "release_start.time.sleep", side_effect=sleep) as sleeping:
            with self.assertRaisesRegex(ValueError, "Timed out"):
                r.wait_ci(self.api, REF, SHA, timeout=31, interval=20)
        self.assertEqual([call.args[0] for call in sleeping.call_args_list], [20, 11])
        self.assertEqual(clock[0], 31)
        self.assertEqual(self.api.writes, [])

    def test_ci_wait_reauthorizes_after_success_instead_of_trusting_list(self):
        self.api.install_tag()
        with patch("release_start.p.authorize", side_effect=ValueError("Latest push CI changed")):
            with self.assertRaisesRegex(ValueError, "changed"):
                r.wait_ci(self.api, REF, SHA, timeout=0)

    def test_reusable_publication_rechecks_main_ci_changelog_tag_and_pin(self):
        self.api.install_tag()
        with patch.dict(os.environ, EXPECTED_SOURCE_SHA=SHA, SOURCE_REF=REF):
            r.verify_publication_input(self.api)
            self.api.runs[0]["conclusion"] = "failure"
            with self.assertRaises(ValueError):
                r.verify_publication_input(self.api)
            self.api.runs[0]["conclusion"] = "success"
            self.api.changelog = changelog_file("## [0.4.2]\n")
            with self.assertRaises(ValueError):
                r.verify_publication_input(self.api)
        for pin in ("", OTHER, "$(whoami)"):
            with patch.dict(os.environ, EXPECTED_SOURCE_SHA=pin, SOURCE_REF=REF):
                with self.assertRaises(ValueError):
                    r.verify_publication_input(self.api)


class WorkflowPolicyTests(unittest.TestCase):
    def setUp(self):
        self.start = (ROOT / ".github/workflows/start-release.yml").read_text()
        self.publish = (ROOT / ".github/workflows/publish.yml").read_text()

    def test_only_dispatch_and_explicit_version_no_input_shell_interpolation(self):
        triggers = self.start.split("\npermissions:", 1)[0]
        self.assertIn("workflow_dispatch:", triggers)
        self.assertIn("version:", triggers)
        for forbidden in ("schedule:", "workflow_call:", "repository_dispatch:", "push:"):
            self.assertNotIn(forbidden, triggers)
        self.assertNotIn("${{ inputs.version }}", self.start.split("\njobs:", 1)[1])
        self.assertNotIn("secrets: inherit", self.start)

    def test_all_new_secret_jobs_guard_before_tokens_and_are_environment_gated(self):
        for name in ("inspect", "tag", "tag-ci"):
            job = re.split(r"\n  [a-z-]+:\n", self.start.split(f"\n  {name}:\n", 1)[1])[0]
            self.assertIn("environment: production-publish", job)
            self.assertLess(job.index("python3 release_start.py guard"),
                            job.index("actions/create-github-app-token@"))
            self.assertIn("permission-actions: read", job)
            self.assertIn("permission-contents: " + ("write" if name == "tag" else "read"), job)
            self.assertNotIn("repository: Ebenderooock/loom", job)
            for forbidden in ("permission-statuses", "permission-checks", "packages: write",
                              "skip-token-revoke", "build.py"):
                self.assertNotIn(forbidden, job)
            if name != "tag":
                self.assertNotIn("GHCR_PUBLISH_TOKEN", job)

    def test_reusable_caller_explicitly_maps_required_environment_secret_names(self):
        caller = self.start.split("\n  publisher:\n", 1)[1].split("\n  completion:\n", 1)[0]
        mappings = dict(re.findall(r"^      (\w+): \$\{\{ secrets\.(\w+) \}\}$",
                                   caller, re.MULTILINE))
        self.assertEqual(mappings, {
            "TVDB_APIKEY": "TVDB_APIKEY",
            "LOOM_APP_PRIVATE_KEY": "LOOM_APP_PRIVATE_KEY",
            "GHCR_PUBLISH_TOKEN": "GHCR_PUBLISH_TOKEN",
        })
        contract = self.publish.split("    secrets:\n", 1)[1].split("    outputs:\n", 1)[0]
        for name in ("LOOM_APP_PRIVATE_KEY", "GHCR_PUBLISH_TOKEN"):
            self.assertRegex(contract, rf"      {name}:\n        required: true")
        self.assertIn("      TVDB_APIKEY:\n        required: false", contract)
        self.assertNotIn("secrets: inherit", self.start + self.publish)

    def test_credential_readiness_blocks_tagging_and_is_rechecked_before_discovery(self):
        tag = self.start.split("\n  tag:\n", 1)[1].split("\n  tag-ci:\n", 1)[0]
        self.assertIn("    needs: inspect\n", tag)
        self.assertNotIn("    if:", tag)
        discover = self.publish.split("\n  discover:\n", 1)[1].split("\n  build:\n", 1)[0]
        self.assertLess(tag.index("Require protected environment secrets"),
                        tag.index("python3 release_start.py tag"))
        for job, guard in ((tag, "release_start.py guard"),
                           (discover, "release_start.py publisher-guard")):
            with self.subTest(guard=guard):
                marker = "      - name: Require protected environment secrets\n"
                self.assertIn("    environment: production-publish\n", job)
                self.assertIn(marker, job)
                self.assertLess(job.index(guard), job.index(marker))
                self.assertLess(job.index(marker), job.index("actions/create-github-app-token@"))
                step = job.split(marker, 1)[1].split("\n      - ", 1)[0]
                self.assertIn("APP_KEY: ${{ secrets.LOOM_APP_PRIVATE_KEY }}", step)
                self.assertIn("PACKAGE_TOKEN: ${{ secrets.GHCR_PUBLISH_TOKEN }}", step)
                script = textwrap.dedent(step.split("        run: |\n", 1)[1])
                for key, token, missing in (
                    ("", "sentinel-pat", "LOOM_APP_PRIVATE_KEY"),
                    ("sentinel-key", "", "GHCR_PUBLISH_TOKEN"),
                    ("", "", "LOOM_APP_PRIVATE_KEY"),
                    ("sentinel-key", "sentinel-pat", None),
                ):
                    result = subprocess.run(
                        ["/bin/bash", "-c", script],
                        env={"APP_KEY": key, "PACKAGE_TOKEN": token},
                        capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(result.returncode, 1 if missing else 0)
                    output = result.stdout + result.stderr
                    if missing:
                        self.assertIn(f"production-publish {missing} missing", output)
                    self.assertNotIn("sentinel-key", output)
                    self.assertNotIn("sentinel-pat", output)

    def test_main_ci_wait_has_job_headroom(self):
        inspect = self.start.split("\n  inspect:\n", 1)[1].split("\n  tag:\n", 1)[0]
        self.assertIn("timeout-minutes: 35", inspect)

    def test_reusable_pipeline_guards_and_actual_completion_not_dispatch(self):
        self.assertIn("workflow_call:", self.publish)
        self.assertIn("uses: ./.github/workflows/publish.yml", self.start)
        self.assertIn("expected_source_sha: ${{ needs.inspect.outputs.sha }}", self.start)
        for name in ("preflight", "discover", "publish"):
            job = re.split(r"\n  [a-z-]+:\n", self.publish.split(f"\n  {name}:\n", 1)[1])[0]
            self.assertIn("python3 release_start.py publisher-guard", job)
            if name != "preflight":
                self.assertLess(job.index("publisher-guard"), job.index("create-github-app-token@"))
                self.assertIn("python3 release_start.py verify", job)
        self.assertIn('echo "result=success" >> "$GITHUB_OUTPUT"', self.publish)
        self.assertLess(self.publish.index("python3 publisher.py publish"),
                        self.publish.index('echo "result=success"'))
        self.assertIn('PUBLICATION_RESULT"] != "success"', self.start)
        self.assertIn('candidate.get("sha") != os.environ["EXPECTED_SOURCE_SHA"]', self.start)
        for forbidden in ("workflow_dispatches", "gh workflow", "gh release create"):
            self.assertNotIn(forbidden, self.start + self.publish)
        self.assertIn("group: owner-release-start", self.start)
        self.assertIn("group: production-publish", self.publish)

    def test_completion_script_reports_only_matching_completed_publication(self):
        code = textwrap.dedent(
            self.start.split("python3 - <<'PY'\n", 1)[1].rsplit("\n          PY", 1)[0])
        with tempfile.TemporaryDirectory() as temporary:
            summary = Path(temporary) / "summary"
            environment = {
                "PUBLICATION_RESULT": "success",
                "CANDIDATE": json.dumps({"ref": REF, "sha": SHA}),
                "SOURCE_REF": REF, "EXPECTED_SOURCE_SHA": SHA,
                "VERSION": "0.4.2",
                "CI_URL": f"https://github.com/{p.SOURCE}/actions/runs/200",
                "GITHUB_STEP_SUMMARY": str(summary),
            }
            for change in (
                {"PUBLICATION_RESULT": "failure"}, {"PUBLICATION_RESULT": ""},
                {"PUBLICATION_RESULT": "skipped"}, {"CANDIDATE": ""},
                {"CANDIDATE": json.dumps({"ref": REF, "sha": OTHER})},
                {"CANDIDATE": json.dumps({"ref": "refs/heads/main", "sha": SHA})},
            ):
                with self.subTest(change=change), patch.dict(os.environ, environment | change):
                    with self.assertRaises(SystemExit):
                        exec(compile(code, "<completion>", "exec"), {})
                    self.assertFalse(summary.exists())
            with patch.dict(os.environ, environment):
                exec(compile(code, "<completion>", "exec"), {})
            text = summary.read_text()
            self.assertIn(f"Published Loom v0.4.2 at `{SHA}`", text)
            self.assertIn("https://github.com/Ebenderooock/loom/releases/tag/v0.4.2", text)
            self.assertIn("ghcr.io/ebenderooock/loom:0.4.2", text)


if __name__ == "__main__":
    unittest.main()
