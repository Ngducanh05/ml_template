import copy
from unittest.mock import Mock
import pytest
from control_plane.operator_v2 import recover_resolution, main
from control_plane.state_v2 import Store, enqueue, Busy, STATE_NAME
from control_plane.kube_v2 import KubeV2
from test_state_v2 import Backend, event


def blocked():
    backend = Backend()
    store = Store(backend)
    enqueue(store, event(3))
    enqueue(store, event(4))
    backend.state["pending"][0].update(status="RESOLUTION_BLOCKED", attempts=5, next_attempt=900, error_type="PermissionError")
    return backend, store


def recover(store, version="3"):
    return recover_resolution(store, version, operator="operator-test", note="Permission repaired", clock=lambda: 100)


def test_recovery_exact_version_audit_and_isolation():
    backend, store = blocked()
    before = store.read()
    assert recover(store) == {"model_version": "3", "status": "RESOLUTION_RETRY"}
    after = store.read()
    item = after["pending"][0]
    assert item["attempts"] == 0 and item["next_attempt"] == 100 and "error_type" not in item
    assert item["event"] == before["pending"][0]["event"]
    assert after["pending"][1] == before["pending"][1]
    for key in ("stable", "stable_fingerprint", "active", "route", "identities", "highest_accepted", "failed", "processed"):
        assert after[key] == before[key]
    assert after["audit"][-1] == dict(time=100, reason="operator_resolution_retry", model_version="3",
        operator="operator-test", note="Permission repaired", previous_status="RESOLUTION_BLOCKED",
        previous_attempts=5, previous_error_type="PermissionError")


@pytest.mark.parametrize("version", ["9", "4", "0"])
def test_wrong_version_or_status_leaves_state_unchanged(version):
    backend, store = blocked()
    before = copy.deepcopy(backend.state)
    with pytest.raises(ValueError): recover(store, version)
    assert backend.state == before


def test_duplicate_recovery_rejected():
    backend, store = blocked()
    recover(store)
    before = copy.deepcopy(backend.state)
    with pytest.raises(ValueError, match="resolution_not_blocked"): recover(store)
    assert backend.state == before


def test_cas_retry_commits_audit_once_and_exhaustion_does_not_write():
    backend, store = blocked()
    backend.conflicts = 2
    recover(store)
    assert sum(x["reason"] == "operator_resolution_retry" for x in backend.state["audit"]) == 1
    backend, store = blocked()
    before = copy.deepcopy(backend.state)
    backend.conflicts = 20
    with pytest.raises(Busy): recover(store)
    assert backend.state == before


def test_legacy_schema_rejected_and_adapter_only_targets_v2():
    backend, store = blocked()
    backend.state["schema_version"] = 1
    before = copy.deepcopy(backend.state)
    with pytest.raises(ValueError, match="isolated v2"): recover(store)
    assert backend.state == before
    kube = object.__new__(KubeV2)
    kube.ns, kube.name = "production", STATE_NAME
    kube.request = Mock()
    backend, store = blocked()
    kube.write_state("42", store.read())
    args, kwargs = kube.request.call_args
    assert args[0] == "/api/v1/namespaces/production/configmaps/model-release-state-v2"
    assert kwargs["data"]["metadata"]["resourceVersion"] == "42"


def test_cli_requires_explicit_live_enable_before_client_creation():
    with pytest.raises(SystemExit) as exc:
        main(["recover-resolution", "--model-version", "3", "--operator", "test", "--note", "fixed"])
    assert exc.value.code == 2
