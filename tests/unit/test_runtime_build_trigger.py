import copy
import json
from unittest.mock import Mock

import pytest
import yaml

from control_plane.runtime_catalog import Catalog
from control_plane.state_v2 import Store, enqueue, REPOSITORY
from control_plane.processor_v2 import Processor
from control_plane.runtime_resolver import ArtifactContract, normalize_requirements
from scripts import runtime_build_trigger as trigger
from test_state_v2 import Backend, event

IMAGE = REPOSITORY + "@sha256:" + "a" * 64
CONTRACT = ArtifactContract("3.12.14", normalize_requirements(
    "mlflow==3.14.0\nskops==0.16.0\nscikit-learn==1.7.2"))


def build_required():
    backend = Backend()
    store = Store(backend)
    enqueue(store, event())
    source = Mock()
    source.inspect.return_value = CONTRACT
    catalog = Mock()
    catalog.lookup.return_value = None
    assert Processor(store, source, catalog, clock=lambda: 0).tick() == "BUILD_REQUIRED"
    return store.read()


def run(state="completed", conclusion="failure", *, run_id=101, title=None):
    fingerprint = json.loads(trigger.canonical_request(build_required()))["fingerprint"]
    return dict(id=run_id, display_title=title or trigger.expected_title(fingerprint),
                event="workflow_dispatch", head_branch="main", status=state,
                conclusion=conclusion)


def test_catalog_hit_never_dispatches_and_processor_reconciles():
    state = build_required()
    catalog = Mock()
    catalog.lookup.return_value = IMAGE
    decision = trigger.decide(state, catalog, [])
    assert decision["outcome"] == "CATALOG_HIT"
    backend = Backend(state)
    source = Mock(); source.inspect.return_value = CONTRACT
    assert Processor(Store(backend), source, catalog, clock=lambda: 901).tick() == "READY_FOR_RELEASE"
    assert Store(backend).read()["pending"][0]["pair"]["runtime_digest"] == IMAGE


def test_build_required_dispatches_exact_contract_once():
    state = build_required(); catalog = Mock(); catalog.lookup.return_value = None
    first = trigger.decide(state, catalog, [])
    assert first["outcome"] == "DISPATCH"
    request = json.loads(first["technical_contract"])
    assert request["fingerprint"] == first["fingerprint"]
    duplicate = trigger.decide(copy.deepcopy(state), catalog,
                               [run("in_progress", None)])
    assert duplicate["outcome"] == "ACTIVE"


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required", "stale", "skipped"])
def test_failed_or_security_blocked_attempt_never_loops(conclusion):
    catalog = Mock(); catalog.lookup.return_value = None
    decision = trigger.decide(build_required(), catalog, [run(conclusion=conclusion)])
    assert decision["outcome"] == "BLOCKED_PREVIOUS_ATTEMPT"


def test_success_waits_for_catalog_without_rebuild():
    catalog = Mock(); catalog.lookup.return_value = None
    decision = trigger.decide(build_required(), catalog, [run(conclusion="success")])
    assert decision["outcome"] == "SUCCEEDED_WAITING_FOR_CATALOG"


def test_duplicate_event_and_restart_keep_one_fingerprint():
    state = build_required(); catalog = Mock(); catalog.lookup.return_value = None
    fingerprint = json.loads(trigger.canonical_request(state))["fingerprint"]
    unrelated = run(title=trigger.RUN_PREFIX + "sha256:" + "f" * 64)
    active = run("queued", None, run_id=102)
    decision = trigger.decide(copy.deepcopy(state), catalog, [unrelated, active])
    assert decision == {"outcome": "ACTIVE", "fingerprint": fingerprint, "run_id": "102"}


def test_aws_error_is_not_catalog_miss():
    catalog = Mock(); catalog.lookup.side_effect = PermissionError("denied")
    with pytest.raises(PermissionError):
        trigger.decide(build_required(), catalog, [])


@pytest.mark.parametrize("change", ["fingerprint", "pair", "authorization", "old"])
def test_invalid_or_stale_candidate_fails_closed(change):
    state = build_required()
    item = state["pending"][0]
    if change == "fingerprint": item["fingerprint"] = "sha256:" + "f" * 64
    if change == "pair": item["pair"] = dict(model_version="3", runtime_digest=IMAGE,
                                               storage_uri=item["event"]["artifact_uri"])
    if change == "authorization": item["authorization"] = {}
    if change == "old": state["highest_accepted"] += 1
    if change == "old":
        assert trigger.canonical_request(state) is None
    else:
        with pytest.raises(ValueError): trigger.canonical_request(state)


def test_workflow_uses_scoped_github_side_trigger_and_existing_builder():
    workflow = yaml.load(open(".github/workflows/runtime-build-trigger.yml"), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["observe-and-dispatch"]
    assert workflow["on"] == {"schedule": [{"cron": "7,22,37,52 * * * *"}]}
    assert job["runs-on"] == ["self-hosted", "Linux", "X64", "mlops-release-readonly"]
    assert job["permissions"] == {"contents": "read", "actions": "write", "id-token": "write"}
    text = json.dumps(job)
    assert "RELEASE_STATE_READER_B64" in text and "RELEASE_STATE_WRITER_B64" not in text
    assert "RUNTIME_BUILD_ROLE_ARN" not in text and "runtime-build.yml" not in text
    build = yaml.load(open(".github/workflows/runtime-build.yml"), Loader=yaml.BaseLoader)
    assert "fromJSON(inputs.technical_contract).fingerprint" in build["run-name"]
    assert "fromJSON(inputs.technical_contract).fingerprint" in build["concurrency"]["group"]


def test_dispatch_uses_main_existing_workflow_and_publish_gate(monkeypatch):
    request = json.dumps({"fingerprint": "sha256:" + "a" * 64})
    call = Mock(return_value=None)
    monkeypatch.setattr(trigger, "github_request", call)
    trigger.dispatch(request)
    call.assert_called_once_with("actions/workflows/runtime-build.yml/dispatches", method="POST",
                                 data={"ref": "main", "inputs": {
                                     "technical_contract": request, "publish": True}})


def test_history_bound_fails_closed(monkeypatch):
    monkeypatch.setattr(trigger, "github_request", lambda path: {"total_count": 101, "workflow_runs": []})
    with pytest.raises(ValueError, match="bounded"):
        trigger.workflow_runs()
