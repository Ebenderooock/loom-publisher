# Loom trusted publisher

Public, free GitHub-hosted Actions publisher for
[`Ebenderooock/loom`](https://github.com/Ebenderooock/loom).
**Inactive until the owner completes every cutover step below.**
This checkout contains no credentials; bootstrap does not publish anything.

## Trust boundary

`publish.yml` polls source CI every five minutes (GitHub may delay schedules),
accepts an optional `workflow_dispatch` input `source_ref`, or is called locally
by the owner-only **Start release** workflow described below. Only the actual
publisher repository on `refs/heads/main` can proceed; reusable callers are
restricted to these two protected-main workflow files, not forks or arbitrary
workflows. It selects **one** eligible current tip per run:

- `refs/heads/main`
- `refs/heads/prelease/X.Y.Z` and `refs/heads/prerelease/X.Y.Z` (strict numeric
  semver core, no leading zeroes)
- `refs/tags/v<semver>` whose peeled commit is an ancestor of current source main.

Authorization requires the **latest** source push CI run for that ref/commit
to be completed/successful, from workflow ID **271638826**, path
`.github/workflows/ci.yml`, active workflow, source head repository. PR,
dispatch, queued, pending, skipped, failed, cancelled and failed reruns cannot
authorize publication. The verified source SHA, CI run ID/attempt, and current
ref are checked again before registry writes and release finalization. Workflow
recreation requires a reviewed change to the pinned source workflow ID.

Builds run on separate ephemeral hosted runners: exact SHA checkout, full
history, `persist-credentials: false`, no environment, no App/PAT, no registry
login, no shared caches. Source Dockerfiles, Go modules and frontend scripts
execute **only there**. Optional repository build secret `TVDB_APIKEY` is
available to that job and therefore must be considered readable by source code.

Control/publication jobs check out only the publisher's exact workflow SHA.
They mint short-lived, repository-scoped App tokens, which the pinned token
action revokes afterwards. The publisher imports validated OCI archives with
`skopeo copy`; it never runs images, extracts release archives, executes asset
scripts, or consumes source GoReleaser configuration. Validation rejects OCI
path traversal, links/devices, remote descriptors, unexpected platforms,
digest/size mismatches, excess/unreferenced blobs, and unexpected asset names.
Checksums are generated again in the trusted job. Artifacts remain **untrusted
payloads**, not evidence that the program is benign.

## Owner setup / cutover (administrative access required)

The implementing account has write, not admin. Nothing below is performed by
this bootstrap. Keep `PUBLISHING_ENABLED` unset until completion.

1. Review and integrate this publisher on its default branch **main**. Both
   repositories remain public to use free hosted Actions and public-repository
   branch/environment protection. Never put secrets in commits.
2. Protect publisher main with reviewed PRs, required **policy** and
   **oci-transport** checks from **Publisher CI**,
   CODEOWNERS review (`@Ebenderooock`), stale-approval dismissal, no force pushes
   or deletions, and no administrator/bypass exemptions. Protect **all** publisher
   code, not just workflows. GitHub does not allow an author to approve their own
   PR: a sole maintainer needs another trusted reviewer; do not silently bypass
   review. Protect source main similarly and restrict release-tag creation/
   modification. Enable source `ci.yml` for both channel spellings and `v*` tags.
3. Create publisher environment **production-publish**. Choose **selected
   deployment branches and tags**, allow **branch main only**, no tag rules.
   Do not use "all protected branches" (that could trust future branches).
   Disable administrator bypass of environment protection. Verify a non-main
   branch/tag cannot access it. Optional required reviewers are available on
   public repos but would prompt for every control/publication job: leave
   environment reviewers off for routine automatic polling, relying on reviewed,
   protected main instead. If your account/settings cannot enforce this model,
   **do not activate**.
4. On the existing production GHCR **loom** package, remove automatic Actions
   access for **both loom and loom-publisher**, including inherited repository
   access that grants Actions write. Repository default read-only workflow
   permissions alone are **not** sufficient: a branch workflow can request
   `packages: write`. Verify neither repository's `GITHUB_TOKEN` can start a blob
   upload to the production package before activation; no image/tag needs to be
   published. Stop cutover if an unapproved workflow can start an upload.
   Do not test by overwriting production tags. Leave the package public/source-linked as desired
   but decouple inherited write access; remove old publication credentials.
5. Create a classic package-publishing PAT owned by the package publishing
   identity, scoped to **write:packages** (implied read), without `repo`,
   `workflow` or `delete:packages`. Grant that identity only the package access
   needed. Classic PATs are not individually package-scoped: all packages that
   identity can write are in scope. Use a minimally privileged identity and
   expiry/rotation. Store it **only** as environment secret
   `production-publish/GHCR_PUBLISH_TOKEN`, never as a repository/org/source
   secret. Do not grant either repository automatic package write instead.
   Run **Verify publishing credential** on publisher `main` to validate the
   configured PAT's identity, package-only scopes, and actual package Write
   access while `PUBLISHING_ENABLED` is still false. This workflow checks out
   no source and requires the registry to authorize an empty upload-initiation
   request (HTTP 202); it transfers no blob data or manifest and publishes no
   image, tag, or package version. The empty probe is not finalized and its
   cleanup is left to the registry, explicitly reported as a warning. This is
   an authorization check, not an end-to-end publication test. Verify actual
   publication separately through the normal publisher workflow. The check is
   also suitable for verifying a rotated PAT.
6. Use the existing **loom-publisher** GitHub App, ID **5789004**, installed
   **only on Ebenderooock/loom**, with:
   **Contents read/write**, **Commit statuses read/write**, **Actions read**,
   **Checks read** (authoritative push before/after metadata);
   no workflows permission, no publisher installation, no webhook needed.
   Store its private key **only** as environment secret
   `production-publish/LOOM_APP_PRIVATE_KEY`.
   For owner-initiated releases, give **only this App** an **Always** bypass on
   source **release-tag creation ruleset 27398035**. This is a one-time App
   exception to creation restrictions, not a per-version exclusion. Keep tag
   patterns broad enough to protect every release (`v*`); do not exclude
   `v0.4.2` or future tags. Leave **immutable-tags ruleset 27398036** and protected
   source branches unchanged, with **no App bypass on tag immutability**.
   The App must be able to create a tag, never update/delete an existing tag.
   No App installation/permission change, owner/PAT bypass, source credential,
   or Cloudflare dispatcher credential is required.
7. Set publisher **repository variables**:

   | Variable | Value |
   |---|---|
   | `LOOM_APP_ID` | `5789004` (existing source-only App) |
   | `GHCR_PUBLISH_USERNAME` | PAT owner's actual GitHub login; never `github.actor` |
   | `PUBLISHING_NOT_BEFORE` | Deliberate CI creation-time floor, UTC `YYYY-MM-DDTHH:MM:SSZ` |
   | `PUBLISHING_ENABLED` | Set to literal `true` **last**, after the steps below |

   Set source variable **`PUBLISHER_APP_SLUG`** to the real App slug, without
   `[bot]`. Optionally set publisher repository build secret `TVDB_APIKEY`.
   No App/PAT/dispatch credential belongs in the source repository.
8. Replace source publication with read-only observers first; remove old
   `packages: write`/release write paths before enabling this publisher. Keep
   public workflow names/files (`publish-main.yml`, `publish-prerelease.yml`,
   `release.yml`) and release/download/version badge URLs on **loom**.
   Confirm current `:latest` has a valid stable semver
   `org.opencontainers.image.version` label; missing/invalid labels and
   registry auth/network errors fail closed, not as "no latest".
9. Choose the baseline intentionally, normally the cutover UTC time. CI runs
   **created before** this floor are excluded even on manual dispatch. For an
   existing current tip, the owner can explicitly choose an earlier floor after
   reviewing what it admits. Rerunning old CI does not change its creation time.
   Already public releases are **always skipped** automatically and rejected on
   explicit dispatch, irrespective of baseline or missing App status. No public
   release assets are clobbered. Then set `PUBLISHING_ENABLED=true` and monitor
   the first eligible **new** source push in each desired channel.

Unset/false activation skips scheduled work; manual requests fail clearly until
fully configured. Missing App/PAT/variables fail enabled jobs. Environment
protection must exist before secrets are added. Setting the switch alone is
not a security boundary against an unprotected publisher branch.

## Owner version button: Start release

After the reviewed workflow is integrated on publisher **main** and the one-time
creation-ruleset App bypass above is configured, the repository owner
**Ebenderooock** opens this repository's **Actions > Start release > Run workflow**,
selects **main**, and enters a version **without `v`**, for example **`0.4.2`**.
Do not push a local tag first: the workflow creates the canonical annotated
remote tag through the GitHub API. Strict semver, prerelease/build metadata, and
the publisher's existing 110-character limit apply.

Only explicit owner dispatch is accepted: publisher repository/owner numeric
IDs, actor ID, original event sender, triggering actor on reruns, workflow path,
main ref, activation switch and App ID are checked before token minting.
An Actions-write dispatcher App cannot start this workflow or rerun an owner's
run to create tags; an owner rerun cannot legitimize an original App dispatch.
The same checks run again before each new privileged job's token request,
including on failed-job reruns. This introduces **no owner restriction on the
existing non-tag-creating publisher schedule/manual interface**.

The workflow:

1. Pins the **current source main SHA**, using repository ID **1230254566**,
   then waits up to **30 minutes** for its latest authoritative **push** CI
   run/attempt (35-minute inspection job ceiling, including API overhead).
   Logs show missing/queued/pending/running CI, elapsed time and canonical CI
   links; the job summary records the pin and readiness or failure/timeout.
   A completed unsuccessful run fails immediately; a newer rerun supersedes
   older success. Source main movement or invalid CI identity fails closed,
   never silently repinning. The activation baseline and exactly one non-empty
   versioned changelog section at that SHA are also required.
2. Revalidates that exact main/CI snapshot and changelog immediately before
   creating `refs/tags/v<version>`. Only this short tagger job requests Contents
   write; inspection and CI waiting use Contents/Actions read. Each source-scoped
   App token is revoked by the pinned token action at job end; the write token
   is gone before waiting for CI. Existing publishing jobs retain their required
   release/status write permissions.
3. Waits up to **30 minutes** (35-minute job ceiling, including API overhead)
   for that exact tag/SHA's latest source **push** CI, pinned to workflow
   **271638826**, `.github/workflows/ci.yml`. PR/dispatch CI, an older success,
   pending attempts and failed reruns cannot authorize release. Logs include
   actionable CI URLs; failure/timeout preserves the tag.
4. Calls the existing `publish.yml` synchronously with the full tag ref and
   pinned SHA. Both before discovery and before publication it checks current
   main, latest green main/tag push CI, the annotated tag and tagged changelog
   again. Existing publisher ref/CI revalidation, isolated builds, asset
   validation, immutable-release handling and GHCR credentials remain intact;
   `publisher.py` policy is unchanged.
5. Reports success with release/assets, CI, container package and image links
   **only after the actual trusted publishing step completes**, not after
   dispatch/queue acceptance. Expand the nested publisher jobs for failures.

Secret-bearing jobs use only the main-only **production-publish** environment.
Control runners check out only publisher code, never source code or source build
scripts. The reusable call passes only the existing optional build key
`TVDB_APIKEY`; App and GHCR secrets resolve in the callee's protected environment.
There is no cross-workflow dispatch token, webhook, paid service, or dependency on
Cloudflare: **Cloudflare does not need to change to release Loom**.

For main-CI failure or timeout during inspection, no tag has been created by
that inspection and no new version is needed. Inspect the linked source run
(or workflow page if startup is missing), rerun source **push** CI if necessary,
then rerun Start release as owner with the same version once prerequisites pass.
The new inspection pins current main again; review it if main has advanced.
Tag creation and publication do not wait or retry writes: their existing
immediate main/CI rechecks must still pass.
For tag-CI failure or timeout, use the same CI recovery and owner rerun.
An existing annotated tag is reusable only when its direct commit target, tag
name and canonical release-start annotation match; lightweight/conflicting tags
are rejected, never forced/deleted. Annotation matching is not a cryptographic
signature: provenance relies on the sole-App creation bypass and immutable-tag
ruleset. A failed ref creation may leave an unreachable annotated Git object.

This button intentionally fails closed if source main advances before publication.
Keep main at the approved SHA until completion; if it has advanced, do not move
the tag to repair it. Investigate and use the existing explicit full-ref publisher
recovery path for the already-tagged commit after owner review, or choose a new
version. The unchanged scheduled publisher can also observe a green tag and
publish it first; transactions serialize under `production-publish`. In that
case Start release rejects the already-public release rather than claiming its
own completion or overwriting anything; inspect the source release and publisher
run. Public releases are never reopened/overwritten, even on owner reruns.

## Publication interface and outputs

Status context is **`loom-publisher/` + source short ref**, for example:

```
loom-publisher/main
loom-publisher/prelease/0.5.0
loom-publisher/prerelease/0.5.0
loom-publisher/v0.5.0
loom-publisher/v0.5.0-rc.1+build.2
```

States: **pending** before build, **success** only after publication,
**failure** for failed builds/imports/releases. Creator must be
`<PUBLISHER_APP_SLUG>[bot]`; target URL must match exactly
`https://github.com/Ebenderooock/loom-publisher/actions/runs/<numeric-id>`
(owner/repo case-insensitive, no suffix/query). Discovery checks that same
creator/URL/context before treating a status as idempotence evidence.

Images always target **ghcr.io/ebenderooock/loom**:

| Source | Tags | Platforms |
|---|---|---|
| main | `main-<7-character-sha>-<committer-unix-timestamp>` | linux/amd64 |
| prelease/X.Y.Z | `prelease-X.Y.Z`, `prelease-X.Y.Z-<sha7>-<source-CI-run-id>-<attempt>` | linux/amd64 |
| prerelease/X.Y.Z | same with distinct `prerelease-` prefix | linux/amd64 |
| vSEMVER | `SEMVER`, `SEMVER-amd64`, `SEMVER-arm64` (Docker `+` replaced with `_`) | linux/amd64 + linux/arm64 manifest and architecture aliases |

Only the highest known stable source version may advance `:latest`, and only
if it is not older than the existing registry label (equal exact versions allow
safe draft recovery). A newer source tag without green CI conservatively blocks
an older release from advancing latest. Prereleases never move latest.
Semver strings over 110 characters are explicitly rejected. Build metadata
remains unchanged in public tag, asset names and status context. `_` cannot occur
in accepted semver, so the Docker encoding is collision-free.

Release assets on **loom**, public and prerelease-classified by semver:

- `loom_<version>_{linux_{amd64,arm64,armv7},darwin_{amd64,arm64}}.tar.gz`
- `loom_<version>_windows_{amd64,arm64}.zip`
- `loom-source-v<version>.tar.gz`, containing tracked sources and built `web/dist`
- a Syft SPDX JSON SBOM per archive (`<archive>.sbom.spdx.json`)
- trusted `checksums.txt` covering all archives and SBOMs.

Binary archives retain LICENSE/README/CHANGELOG, platform packaging, and
top-level docs. Publisher-owned Python cross-compilation/package logic replaces
GoReleaser; **no GoReleaser hooks/config/skip flags are used**. It preserves
`CGO_ENABLED=0`, embed/trimpath and Version/Commit/Date ldflags and seven targets.
The release runtime follows the previous COPY-only GoReleaser image (no Rain).
Main/channel images retain the source Dockerfile, Rain and TVDB build argument.

### Release notes

For `v<semver>` tags, the trusted publisher reads **`CHANGELOG.md` from the exact
peeled source commit SHA**, through the source repository Contents API. Before
any image or release writes, it requires one non-empty level-two section for
that exact version (including any prerelease/build metadata), for example
`## [0.4.2] - 2026-11-17`. Bracketed or bare versions, with or without a date,
are supported. The heading is omitted; the Markdown body ends at the next
level-two heading, retaining nested headings and lists. Fenced examples are
not interpreted as section headings.

Missing/unreadable files, missing/empty sections, and duplicate matching
sections fail publication. There is **no Unreleased or generated-notes
fallback**. Prepare the versioned changelog before tagging; a later main-branch
edit cannot repair the changelog at an existing tag. Standard inline/image
links and reference definitions within the section resolve relative paths
against the source repository's `blob/<SHA>/` URLs (fragment-only links point
to `CHANGELOG.md` at that SHA); absolute URLs are unchanged. Keep referenced
link definitions inside the versioned section, or use absolute URLs.

The trusted publisher run-link marker remains the first paragraph. Recovery
of an App-owned matching draft validates the changelog again and replaces old
notes with this body when finalizing, after asset uploads and the existing
ownership/ref/CI checks. Source Markdown is inert data: no source scripts,
templates, or GoReleaser configuration run in publication jobs.

## Retry and limitations

- One transaction at a time; no historical matrix fan-out. Eligible releases
  precede branches. Five minutes is not a delivery SLA; GitHub can coalesce queue
  entries, delay schedules, and **auto-disable public scheduled workflows after
  60 days without repository activity**. Monitor and re-enable deliberately.
  Source observers must fail on publication failure/timeout, not interpret
  discovery or dispatch acceptance as publication success.
- All jobs use standard `ubuntu-latest` runners. There are no persistent build
  caches; intermediate artifacts expire after **one day**. No extra automatic
  Actions-write permission is requested merely to delete them earlier.
- Trusted success suppresses repeat publication for that ref/commit. Failure
  suppresses automatic retries of that same CI run/attempt; rerun CI or dispatch
  the **explicit full ref** on publisher main to retry. Empty dispatch uses normal
  discovery (including failure suppression). No SHA/image/output-tag input exists.
- Only unpublished drafts created by this App, for that exact tag/SHA and with
  this publisher's run-link marker, can resume. Differing assets can be replaced
  **only in those drafts**. Public releases and unrelated drafts are immutable.
  Do not manually publish an incomplete publisher draft. Prefer a new version
  for corrections. A failure after release publication but before status success
  needs owner investigation/reconciliation; rediscovery will not overwrite it.
- Images, GitHub releases and statuses are not an atomic transaction. A failure
  may leave version images, latest, or a draft behind; failure is reported rather
  than claiming rollback. Latest is written before the draft is finalized.
  A cancelled run or lost App authentication/runner may leave pending status;
  discovery can retry it. Failure reporting itself needs a healthy App/API.
- Source refs and CI are rechecked near writes, but GitHub offers no atomic
  compare-and-publish API. Protect source main/tags against force updates; revoke
  the PAT and disable activation during an incident. No other external writer
  may race the publisher for production tags.
- Documentation-only branch pushes retain the old exclusions (`**.md`,
  `docs/**`), using the source CI check suite's authoritative before/after
  commit comparison. New branches and comparisons reaching GitHub's 300-file
  cap conservatively publish. Explicit dispatch can publish a docs-only tip
  after successful CI, as the previous manual workflow allowed.
- Release notes use the tagged source's versioned changelog section with a trusted
  publisher run link, not GitHub-generated notes. No cosign signatures are produced
  (the previous release workflow installed cosign but did not sign outputs).
- Hosted image/tool dependencies are not fully hermetic (base images, apt,
  toolchain downloads). All third-party Actions are full-SHA pinned; Syft and
  pnpm are version-fixed. Artifact/OCI tooling must remain patched. Pagination
  is fail-closed after 10,000 entries per endpoint.
- CI exercises actual Buildx single- and multi-platform OCI exports and local
  Skopeo imports, including architecture selection, without registry access
  or credentials. Policy tests also use synthetic OCI and mocked APIs. These
  are **not** a production push, real release, protected-environment enforcement,
  full Go/frontend builds or live registry round trip. Validate those with a separate
  disposable package/repo before production activation, not production tags.

## Local checks (no production credentials)

Requires Python 3.12+ (standard `ubuntu-latest` includes it) and Go for actionlint:

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile publisher.py build.py release_start.py tests/test_*.py
go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.12 -shellcheck= .github/workflows/*.yml
```

CI runs the same policy tests, syntax compilation and workflow lint, plus
credential-free OCI transport checks on hosted Docker, without protected
environments or publishing credentials.
