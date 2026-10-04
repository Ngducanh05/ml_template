"""Build a source-identified P0 Dev runtime without creating a release candidate.

The snapshot contains only the runtime Dockerfile, runtime requirements and
Python source. Authentication is supplied externally through Docker/AWS; this
helper never logs credentials and never deploys or approves production.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid


DIGEST = r"sha256:[0-9a-f]{64}"
ECR_REPOSITORY = (
    r"[0-9]{12}\.dkr\.ecr\.[a-z]{2}(?:-[a-z]+)+-[0-9]+\.amazonaws\.com/"
    r"[a-z0-9]+(?:[._/-][a-z0-9]+)*"
)
MANIFEST_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
IDENTITY_PATH = Path("src/model_service/build-identity.json")
TRIVY_VERSION = "0.70.0"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def run(*args: str, timeout: int = 180, check: bool = True, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                            timeout=timeout, **kwargs)
    # Do not echo subprocess stderr, environment, image config or full command.
    require(not check or result.returncode == 0,
            f"{Path(args[0]).name} failed (exit {result.returncode}); no success evidence recorded")
    return result


def source_identity(repo: Path) -> dict:
    actual = Path(run("git", "rev-parse", "--show-toplevel", cwd=repo).stdout.strip()).resolve()
    require(actual == repo.resolve(), "--repo must be the existing Git working-tree root")
    commit = run("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
    require(re.fullmatch(r"[0-9a-f]{40}", commit) is not None, "Invalid source commit")
    dirty = bool(run("git", "status", "--porcelain=v1", "--untracked-files=all",
                     cwd=repo).stdout.strip())
    return {"source_commit": commit, "source_dirty": dirty}


def linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def snapshot(repo: Path, context: Path, identity: dict) -> dict:
    require(not (repo / IDENTITY_PATH).exists() and not linked(repo / IDENTITY_PATH),
            "Build identity must be generated, not supplied by repository source")
    require(not context.resolve().is_relative_to((repo / "src").resolve()),
            "Build context cannot be inside src")
    require(not linked(repo / "src"), "Source symlinks/junctions are not build inputs")
    inputs = [repo / "Dockerfile.runtime", repo / "requirements-runtime.txt"]
    for directory, directories, filenames in os.walk(repo / "src", followlinks=False):
        base = Path(directory)
        for name in directories[:]:
            path = base / name
            require(not linked(path), "Source symlinks/junctions are not build inputs")
            if name.startswith(".") or name == "__pycache__":
                directories.remove(name)
        for name in filenames:
            path = base / name
            require(not linked(path), "Source symlinks/junctions are not build inputs")
            # Current runtime is Python-only. Hidden files, keys, dotenv files,
            # caches and unrelated root files never enter the build context.
            if path.suffix == ".py" and not name.startswith("."):
                inputs.append(path)
    require(any(p.relative_to(repo).as_posix() == "src/model_service/main.py" for p in inputs),
            "Missing serving application source")
    context.mkdir(parents=True, exist_ok=False)
    manifest = []
    for path in sorted(inputs):
        require(not linked(path) and path.resolve().is_relative_to(repo.resolve()),
                "Build input escapes source root")
        content = path.read_bytes()
        relative = path.relative_to(repo)
        destination = context / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        manifest.append({"path": relative.as_posix(), "sha256": hashlib.sha256(content).hexdigest()})
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    result = {
        "schema_version": 1, "build_kind": "p0-dev-runtime", "production_eligible": False,
        **identity, "source_digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
        "source_files": manifest, "created_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(context / IDENTITY_PATH, result)
    return result


def exact_image(value: str) -> str:
    require(re.fullmatch(ECR_REPOSITORY + "@" + DIGEST, value) is not None,
            "Expected an exact ECR repository@sha256 image, without credentials or tags")
    return value


def security_policy(severities: str | None, ignore_unfixed: str | None) -> dict:
    allowed = {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
    require(isinstance(severities, str) and bool(severities)
            and set(severities.split(",")) <= allowed,
            "Explicit existing Trivy blocking severities are required")
    require(ignore_unfixed in {"true", "false"},
            "Explicit existing Trivy ignore-unfixed=true or false policy is required")
    return {"blocking_severities": severities, "ignore_unfixed": ignore_unfixed}


def validate_manifest(manifest: dict, config: dict) -> None:
    require(isinstance(manifest, dict) and manifest.get("schemaVersion") == 2
            and manifest.get("mediaType") in MANIFEST_TYPES
            and "manifests" not in manifest and isinstance(manifest.get("config"), dict),
            "Image must be a single platform manifest; indexes are not accepted")
    require(isinstance(config, dict) and config.get("os") == "linux"
            and config.get("architecture") == "amd64",
            "Image must use linux/amd64")


def inspect_remote(image: str) -> dict:
    exact_image(image)
    manifest = json.loads(run("docker", "buildx", "imagetools", "inspect", image, "--raw").stdout)
    config = json.loads(run("docker", "buildx", "imagetools", "inspect", image,
                            "--format", "{{json .Image}}").stdout)
    validate_manifest(manifest, config)
    return config


def labels(identity: dict) -> dict:
    return {
        "org.opencontainers.image.revision": identity["source_commit"],
        "io.model-service.source-digest": identity["source_digest"],
        "io.model-service.source-dirty": str(identity["source_dirty"]).lower(),
        "io.model-service.production-eligible": "false",
    }


def verify_labels(config: dict, identity: dict) -> None:
    require(isinstance(config, dict), "Missing built image configuration")
    actual = config.get("Labels") or {}
    require(isinstance(actual, dict)
            and all(actual.get(key) == value for key, value in labels(identity).items()),
            "Built image source labels do not match the snapshot")


def scanner_environment() -> dict:
    # A user's TRIVY_SKIP_*, TRIVY_IGNORE_POLICY, etc. cannot silently relax the gate.
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("TRIVY_")}


def check_scanner() -> None:
    output = run("trivy", "--version", env=scanner_environment()).stdout
    require(re.search(r"^Version: " + re.escape(TRIVY_VERSION) + r"\s*$", output, re.MULTILINE)
            is not None, f"Trivy {TRIVY_VERSION} is required")


def scan_error(stderr: str) -> str | None:
    """Classify known failures without persisting arbitrary external logs/secrets."""
    lowered = stderr.lower()
    for fragments, message in (
        (("not enough space on the disk", "no space left on device"), "Insufficient disk space"),
        (("access is denied", "permission denied"), "Filesystem access denied"),
        (("unauthorized", "authentication required", "no basic auth credentials", "expiredtoken"), "Registry authentication failed"),
        (("failed to download vulnerability db",), "Vulnerability database download failed"),
        (("certificate signed by unknown authority",), "TLS certificate verification failed"),
        (("timeout", "timed out"), "Operation timed out"),
    ):
        if any(fragment in lowered for fragment in fragments):
            return message
    return "Unclassified scanner error (raw logs withheld)" if stderr.strip() else None


def scan_pair(runtime: str, artifact: str, policy: dict, directory: Path) -> dict:
    exact_image(runtime)
    exact_image(artifact)
    security_policy(policy.get("blocking_severities"), policy.get("ignore_unfixed"))
    check_scanner()
    results = {}
    diagnostics = {}
    for kind, image in (("runtime", runtime), ("artifact", artifact)):
        output = directory / f"trivy-{kind}.json"
        require(not output.exists(), "Refusing to reuse previous scan evidence")
        # Trivy's vulnerability database can be large and its default cache lives
        # under the protected user profile on Windows. Keep it outside evidence.
        cache_dir = Path.cwd() / ".pytest_cache" / "p0-trivy-cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        command = [
            "trivy", "--cache-dir", str(cache_dir), "--config", os.devnull,
            "image", "--image-src", "remote",
            "--scanners", "vuln", "--format", "json", "--output", str(output),
            "--severity", policy["blocking_severities"],
            "--ignore-unfixed=" + policy["ignore_unfixed"], "--ignorefile", os.devnull,
            "--exit-code", "1", image,
        ]
        result = run(*command, timeout=1800, check=False, env=scanner_environment())
        parse_outcome = "PASS"
        try:
            report = json.loads(output.read_text(encoding="utf-8"))
            valid = (isinstance(report, dict) and report.get("ArtifactName") == image
                     and isinstance(report.get("Results"), list)
                     and bool(report["Results"]))
            if not valid:
                parse_outcome = "INVALID_REPORT_IDENTITY_OR_RESULTS"
        except OSError:
            parse_outcome = "REPORT_UNAVAILABLE"
            valid = False
        except ValueError:
            parse_outcome = "INVALID_JSON"
            valid = False
        results[kind] = "PASS" if result.returncode == 0 and valid else "FAIL"
        diagnostics[kind] = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "exit_code": result.returncode, "report_exists": output.is_file(),
            "report_validation": parse_outcome,
            "error": scan_error(result.stderr) if result.returncode and not valid else None,
        }
    write_json(directory / "scans.json", {
        "trivy_version": TRIVY_VERSION, "security_policy": policy, "results": results,
        "exact_runtime_image": runtime, "exact_artifact_image": artifact,
        "diagnostics": diagnostics,
    })
    require(all(value == "PASS" for value in results.values()),
            "Exact-image scan gate failed; images are not validated for deployment")
    return results


def build(context: Path, directory: Path, identity: dict, repository: str | None, publish: bool) -> str:
    tag = f'p0-dev-{identity["source_commit"][:12]}-{uuid.uuid4().hex[:12]}'
    image = f'{repository if publish else "model-service-runtime"}:{tag}'
    metadata = directory / "build-metadata.json"
    args = ["docker", "buildx", "build", "--platform", "linux/amd64",
            "--provenance=false", "--sbom=false", "--metadata-file", str(metadata),
            "--file", str(context / "Dockerfile.runtime"), "--tag", image]
    for key, value in labels(identity).items():
        args.extend(("--label", f"{key}={value}"))
    args.extend(("--push" if publish else "--load", str(context)))
    run(*args, timeout=3600)
    if publish:
        digest = json.loads(metadata.read_text(encoding="utf-8")).get("containerimage.digest", "")
        require(isinstance(digest, str) and re.fullmatch(DIGEST, digest) is not None,
                "Build did not return a valid registry digest")
        image = exact_image(f"{repository}@{digest}")
        config = inspect_remote(image)
        verify_labels(config.get("config", {}), identity)
    else:
        config = json.loads(run("docker", "image", "inspect", image,
                                "--format", "{{json .}}").stdout)
        require(config.get("Os") == "linux" and config.get("Architecture") == "amd64",
                "Local image must use linux/amd64")
        verify_labels(config.get("Config", {}), identity)
        # A local image ID is a config digest, never registry-manifest evidence.
        write_json(directory / "local-image.json", {"image": image, "local_image_id": config["Id"]})
    return image


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True, help="New evidence directory")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--snapshot-only", action="store_true")
    mode.add_argument("--publish", action="store_true", help="Explicitly publish to the given ECR repository")
    parser.add_argument("--runtime-repository", help="ECR repository URI without tag or digest")
    parser.add_argument("--artifact-image", help="Existing trusted ECR artifact repository@sha256 digest")
    parser.add_argument("--blocking-severities")
    parser.add_argument("--ignore-unfixed", choices=("true", "false"))
    args = parser.parse_args(argv)
    directory = args.output_dir.resolve()
    output_owned = False
    report = {"production_eligible": False, "status": "FAIL", "scan_status": "NOT_RUN"}
    try:
        policy = None
        if args.publish:
            require(isinstance(args.runtime_repository, str)
                    and re.fullmatch(ECR_REPOSITORY, args.runtime_repository) is not None,
                    "--publish requires an explicit ECR runtime repository")
            exact_image(args.artifact_image or "")
            policy = security_policy(args.blocking_severities, args.ignore_unfixed)
        require(not directory.exists(), "Use a new output directory; prior evidence is immutable")
        identity = source_identity(args.repo.resolve())
        directory.mkdir(parents=True)
        output_owned = True
        identity = snapshot(args.repo.resolve(), directory / "context", identity)
        report.update(identity)
        write_json(directory / "source.json", identity)
        if not args.snapshot_only:
            if args.publish:
                check_scanner()
                inspect_remote(args.artifact_image)
                report.update(exact_artifact_image=args.artifact_image, security_policy=policy)
            image = build(directory / "context", directory, identity,
                          args.runtime_repository, args.publish)
            report["exact_runtime_image" if args.publish else "local_runtime_image"] = image
            report["build_status"] = "PASS"
            if args.publish:
                report["scan_status"] = "FAIL"
                scan_pair(image, args.artifact_image, policy, directory)
                report["scan_status"] = "PASS"
        report["status"] = "PASS"
        return 0
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        # Only require() emits plain ValueError with a controlled, non-sensitive
        # message. Never serialize external exception strings or response bodies.
        report["failure"] = str(exc) if type(exc) is ValueError else type(exc).__name__
        print(f'P0 Dev runtime blocked: {report["failure"]}. No production eligibility.', file=sys.stderr)
        return 1
    finally:
        if output_owned:
            write_json(directory / "dev-runtime.json", report)


if __name__ == "__main__":
    raise SystemExit(main())
