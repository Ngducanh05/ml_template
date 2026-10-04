import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location(
    "dev_artifact", Path(__file__).resolve().parents[2] / "scripts/dev_artifact.py"
)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
artifact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifact)


def identity():
    return {"source_commit": "a" * 40, "source_dirty": True,
            "artifact_sha256": "b" * 64, "artifact_bytes": 2150,
            "artifact_model_version": "iris-demo-v1", "production_eligible": False}


@pytest.mark.parametrize("uri", ["596432515999.dkr.ecr.us-east-2.amazonaws.com/model-service-artifact",
                                  "https://user:pass@host/repo", "example.com/repo:tag"])
def test_registry_name_accepts_only_ecr_uri_without_tag(uri):
    with pytest.raises(ValueError):
        artifact.publish(Path("C:/repo"), Path("C:/evidence"), uri, identity())


def test_publish_pins_single_manifest_with_source_and_artifact_hash_labels(tmp_path, monkeypatch):
    digest = "sha256:" + "c" * 64
    manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"digest": "sha256:" + "d" * 64}, "layers": []}
    labels = {"org.opencontainers.image.revision": "a" * 40,
              "io.model-service.artifact-sha256": "b" * 64,
              "io.model-service.production-eligible": "false"}

    def run(*args, **kwargs):
        args = list(args)
        if "build" in args:
            Path(args[args.index("--metadata-file") + 1]).write_text(
                json.dumps({"containerimage.digest": digest}))
            assert args[args.index("--platform") + 1] == "linux/amd64"
            assert "--provenance=false" in args and "--sbom=false" in args
        if "--raw" in args:
            return Mock(stdout=json.dumps(manifest))
        if "--format" in args:
            return Mock(stdout=json.dumps({"os": "linux", "architecture": "amd64",
                                          "config": {"Labels": labels}}))
        return Mock(stdout="")

    monkeypatch.setattr(artifact, "run", run)
    repository = "596432515999.dkr.ecr.us-east-2.amazonaws.com/model-service-artifact"
    result = artifact.publish(Path("C:/repo"), tmp_path, repository, identity())
    assert result["exact_artifact_image"] == repository + "@" + digest
    assert result["production_eligible"] is False
    assert result["manifest_media_type"] == manifest["mediaType"]
