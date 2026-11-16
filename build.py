#!/usr/bin/env python3
"""Unprivileged build runner. All source execution stays in this job."""

import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import zipfile

from publisher import TARGETS, image_tags, ref_policy, require

ROOT = Path(__file__).resolve().parent
SOURCE = Path("source").resolve()
OUTPUT = Path("payload").resolve()


def run(*args, cwd=SOURCE, env=None):
    subprocess.run(args, cwd=cwd, env=env, check=True)


def release(candidate, value):
    # No source GoReleaser config, before/after hooks, or publication integrations.
    run("npm", "install", "--global", "pnpm@9.15.9")
    run("pnpm", "install", "--frozen-lockfile", cwd=SOURCE / "web")
    run("pnpm", "run", "build", cwd=SOURCE / "web")
    require((SOURCE / "web/dist/index.html").stat().st_size > 0, "Missing frontend")
    assets = OUTPUT / "assets"
    assets.mkdir()
    for system, arch in TARGETS:
        directory = ROOT / "work" / f"{system}-{arch}"
        directory.mkdir(parents=True)
        binary = "loom.exe" if system == "windows" else "loom"
        env = os.environ.copy()
        env.update(CGO_ENABLED="0", GOOS=system, GOARCH="arm" if arch == "armv7" else arch,
                   GOARM="7", GOMAXPROCS="2")
        ldflags = (
            "-s -w"
            f" -X github.com/ebenderooock/loom/internal/buildinfo.Version={value}"
            f" -X github.com/ebenderooock/loom/internal/buildinfo.Commit={candidate['sha']}"
            f" -X github.com/ebenderooock/loom/internal/buildinfo.Date={candidate['date']}"
        )
        run("go", "build", "-trimpath", "-tags", "embed", "-ldflags", ldflags,
            "-o", str(directory / binary), "./cmd/loom", env=env)
        for name in ("LICENSE", "README.md", "CHANGELOG.md"):
            shutil.copyfile(SOURCE / name, directory / name)
        packaging = "macos" if system == "darwin" else system
        shutil.copytree(SOURCE / "packaging" / packaging, directory / "packaging" / packaging)
        (directory / "docs").mkdir()
        for document in (SOURCE / "docs").glob("*.md"):
            shutil.copyfile(document, directory / "docs" / document.name)
        basename = f"loom_{value}_{system}_{arch}"
        if system == "windows":
            with zipfile.ZipFile(assets / (basename + ".zip"), "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(directory.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(directory))
        else:
            with tarfile.open(assets / (basename + ".tar.gz"), "w:gz") as archive:
                for path in sorted(directory.iterdir()):
                    archive.add(path, arcname=path.name)
    # git archive contains only tracked sources; append web/dist in a separate
    # unprivileged staging tree (no artifact ever gets extracted in publishing).
    staging = ROOT / "work" / "curated"
    staging.mkdir()
    raw = ROOT / "work" / "tracked.tar"
    with raw.open("wb") as stream:
        subprocess.run(["git", "archive", candidate["sha"]], cwd=SOURCE,
                       stdout=stream, check=True)
    with tarfile.open(raw) as archive:
        archive.extractall(staging, filter="data")
    shutil.copytree(SOURCE / "web/dist", staging / "web/dist", dirs_exist_ok=True)
    with tarfile.open(assets / f"loom-source-v{value}.tar.gz", "w:gz") as archive:
        archive.add(staging, arcname=f"loom-{value}")
    syft = os.environ["SYFT_CMD"]
    for archive in sorted(assets.iterdir()):
        run(syft, "scan", "file:" + str(archive), "-o",
            "spdx-json=" + str(archive) + ".sbom.spdx.json")
    # Buildx only assembles these cross-compiled binaries; no arm emulation.
    dockerfile = ROOT / "release.Dockerfile"
    context = ROOT / "work" / "images"
    context.mkdir()
    for arch in ("amd64", "arm64"):
        shutil.copyfile(ROOT / "work" / ("linux-" + arch) / "loom", context / ("loom-" + arch))
    image(candidate, value, dockerfile, context, "linux/amd64,linux/arm64")


def image(candidate, value, dockerfile, context, platforms):
    run("docker", "buildx", "build", "--platform", platforms,
        "--file", str(dockerfile), "--output", "type=oci,dest=" + str(OUTPUT / "image.tar"),
        "--provenance=false", "--sbom=false",
        "--tag", "loom-build:" + image_tags(candidate)[0],
        "--build-arg", "VERSION=" + value,
        "--build-arg", "COMMIT=" + candidate["sha"],
        "--build-arg", "DATE=" + candidate["date"],
        "--build-arg", "TVDB_APIKEY=" + os.environ.get("TVDB_APIKEY", ""),
        "--build-arg", "GOMAXPROCS=2", str(context))


def main():
    candidate = json.loads(os.environ["CANDIDATE"])
    kind, value = ref_policy(candidate["ref"])
    require(subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE,
                                    text=True).strip() == candidate["sha"], "Wrong source checkout")
    OUTPUT.mkdir()
    if kind == "release":
        release(candidate, value)
    else:
        value = (subprocess.check_output(["git", "describe", "--tags", "--always"],
                                        cwd=SOURCE, text=True).strip()
                 if kind == "main" else
                 f"{value}-pre.{candidate['ci_id']}.{candidate['ci_attempt']}")
        candidate["date"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        image(candidate, value, SOURCE / "deploy/docker/Dockerfile", SOURCE, "linux/amd64")


if __name__ == "__main__":
    main()
