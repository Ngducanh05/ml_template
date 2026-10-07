"""Dry/local automated E2E; never contacts AWS, Kubernetes or producer S3."""
import copy
import io
import json
from unittest.mock import Mock
from urllib.error import HTTPError
from control_plane.consumer_v2 import handle_message
from control_plane.state_v2 import Store, initial_state, enqueue
from control_plane.processor_v2 import Processor
from control_plane.runtime_catalog import Catalog, fingerprint_tag
from control_plane.runtime_resolver import runtime_fingerprint, ArtifactContract, normalize_requirements
from control_plane.controller_v2 import Controller
from control_plane.policy import Policy
from control_plane.reconciliation_v2 import Reconciler
from control_plane.router_v2 import forward
from test_state_v2 import Backend, event
from test_processor_v2 import IMAGE, FP, CONTRACT, client
from test_controller_v2 import Effects, OLD


def test_dry_automated_ingest_resolve_promote_restart_reconcile():
    backend = Backend(initial_state(OLD, FP)); store = Store(backend)
    sqs = Mock()
    sqs.delete_message.side_effect = lambda **kw: store.read()["identities"][0]
    handle_message(sqs, store, dict(Body=json.dumps(event()), ReceiptHandle="receipt"))
    assert sqs.delete_message.call_count == 1
    source = Mock(); source.inspect.return_value = CONTRACT
    ecr = client()
    ecr.describe_images.return_value["imageDetails"][0]["imageTags"] = [fingerprint_tag(runtime_fingerprint(CONTRACT))]
    assert Processor(Store(backend), source, Catalog(ecr)).tick() == "READY_FOR_RELEASE"
    effects = Effects(store)
    policy = Policy(1, 0, 300, .05, .1)
    for _ in range(15):
        # Recreate both store and controller each tick; all progression is durable.
        Controller(Store(backend), effects, owner="same", clock=lambda: 0, policy=policy).tick()
    assert store.read()["stable"]["model_version"] == "3"
    assert store.read()["active"] is None
    assert set(effects.weights) >= {0, 10, 25, 50, 100}
    assert handle_message(sqs, store, dict(Body=json.dumps(event()), ReceiptHandle="redelivery")) == "duplicate"
    assert enqueue(store, event(2)) == "stale"
    # Representative schema-v1 fixture adapter; not a claim about remote pointer.
    reconcile = Reconciler(store, lambda: event(), lambda pointer: pointer, clock=lambda: 0)
    assert reconcile.tick() == "duplicate"
    assert reconcile.tick() is None
    assert len(store.read()["processed"]) == 1


def test_dry_failure_restores_exact_pair_and_miss_cannot_deploy():
    backend = Backend(initial_state(OLD, FP)); store = Store(backend)
    sqs = Mock(); handle_message(sqs, store, dict(Body=json.dumps(event()), ReceiptHandle="r"))
    source = Mock(); source.inspect.return_value = CONTRACT
    catalog = Mock(); catalog.lookup.return_value = IMAGE
    Processor(store, source, catalog).tick()
    effects = Effects(store); effects.values["errors"] = .9
    c = Controller(store, effects, owner="same", clock=lambda: 0, policy=Policy(1, 0, 300, .05, .1))
    for _ in range(8): c.tick()
    assert store.read()["stable"] == OLD and effects.lanes["stable"] == OLD
    assert len(store.read()["failed"]) == 1 and not store.read()["processed"]
    assert enqueue(store, event()) == "duplicate"
    enqueue(store, event(4))
    changed = ArtifactContract("3.12.14", normalize_requirements("mlflow==3.15.0\nskops==0.16.0"))
    assert runtime_fingerprint(CONTRACT) != runtime_fingerprint(changed)
    source.inspect.return_value = changed; catalog.lookup.return_value = None
    assert Processor(store, source, catalog).tick() == "BUILD_REQUIRED"
    for _ in range(3): c.tick()
    assert store.read()["active"] is None
    assert store.read()["pending"][0]["status"] == "BUILD_REQUIRED"


def test_native_v2_bytes_and_error_content_type_preserved():
    body = b'{ "inputs": [{"name":"MonthlyIncome","data":[null]}] }'
    response = Mock(); response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
    response.status = 200; response.read.return_value = b'{"outputs":[1]}'
    response.headers = {"Content-Type": "application/json"}
    opener = Mock(return_value=response)
    assert forward("http://backend", "canary", body, "application/json", opener)[0] == 200
    request = opener.call_args.args[0]
    assert request.data == body
    assert request.full_url.endswith("/v2/models/credit-risk-canary-v2/infer")
    opener.side_effect = HTTPError("http://backend", 422, "invalid", {"Content-Type":"application/problem+json"}, io.BytesIO(b'problem'))
    assert forward("http://backend", "stable", body, "application/json", opener) == (422, b'problem', "application/problem+json")

