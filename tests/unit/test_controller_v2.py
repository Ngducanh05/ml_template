import copy
from urllib.error import URLError
from unittest.mock import Mock
import pytest
from control_plane.state_v2 import Store, initial_state, enqueue
from control_plane.controller_v2 import Controller
from control_plane.policy import Policy
from control_plane.processor_v2 import Processor
from control_plane.kserve_effects import runtime_manifest, lane_manifest, KServeEffects, LANES
from test_state_v2 import Backend, event
from test_processor_v2 import IMAGE, FP, CONTRACT

OLD = dict(model_version="1", runtime_digest=IMAGE, storage_uri=event(1)["artifact_uri"])


class Effects:
    def __init__(self, store):
        self.store = store; self.lanes = {"stable": copy.deepcopy(OLD)}; self.failure = None
        self.values = dict(up=1, age=1, traffic=10, success=10, errors=0, p95=.025, stable_errors=0)
        self.weights = []
    def ensure(self, lane, pair, fingerprint): self.lanes[lane] = copy.deepcopy(pair)
    def ready(self, lane, pair, fingerprint):
        if self.failure: return self.failure(lane, pair)
        return self.lanes.get(lane) == pair
    def router(self):
        state = self.store.read(); self.weights.append(state["route"]["weight"])
        return dict(ready=True, **state["route"])
    def metrics(self): return self.values


def setup():
    b = Backend(initial_state(OLD, FP)); s = Store(b)
    enqueue(s, event())
    source = Mock(); source.inspect.return_value = CONTRACT
    catalog = Mock(); catalog.lookup.return_value = IMAGE
    Processor(s, source, catalog).tick()
    e = Effects(s); clock = [0]
    c = Controller(s, e, owner="one", clock=lambda: clock[0], policy=Policy(1, 0, 300, .05, .1))
    return b, s, e, clock, c


def until(c, s, phase):
    for _ in range(25):
        c.tick()
        if s.read()["active"] and s.read()["active"]["phase"] == phase: return
    raise AssertionError(phase)


def test_restart_all_phases_and_healthy_promotion():
    b, s, e, clock, c = setup()
    for phase in ("PREPARING", "RAMP", "FINALIZING", "DRAINING"):
        until(c, s, phase)
        c = Controller(Store(b), e, owner="one", clock=lambda: clock[0], policy=c.policy)
    c.tick()
    assert s.read()["active"] is None
    assert s.read()["stable"]["storage_uri"] == event()["artifact_uri"]
    assert set(e.weights) >= {0, 10, 25, 50, 100}
    assert len(s.read()["processed"]) == 1
    c.tick(); assert s.read()["active"] is None


@pytest.mark.parametrize("metric,value,reason", [("errors", .5, "canary_errors"), ("p95", 1., "canary_latency"), ("p95", float("nan"), "invalid_canary_metrics")])
def test_metrics_rollback_exact_pair(metric, value, reason):
    _, s, e, _, c = setup(); until(c, s, "RAMP")
    e.values[metric] = value; c.tick(); c.tick()
    assert s.read()["active"]["reason"] == reason
    c.tick(); assert s.read()["stable"] == OLD and s.read()["active"] is None
    assert s.read()["failed"][0]["reason"] == reason
    assert enqueue(s, event()) == "duplicate"


def test_zero_traffic_wait_then_deadline():
    _, s, e, clock, c = setup(); until(c, s, "RAMP"); c.tick()
    e.values.update(traffic=0, success=0, errors=float("nan"), p95=float("nan"))
    c.tick(); assert s.read()["route"]["weight"] == 10
    clock[0] = 301; c.tick()
    assert s.read()["active"]["reason"] == "insufficient_canary_traffic"
    c.tick(); assert s.read()["stable"] == OLD


def test_finalization_transport_retry_and_exact_rollback_blocked_recovery():
    _, s, e, clock, c = setup(); until(c, s, "FINALIZING")
    def unavailable(lane, pair): raise URLError("transient")
    e.failure = unavailable; c.tick()
    assert s.read()["active"]["phase"] == "FINALIZING" and s.read()["stable"] == OLD
    clock[0] = 301; c.tick(); assert s.read()["active"]["phase"] == "ROLLBACK"
    assert e.lanes["stable"] != OLD
    clock[0] = 602; c.tick(); assert s.read()["active"]["phase"] == "BLOCKED"
    assert e.lanes["stable"] == OLD
    e.failure = None; c.tick()
    assert s.read()["active"] is None and s.read()["stable"] == OLD


def test_transient_finalization_success_and_metadata_hard_failure():
    _, s, e, _, c = setup(); until(c, s, "FINALIZING")
    def mismatch(lane, pair): raise ValueError("metadata mismatch")
    e.failure = mismatch; c.tick()
    assert s.read()["active"]["reason"] == "invalid_backend_contract"
    e.failure = None; c.tick(); assert s.read()["stable"] == OLD


def test_lease_conflict_cas_retry_and_no_uninitialized_release():
    b, s, e, clock, c = setup(); b.conflicts = 2; c.tick()
    other = Controller(s, e, owner="two", clock=lambda: 1, policy=c.policy)
    other.tick(); assert s.read()["active"]["phase"] == "PREPARING"
    clock[0] = 70; other.clock = lambda: 70; other.tick()
    assert s.read()["lease"]["owner"] == "two"
    empty = Store(Backend()); enqueue(empty, event())
    Controller(empty, e).tick(); assert empty.read()["active"] is None


def test_runtime_and_lane_builders_and_collision():
    desired = runtime_manifest(FP, IMAGE)
    assert desired["spec"]["containers"][0]["image"] == IMAGE
    assert desired["spec"]["containers"][0]["securityContext"]["capabilities"]["drop"] == ["ALL"]
    lane = lane_manifest("canary", OLD, FP)
    assert lane["metadata"]["name"] == LANES["canary"]
    assert lane["spec"]["predictor"]["model"]["storageUri"] == OLD["storage_uri"]
    api = Mock(); api.get.return_value = copy.deepcopy(desired)
    e = KServeEffects(api, Mock(), Mock())
    e.ensure_runtime(FP, IMAGE); api.create.assert_not_called()
    api.get.return_value["spec"]["containers"][0]["image"] = IMAGE.replace("a" * 64, "c" * 64)
    with pytest.raises(ValueError, match="collision"): e.ensure_runtime(FP, IMAGE)
    api.replace.assert_not_called()
