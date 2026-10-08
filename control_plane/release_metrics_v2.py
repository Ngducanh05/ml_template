"""Bounded-cardinality release telemetry from verified CAS state, never raw labels."""
import copy
import threading
import time
from prometheus_client.core import GaugeMetricFamily

PHASES = ('IDLE', 'PREPARING', 'RAMP', 'FINALIZING', 'DRAINING', 'ROLLBACK', 'BLOCKED')
OUTCOMES = {'release_completed': 'success', 'rollback_completed': 'rollback', 'release_blocked': 'blocked'}


class ReleaseCollector:
    def __init__(self, clock=time.time, max_age=10):
        self.clock, self.max_age = clock, max_age
        self.lock = threading.Lock()
        self.snapshot = None
        self.updated = None

    def update(self, state):
        from .state_v2 import Store
        Store.validate(state)
        with self.lock:
            self.snapshot = copy.deepcopy(state)
            self.updated = self.clock()

    def collect(self):
        with self.lock:
            state, updated = self.snapshot, self.updated
        fresh = updated is not None and 0 <= self.clock() - updated <= self.max_age
        health = GaugeMetricFamily('mlops_v2_release_snapshot_fresh', 'Fresh validated CAS snapshot (not serving health)')
        health.add_metric([], int(fresh)); yield health
        if not fresh:
            return  # Never retain stale model/version/outcome values as healthy.
        stable = GaugeMetricFamily('mlops_v2_stable_version', 'Durable stable version; does not prove backend readiness')
        if state['stable']:
            stable.add_metric([], int(state['stable']['model_version']))
        yield stable
        candidate = GaugeMetricFamily('mlops_v2_candidate_version', 'Active candidate version; absent when idle')
        active = state['active']
        if active:
            candidate.add_metric([], int(active['pair']['model_version']))
        yield candidate
        phase = GaugeMetricFamily('mlops_v2_release_phase', 'Current release phase', labels=['phase'])
        current = active['phase'] if active else 'IDLE'
        for value in PHASES:
            phase.add_metric([value], int(current == value))
        yield phase
        pending = GaugeMetricFamily('mlops_v2_pending_releases', 'Pending releases by bounded state', labels=['status'])
        for value in ('ACCEPTED', 'RESOLUTION_RETRY', 'RESOLUTION_BLOCKED', 'BUILD_REQUIRED', 'READY_FOR_RELEASE'):
            pending.add_metric([value], sum(x['status']==value for x in state['pending']))
        yield pending
        last = next((row for row in reversed(state['audit']) if row['reason'] in OUTCOMES), None)
        if last:
            outcome = GaugeMetricFamily('mlops_v2_last_outcome_timestamp_seconds', 'Timestamp of last terminal audit outcome', labels=['outcome'])
            outcome.add_metric([OUTCOMES[last['reason']]], last['time']); yield outcome
        started = next((row for row in reversed(state['audit']) if row['reason']=='release_started' and active and row.get('pair')==active['pair']), None)
        if started:
            age = GaugeMetricFamily('mlops_v2_release_elapsed_seconds', 'Elapsed time since active release began')
            age.add_metric([], max(0, self.clock()-started['time'])); yield age
        stages = GaugeMetricFamily('mlops_v2_stage_promotion_timestamp_seconds', 'Latest retained stage audit timestamp', labels=['weight'])
        for weight in (10,25,50,100):
            row = next((r for r in reversed(state['audit']) if r['reason']=='stage_promoted' and r.get('previous_weight')==weight),None)
            if row: stages.add_metric([str(weight)],row['time'])
        yield stages
