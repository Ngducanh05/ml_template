"""Durable ServingPair release controller, isolated from legacy digest semantics."""
import copy
import math
import time
import uuid
from urllib.error import HTTPError, URLError
from .state import Busy
from .state_v2 import audit, HISTORY
from .authorization_v2 import claimable, validate_authorization
from .policy import Policy
from .controller import assess
from .telemetry import log


def route(state, weight):
    if state["route"]["weight"] != weight:
        state["route"] = dict(weight=weight, revision=state["route"]["revision"] + 1)


class Controller:
    def __init__(self, store, effects, owner=None, clock=time.time, policy=None):
        self.store, self.effects = store, effects
        self.owner, self.clock = owner or str(uuid.uuid4()), clock
        self.policy = policy or Policy.from_env()

    def acquire(self):
        def claim(s):
            lease = s["lease"]
            if lease and lease["owner"] != self.owner and lease["until"] > self.clock():
                return False
            s["lease"] = dict(owner=self.owner, until=self.clock() + 60)
            return True
        return self.store.update(claim)

    def guard(self, state):
        lease = state["lease"]
        if not lease or lease["owner"] != self.owner or lease["until"] <= self.clock() + 10:
            raise Busy("Controller lease lost")

    def change(self, mutate):
        def guarded(s):
            self.guard(s)
            return mutate(s)
        return self.store.update(guarded)

    def ensure(self, lane, pair, fingerprint):
        self.guard(self.store.read())
        return self.effects.ensure(lane, pair, fingerprint)

    def rollback(self, reason):
        def change(s):
            a = s["active"]
            if a["phase"] in ("ROLLBACK", "BLOCKED"):
                return
            a.update(phase="ROLLBACK", reason=reason, deadline=self.clock() + self.policy.timeout)
            route(s, 0)
            audit(s, "rollback_requested", self.clock(), detail=reason)
        self.change(change)
        log("ROLLBACK_REQUESTED", reason=reason)

    def ack(self, state):
        seen = self.effects.router()
        return bool(seen.get("ready") and seen.get("weight") == state["route"]["weight"]
                    and seen.get("revision") == state["route"]["revision"])

    def tick(self):
        if not self.acquire():
            return
        s = self.store.read(); self.guard(s)
        a = s["active"]
        if a is None:
            def claim(state):
                # An operator must establish an exact ready KServe stable baseline.
                # Never infer a ServingPair from legacy model-release-state.
                if state["stable"] is None:
                    return
                eligible = [x for x in state["pending"] if claimable(state, x, self.clock())
                            and int(x["pair"]["model_version"]) > int(state["stable"]["model_version"])]
                if not eligible:
                    return
                candidate = max(eligible, key=lambda x: int(x["pair"]["model_version"]))
                # Remove older queued versions only when a newer resolved version
                # is claimed. Their identities remain handled; never redeploy them.
                state["pending"] = [x for x in state["pending"]
                                    if int(x["event"]["model_version"]) > int(candidate["pair"]["model_version"])]
                state["active"] = dict(pair=copy.deepcopy(candidate["pair"]), fingerprint=candidate["fingerprint"],
                    phase="PREPARING", previous_stable=copy.deepcopy(state["stable"]),
                    previous_fingerprint=state["stable_fingerprint"], deadline=self.clock() + self.policy.timeout,
                    stage_since=None, authorization=copy.deepcopy(candidate["authorization"]))
                route(state, 0)
                audit(state, "release_started", self.clock(), pair=candidate["pair"], authorization_id=candidate["authorization"]["evidence_hash"])
            self.change(claim)
            return
        phase = a["phase"]
        if phase not in ("ROLLBACK", "BLOCKED"):
            try:
                auth = a['authorization']
                validate_authorization(auth, dict(pair=a['pair'], fingerprint=a['fingerprint'],
                    event=auth['evidence']['candidate']['event']))
            except (KeyError, ValueError, TypeError):
                self.rollback('missing_release_authorization')
                return
        try:
            self.advance(s, a)
        except Busy:
            raise
        except (URLError, TimeoutError, ConnectionError) as exc:
            if phase == "FINALIZING" and not isinstance(exc, HTTPError):
                log("FINALIZE_WAIT_BACKEND", error_type=type(exc).__name__, deadline=a["deadline"])
                if self.clock() >= a["deadline"]:
                    self.rollback("stable_backend_unavailable")
            elif phase in ("ROLLBACK", "BLOCKED"):
                self.block_if_expired(a)
            else:
                self.rollback("backend_unavailable")
        except Exception as exc:
            log("RELEASE_ERROR", phase=phase, error_type=type(exc).__name__)
            if phase in ("ROLLBACK", "BLOCKED"):
                self.block_if_expired(a)
            else:
                self.rollback("invalid_backend_contract" if isinstance(exc, ValueError) else "effect_failure")

    def block_if_expired(self, active):
        if self.clock() >= active["deadline"]:
            self.change(lambda s: s["active"].update(phase="BLOCKED"))
            log("RECOVERY_BLOCKED", reason="previous_stable_or_router_unavailable")

    def advance(self, s, a):
        phase, pair, fp = a["phase"], a["pair"], a["fingerprint"]
        expired = self.clock() >= a["deadline"]
        if phase == "PREPARING":
            self.ensure("canary", pair, fp)
            if expired:
                self.rollback("candidate_readiness_timeout")
            elif self.effects.ready("canary", pair, fp):
                def arm(state):
                    state["active"].update(phase="RAMP", stage_since=None, deadline=self.clock() + self.policy.timeout)
                    route(state, 10)
                self.change(arm)
        elif phase == "RAMP":
            if not self.effects.ready("canary", pair, fp) or not self.effects.ready("stable", s["stable"], s["stable_fingerprint"]):
                self.rollback("backend_unhealthy"); return
            if not self.ack(s):
                if expired: self.rollback("router_ack_timeout")
                return
            if a["stage_since"] is None:
                if expired: self.rollback("stage_deadline_exceeded")
                else: self.change(lambda state: state["active"].update(stage_since=self.clock()))
                return
            values = self.effects.metrics()
            if any(not math.isfinite(values.get(k, float("nan"))) for k in ("up", "age", "traffic")) or values["traffic"] < 0:
                self.rollback("invalid_canary_metrics"); return
            if values["up"] != 1 or not 0 <= values["age"] <= 15:
                self.rollback("router_unavailable_or_stale"); return
            if values["traffic"] < self.policy.minimum:
                if expired: self.rollback("insufficient_canary_traffic")
                else: log("RAMP_WAIT_TRAFFIC", reason="insufficient_observations", observations=values["traffic"])
                return
            reasons = assess(values, self.policy.error_limit, self.policy.p95_limit)
            if reasons:
                self.rollback(",".join(reasons)); return
            if expired:
                self.rollback("stage_deadline_exceeded"); return
            if self.clock() - a["stage_since"] < self.policy.dwell:
                return
            weight = s["route"]["weight"]
            def advance(state):
                if weight == 100:
                    state["active"].update(phase="FINALIZING", deadline=self.clock() + self.policy.timeout)
                else:
                    route(state, {10: 25, 25: 50, 50: 100}[weight])
                    state["active"].update(stage_since=None, deadline=self.clock() + self.policy.timeout)
                audit(state, "stage_promoted", self.clock(), previous_weight=weight)
            self.change(advance)
        elif phase == "FINALIZING":
            self.ensure("stable", pair, fp)
            ready = self.effects.ready("stable", pair, fp)
            if expired:
                self.rollback("stable_readiness_timeout")
            elif ready:
                def commit(state):
                    state["stable"] = copy.deepcopy(pair)
                    state["stable_fingerprint"] = fp
                    state["active"].update(phase="DRAINING", deadline=self.clock() + self.policy.timeout)
                    route(state, 0)
                    audit(state, "stable_committed", self.clock(), pair=pair)
                self.change(commit)
        elif phase == "DRAINING":
            if not self.effects.ready("stable", pair, fp):
                self.rollback("stable_unhealthy_during_drain"); return
            if not self.ack(s):
                if expired: self.rollback("drain_router_timeout")
                return
            def complete(state):
                state["processed"] = (state["processed"] + [copy.deepcopy(pair)])[-HISTORY:]
                state["active"] = None
                audit(state, "release_completed", self.clock(), pair=pair, authorization_id=a.get("authorization", {}).get("evidence_hash"))
            self.change(complete)
        elif phase in ("ROLLBACK", "BLOCKED"):
            self.change(lambda state: route(state, 0))
            previous, previous_fp = a["previous_stable"], a["previous_fingerprint"]
            self.ensure("stable", previous, previous_fp)
            if not self.effects.ready("stable", previous, previous_fp) or not self.ack(self.store.read()):
                self.block_if_expired(a); return
            def restored(state):
                state["stable"] = copy.deepcopy(previous)
                state["stable_fingerprint"] = previous_fp
                state["failed"] = (state["failed"] + [dict(pair=pair, reason=a["reason"])])[-HISTORY:]
                state["active"] = None
                audit(state, "rollback_completed", self.clock(), previous_stable=previous, authorization_id=a.get("authorization", {}).get("evidence_hash"))
            self.change(restored)
