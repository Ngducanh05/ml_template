"""Build the repository's already trusted model bundle as one Dev artifact image."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

from dev_runtime import require, run, validate_manifest


ECR_REPOSITORY = (
    r"[0-9]{12}\.dkr\.ecr\.[a-z]{2}(?:-[a-z]+)+-[0-9]+\.amazonaws\.com/"
    r"[a-z0-9]+(?:[._/-][a-z0-9]+)*"
)
DIGEST = r"sha256:[0-9a-f]{64}"


def source_identity(repo: Path) -> dict:
    root = Path(run("git", "rev-parse", "--show-toplevel", cwd=repo).stdout.strip()).resolve()
    require(root == repo.resolve(), "Repository root mismatch")
    commit = run("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
    require(re.fullmatch(r"[0-9a-f]{40}", commit) is not None, "Invalid source commit")
    dirty = bool(run("git", "status", "--porcelain=v1", "--untracked-files=all", cwd=repo).stdout.strip())
    artifact = repo / "artifacts/model.joblib"
    require(artifact.is_file() and not artifact.is_symlink(), "Trusted model artifact is missing or linked")
    return {"source_commit": commit, "source_dirty": dirty,
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "artifact_bytes": artifact.stat().st_size,
            "artifact_model_version": "iris-demo-v1",
            "production_eligible": False}


def publish(repo: Path, output_dir: Path, repository: str, identity: dict) -> dict:
    require(re.fullmatch(ECR_REPOSITORY, repository) is not None,
            "Expected an explicit ECR repository URI without credentials/tag")
    tag = f'p0-dev-artifact-{identity["source_commit"][:12]}-{uuid.uuid4().hex[:12]}'
    image = f"{repository}:{tag}"
    metadata = output_dir / "build-metadata.json"
    artifact_sha = identity["artifact_sha256"]
    labels = {"org.opencontainers.image.revision": identity["source_commit"],
              "io.model-service.artifact-sha256": artifact_sha,
              "io.model-service.production-eligible": "false"}
    args = ["docker", "buildx", "build", "--platform", "linux/amd64",
            "--provenance=false", "--sbom=false", "--metadata-file", str(metadata),
            "--file", str(repo / "Dockerfile.artifact"), "--tag", image]
    for key, value in labels.items():
        args.extend(("--label", f"{key}={value}"))
    args.extend(("--push", str(repo)))
    run(*args, timeout=3600)
    build = json.loads(metadata.read_text(encoding="utf-8"))
    digest = build.get("containerimage.digest", "")
    require(isinstance(digest, str) and re.fullmatch(DIGEST, digest) is not None,
            "Artifact build returned no immutable digest")
    exact_image = f"{repository}@{digest}"
    manifest = json.loads(run("docker", "buildx", "imagetools", "inspect", exact_image, "--raw").stdout)
    config = json.loads(run("docker", "buildx", "imagetools", "inspect", exact_image,
                            "--format", "{{json .Image}}").stdout)
    validate_manifest(manifest, config)
    config_labels = config.get("config", {}).get("Labels") or {}
    require(all(config_labels.get(k) == v for k, v in labels.items()),
            "Published model artifact identity labels do not match source bundle")
    return {**identity, "build_timestamp": datetime.now(timezone.utc).isoformat(),
            "tag": tag, "exact_artifact_image": exact_image,
            "manifest_media_type": manifest.get("mediaType"),
            "platform": "linux/amd64", "build_status": "PASS",
            "production_eligible": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--repository", help="ECR artifact repository URI; required with --publish")
    args = parser.parse_args(argv)
    output_dir = args.output_dir.resolve()
    created = False
    report = {"status": "FAIL", "build_status": "NOT_RUN", "production_eligible": False}
    try:
        require(args.publish and args.repository, "Explicit --publish and --repository are required")
        require(not output_dir.exists(), "Use a new evidence directory")
        output_dir.mkdir(parents=True)
        created = True
        repo = args.repo.resolve()
        identity = source_identity(repo)
        report.update(identity)
        report["exact_artifact_image"] = ""
        result = publish(repo, output_dir, args.repository, identity)
        report.update(result, status="PASS")
        return 0
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        report["failure"] = str(exc) if type(exc) is ValueError else type(exc).__name__
        print(f'P0 trusted artifact build blocked: {report["failure"]}', file=sys.stderr)
        return 1
    finally:
        if created:
            (output_dir / "artifact-build.json").write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
