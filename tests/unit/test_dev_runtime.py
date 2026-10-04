"""P0 Dev build provenance and exact-image security boundaries, without a registry."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest


SPEC = importlib.util.spec_from_file_location(
    "dev_runtime", Path(__file__).resolve().parents[2] / "scripts" / "dev_runtime.py"
)
dev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dev)
REPOSITORY = "123456789012.dkr.ecr.us-east-2.amazonaws.com/model-service-runtime"
RUNTIME = REPOSITORY + "@sha256:" + "a" * 64
ARTIFACT = REPOSITORY.replace("runtime", "artifact") + "@sha256:" + "b" * 64


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    source = root / "src/model_service"
    source.mkdir(parents=True)
    (source / "main.py").write_text("app = object()\n", encoding="utf-8")
    (root / "Dockerfile.runtime").write_text("FROM scratch\nCOPY src/ /app/src/\n", encoding="utf-8")
    (root / "requirements-runtime.txt").write_text("fastapi==0.141.1\n", encoding="utf-8")
    return root


@pytest.fixture
def identity():
    return {"source_commit": "c" * 40, "source_dirty": True}


def test_snapshot_binds_untracked_python_and_excludes_unrelated_inputs(repo, tmp_path, identity):
    first = dev.snapshot(repo, tmp_path / "first", identity)
    (repo / ".env").write_text("SECRET=must-not-enter-context\n")
    (repo / "user-canary.yaml").write_text("unrelated: true\n")
    (repo / "src/model_service/.env").write_text("SECRET=must-not-enter-context\n")
    (repo / "src/model_service/private.key").write_text("not-a-real-private-key\n")
    cache = repo / "src/model_service/__pycache__"
    cache.mkdir()
    (cache / "cached.py").write_text("excluded_cache = True\n")
    second = dev.snapshot(repo, tmp_path / "second", identity)
    assert first["source_digest"] == second["source_digest"]
    paths = {p.relative_to(tmp_path / "second").as_posix()
             for p in (tmp_path / "second").rglob("*") if p.is_file()}
    assert paths == {"Dockerfile.runtime", "requirements-runtime.txt",
                     "src/model_service/main.py", "src/model_service/build-identity.json"}
    # No Git index is involved: new source must be covered before it is committed.
    (repo / "src/model_service/metrics.py").write_text("requests_total = 1\n", encoding="utf-8")
    third = dev.snapshot(repo, tmp_path / "third", identity)
    assert third["source_digest"] != second["source_digest"]
    assert "src/model_service/metrics.py" in {f["path"] for f in third["source_files"]}
    embedded = json.loads((tmp_path / "third" / dev.IDENTITY_PATH).read_text())
    assert embedded == third
    assert embedded["source_dirty"] is True
    assert embedded["production_eligible"] is False
    assert not any(f["path"].endswith("build-identity.json") for f in third["source_files"])


@pytest.mark.parametrize("path", ["Dockerfile.runtime", "requirements-runtime.txt", "src/model_service/main.py"])
def test_every_runtime_input_changes_source_digest(repo, tmp_path, identity, path):
    before = dev.snapshot(repo, tmp_path / "before", identity)
    with (repo / path).open("a", encoding="utf-8") as stream:
        stream.write("\n# changed input\n")
    after = dev.snapshot(repo, tmp_path / "after", identity)
    assert before["source_digest"] != after["source_digest"]


def test_source_identity_tracks_dirty_tree_without_storing_paths(monkeypatch, repo):
    responses = [str(repo), "c" * 40, "?? private-user-file\n M src/model_service/main.py\n"]
    runner = Mock(side_effect=[subprocess.CompletedProcess([], 0, text, "") for text in responses])
    monkeypatch.setattr(dev, "run", runner)
    assert dev.source_identity(repo) == {"source_commit": "c" * 40, "source_dirty": True}
    assert runner.call_args.args == ("git", "status", "--porcelain=v1", "--untracked-files=all")


def test_preexisting_or_linked_identity_cannot_fake_build_provenance(repo, tmp_path, identity):
    (repo / dev.IDENTITY_PATH).write_text('{"source_commit": "fake"}')
    with pytest.raises(ValueError, match="generated"):
        dev.snapshot(repo, tmp_path / "snapshot", identity)


def test_source_links_are_rejected_before_reading_outside_workspace(monkeypatch, repo, tmp_path, identity):
    original = dev.linked
    monkeypatch.setattr(dev, "linked", lambda path: path.name == "main.py" or original(path))
    with pytest.raises(ValueError, match="symlinks"):
        dev.snapshot(repo, tmp_path / "snapshot", identity)


@pytest.mark.parametrize("image", [
    REPOSITORY + ":latest", REPOSITORY + "@sha256:short", REPOSITORY + "@sha256:" + "A" * 64,
    "https://secret@example.com/repo@sha256:" + "a" * 64,
    "other-registry.example/model@sha256:" + "a" * 64,
])
def test_only_exact_credential_free_ecr_images_are_accepted(image):
    with pytest.raises(ValueError, match="exact ECR"):
        dev.exact_image(image)


@pytest.mark.parametrize("media_type", sorted(dev.MANIFEST_TYPES))
def test_single_linux_amd64_manifest_is_accepted(media_type):
    dev.validate_manifest({"schemaVersion": 2, "mediaType": media_type, "config": {}},
                          {"os": "linux", "architecture": "amd64"})


@pytest.mark.parametrize("manifest,config", [
    ({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": []},
     {"os": "linux", "architecture": "amd64"}),
    ({"schemaVersion": 2, "mediaType": "application/vnd.docker.distribution.manifest.list.v2+json"},
     {"os": "linux", "architecture": "amd64"}),
    ({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json", "config": {}},
     {"os": "linux", "architecture": "arm64"}),
    ({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json", "config": {}},
     {"os": "windows", "architecture": "amd64"}),
])
def test_index_or_wrong_platform_never_counts_as_exact_platform_manifest(manifest, config):
    with pytest.raises(ValueError):
        dev.validate_manifest(manifest, config)


@pytest.mark.parametrize("severity,ignore", [(None, None), ("", "false"), ("HIGH,", "true"),
                                           ("HIGH", None), ("HIGH", "yes"), ("ALL", "false")])
def test_scan_policy_has_no_silent_default(severity, ignore):
    with pytest.raises(ValueError):
        dev.security_policy(severity, ignore)


@pytest.mark.parametrize("version", ["0.69.0", "0.70.01", "unknown"])
def test_scanner_version_is_pinned(monkeypatch, version):
    monkeypatch.setattr(dev, "run", Mock(return_value=subprocess.CompletedProcess(
        [], 0, f"Version: {version}\n", "")))
    with pytest.raises(ValueError, match="0.70.0"):
        dev.check_scanner()


def test_scan_operational_failure_records_safe_diagnostics(monkeypatch, tmp_path):
    monkeypatch.setattr(dev, "check_scanner", lambda: None)
    monkeypatch.setattr(dev, "run", Mock(return_value=subprocess.CompletedProcess(
        [], 1, "", "secret-value: write trivy.db: There is not enough space on the disk.")))
    with pytest.raises(ValueError, match="scan gate failed"):
        dev.scan_pair(RUNTIME, ARTIFACT, dev.security_policy("HIGH,CRITICAL", "false"), tmp_path)
    raw = (tmp_path / "scans.json").read_text()
    assert "secret-value" not in raw
    evidence = json.loads(raw)
    for kind in ("runtime", "artifact"):
        assert evidence["results"][kind] == "FAIL"
        assert evidence["diagnostics"][kind]["exit_code"] == 1
        assert evidence["diagnostics"][kind]["report_exists"] is False
        assert evidence["diagnostics"][kind]["report_validation"] == "REPORT_UNAVAILABLE"
        assert evidence["diagnostics"][kind]["error"] == "Insufficient disk space"


@pytest.mark.parametrize("failed_kind,bad_evidence", [(None, False), ("runtime", False),
                                                    ("artifact", False), (None, True)])
def test_scans_bind_both_exact_images_and_fail_closed(monkeypatch, tmp_path, failed_kind, bad_evidence):
    calls = []
    monkeypatch.setenv("TRIVY_SKIP_FILES", "**")
    monkeypatch.setenv("TRIVY_IGNORE_POLICY", "some-policy")

    def fake_run(*args, **kwargs):
        calls.append(args)
        assert not any(k.upper().startswith("TRIVY_") for k in kwargs["env"])
        if "--version" in args:
            return subprocess.CompletedProcess(args, 0, "Version: 0.70.0\n", "")
        image = args[-1]
        kind = "runtime" if image == RUNTIME else "artifact"
        output = Path(args[args.index("--output") + 1])
        output.write_text(json.dumps({"ArtifactName": "other" if bad_evidence else image,
                                      "Results": [{"Target": "test OS"}]}))
        assert args[args.index("--config") + 1] == os.devnull
        assert args[args.index("--ignorefile") + 1] == os.devnull
        assert "--ignore-unfixed=false" in args
        assert args[args.index("--image-src") + 1] == "remote"
        return subprocess.CompletedProcess(args, int(kind == failed_kind), "", "")

    monkeypatch.setattr(dev, "run", fake_run)
    policy = dev.security_policy("HIGH,CRITICAL", "false")
    if failed_kind or bad_evidence:
        with pytest.raises(ValueError, match="scan gate failed"):
            dev.scan_pair(RUNTIME, ARTIFACT, policy, tmp_path)
    else:
        assert dev.scan_pair(RUNTIME, ARTIFACT, policy, tmp_path) == {"runtime": "PASS", "artifact": "PASS"}
    assert len([c for c in calls if "image" in c]) == 2
    evidence = json.loads((tmp_path / "scans.json").read_text())
    assert evidence["exact_runtime_image"] == RUNTIME
    assert evidence["exact_artifact_image"] == ARTIFACT
    if failed_kind:
        assert evidence["results"][failed_kind] == "FAIL"


def test_local_build_preserves_platform_and_source_identity_without_registry_digest(
    monkeypatch, repo, tmp_path, identity
):
    context = tmp_path / "context"
    identity = dev.snapshot(repo, context, identity)
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        if "build" in args:
            assert "--load" in args and "--push" not in args
            assert "--provenance=false" in args and "--sbom=false" in args
            assert args[args.index("--platform") + 1] == "linux/amd64"
            return subprocess.CompletedProcess(args, 0, "", "")
        config = {"Os": "linux", "Architecture": "amd64", "Id": "sha256:" + "f" * 64,
                  "Config": {"Labels": dev.labels(identity)}}
        return subprocess.CompletedProcess(args, 0, json.dumps(config), "")

    monkeypatch.setattr(dev, "run", fake_run)
    image = dev.build(context, tmp_path, identity, None, False)
    evidence = json.loads((tmp_path / "local-image.json").read_text())
    assert image.startswith("model-service-runtime:p0-dev-")
    assert "exact_runtime_image" not in evidence
    assert evidence["local_image_id"] == "sha256:" + "f" * 64


def test_snapshot_only_writes_distinct_nonproduction_evidence(monkeypatch, repo, tmp_path, identity):
    monkeypatch.setattr(dev, "source_identity", lambda root: identity)
    build = Mock()
    monkeypatch.setattr(dev, "build", build)
    directory = tmp_path / "evidence"
    assert dev.main(["--repo", str(repo), "--output-dir", str(directory), "--snapshot-only"]) == 0
    report = json.loads((directory / "dev-runtime.json").read_text())
    assert report["production_eligible"] is False
    assert report["scan_status"] == "NOT_RUN"
    assert "release_version" not in report and "workflow_run" not in report
    build.assert_not_called()
    before = deepcopy(report)
    assert dev.main(["--repo", str(repo), "--output-dir", str(directory), "--snapshot-only"]) == 1
    assert json.loads((directory / "dev-runtime.json").read_text()) == before


@pytest.mark.parametrize("digest", ["sha256:" + "d" * 64, "sha256:short", "sha256:" + "D" * 64])
def test_publication_pins_returned_digest_and_preserves_source_labels(
    monkeypatch, repo, tmp_path, identity, digest
):
    context = tmp_path / "context"
    identity = dev.snapshot(repo, context, identity)
    inspected = []

    def fake_run(*args, **kwargs):
        assert "--push" in args and "--load" not in args
        assert "--provenance=false" in args and "--sbom=false" in args
        assert args[args.index("--platform") + 1] == "linux/amd64"
        metadata = Path(args[args.index("--metadata-file") + 1])
        metadata.write_text(json.dumps({"containerimage.digest": digest}))
        return subprocess.CompletedProcess(args, 0, "", "")

    def inspect(image):
        inspected.append(image)
        return {"config": {"Labels": dev.labels(identity)}}

    monkeypatch.setattr(dev, "run", fake_run)
    monkeypatch.setattr(dev, "inspect_remote", inspect)
    if digest == "sha256:" + "d" * 64:
        assert dev.build(context, tmp_path, identity, REPOSITORY, True) == REPOSITORY + "@" + digest
        assert inspected == [REPOSITORY + "@" + digest]
    else:
        with pytest.raises(ValueError, match="valid registry digest"):
            dev.build(context, tmp_path, identity, REPOSITORY, True)
        assert inspected == []


def test_failed_scan_retains_published_pair_but_cannot_report_success(
    monkeypatch, repo, tmp_path, identity
):
    monkeypatch.setattr(dev, "source_identity", lambda root: identity)
    monkeypatch.setattr(dev, "check_scanner", lambda: None)
    monkeypatch.setattr(dev, "inspect_remote", lambda image: {})
    monkeypatch.setattr(dev, "build", lambda *args: RUNTIME)

    def fail_scan(*args):
        raise ValueError("Exact-image scan gate failed")

    monkeypatch.setattr(dev, "scan_pair", fail_scan)
    directory = tmp_path / "evidence"
    assert dev.main([
        "--repo", str(repo), "--output-dir", str(directory), "--publish",
        "--runtime-repository", REPOSITORY, "--artifact-image", ARTIFACT,
        "--blocking-severities", "HIGH,CRITICAL", "--ignore-unfixed", "false",
    ]) == 1
    report = json.loads((directory / "dev-runtime.json").read_text())
    assert report["status"] == "FAIL" and report["scan_status"] == "FAIL"
    assert report["build_status"] == "PASS"
    assert report["exact_runtime_image"] == RUNTIME and report["exact_artifact_image"] == ARTIFACT
    assert report["production_eligible"] is False


def test_missing_policy_stops_before_any_publish_or_source_read(monkeypatch, repo, tmp_path):
    source = Mock()
    builder = Mock()
    monkeypatch.setattr(dev, "source_identity", source)
    monkeypatch.setattr(dev, "build", builder)
    assert dev.main([
        "--repo", str(repo), "--output-dir", str(tmp_path / "evidence"), "--publish",
        "--runtime-repository", REPOSITORY, "--artifact-image", ARTIFACT,
    ]) == 1
    source.assert_not_called()
    builder.assert_not_called()
