import io
from unittest.mock import Mock
import pytest
from control_plane.runtime_catalog import Catalog, fingerprint_tag
from control_plane.state_v2 import Store, enqueue, REPOSITORY
from control_plane.processor_v2 import Processor, S3Metadata
from control_plane.runtime_resolver import ArtifactContract, normalize_requirements
from control_plane.build_contract import build_request, validate_request
from test_state_v2 import Backend, event

IMAGE = REPOSITORY + "@sha256:" + "a" * 64
FP = "sha256:" + "b" * 64
CONTRACT = ArtifactContract("3.12.14", normalize_requirements("mlflow==3.14.0\nskops==0.16.0"))


class AWSFailure(Exception):
    def __init__(self, code): self.response = {"Error": {"Code": code}}


def client():
    c = Mock()
    c.describe_repositories.return_value = {"repositories": [{"repositoryUri": REPOSITORY, "imageTagMutability": "IMMUTABLE"}]}
    c.describe_images.return_value = {"imageDetails": [{"imageDigest": "sha256:" + "a" * 64, "imageTags": [fingerprint_tag(FP)]}]}
    return c


def test_catalog_hit_miss_and_operational_error():
    c = client(); catalog = Catalog(c)
    assert catalog.lookup(FP) == IMAGE
    c.describe_images.side_effect = AWSFailure("ImageNotFoundException")
    assert catalog.lookup(FP) is None
    c.describe_images.side_effect = AWSFailure("AccessDeniedException")
    with pytest.raises(AWSFailure): catalog.lookup(FP)


def test_invalid_response_fingerprint_mutability_and_collision():
    c = client(); catalog = Catalog(c)
    with pytest.raises(ValueError): catalog.lookup("sha256:bad")
    with pytest.raises(ValueError): catalog.publish(FP, REPOSITORY + "@sha256:" + "c" * 64)
    c.put_image.assert_not_called()
    assert catalog.publish(FP, IMAGE) == IMAGE
    c.describe_images.return_value["imageDetails"][0]["imageDigest"] = "latest"
    with pytest.raises(ValueError): catalog.lookup(FP)
    c.describe_repositories.return_value["repositories"][0]["imageTagMutability"] = "MUTABLE"
    with pytest.raises(ValueError): catalog.lookup(FP)


def test_processor_restart_and_build_required():
    b = Backend(); store = Store(b); enqueue(store, event())
    source = Mock(); source.inspect.return_value = CONTRACT
    catalog = Mock(); catalog.lookup.return_value = None
    assert Processor(store, source, catalog, clock=lambda: 0).tick() == "BUILD_REQUIRED"
    item = store.read()["pending"][0]
    assert "pair" not in item
    validate_request(item["build_request"])
    assert Processor(Store(b), source, catalog, clock=lambda: 1).tick() is None
    catalog.lookup.return_value = IMAGE
    assert Processor(Store(b), source, catalog, clock=lambda: 901).tick() == "READY_FOR_RELEASE"
    assert store.read()["pending"][0]["pair"]["storage_uri"] == event()["artifact_uri"]
    assert Processor(store, source, catalog, clock=lambda: 9999).tick() is None


def test_processor_errors_are_not_catalog_miss():
    b = Backend(); store = Store(b); enqueue(store, event())
    source = Mock(); source.inspect.side_effect = OSError()
    p = Processor(store, source, Mock(), clock=lambda: 0, max_attempts=1)
    assert p.tick() == "RESOLUTION_BLOCKED"
    assert "pair" not in store.read()["pending"][0]


def test_s3_only_fetches_bounded_metadata_objects(monkeypatch):
    monkeypatch.setattr("control_plane.processor_v2.validate_mlmodel", lambda text, contract: None)
    c = Mock(); c.get_object.side_effect = [{"Body": io.BytesIO(b"python: 3.12.14\n")},
                                          {"Body": io.BytesIO(b"mlflow==3.14.0\nskops==0.16.0")}, {"Body": io.BytesIO(b"flavors: {}")}]
    assert S3Metadata(c).inspect(event()["artifact_uri"]) == CONTRACT
    assert [x.kwargs["Key"].split("/")[-1] for x in c.get_object.call_args_list] == ["python_env.yaml", "requirements.txt", "MLmodel"]


def test_build_contract_recomputes_fingerprint():
    req = build_request(CONTRACT)
    assert validate_request(req)[0] == CONTRACT
    req["requirements"][0] = "mlflow==3.15.0"
    with pytest.raises(ValueError): validate_request(req)

def test_catalog_publish_race_is_idempotent_only_for_same_digest():
    c = client(); catalog = Catalog(c)
    missing = AWSFailure('ImageNotFoundException')
    hit = {'imageDetails': [{'imageDigest': 'sha256:' + 'a'*64, 'imageTags': [fingerprint_tag(FP)]}]}
    c.describe_images.side_effect = [missing, hit]
    c.batch_get_image.return_value = {'images':[{'imageId':{'imageDigest':'sha256:'+'a'*64}, 'imageManifest':'{}'}]}
    c.put_image.side_effect = AWSFailure('ImageTagAlreadyExistsException')
    assert catalog.publish(FP, IMAGE) == IMAGE
    c.describe_images.side_effect = [missing, {'imageDetails':[{'imageDigest':'sha256:'+'c'*64,'imageTags':[fingerprint_tag(FP)]}]}]
    with pytest.raises(ValueError, match='conflict'): catalog.publish(FP, IMAGE)


def test_s3_metadata_failure_never_reads_model_bytes():
    c = Mock(); c.get_object.side_effect = OSError('access denied')
    with pytest.raises(OSError): S3Metadata(c).inspect(event()['artifact_uri'])
    assert c.get_object.call_count == 1
