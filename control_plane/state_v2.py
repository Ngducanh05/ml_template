"""Bounded v2 promotion state. Never reads or migrates legacy release state."""
from __future__ import annotations
import copy
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import re
import time
from urllib.parse import urlsplit
from .state import Busy, Conflict

MODEL = "credit-risk-classifier"
ARTIFACT_PREFIX = "mlflow/workspaces/mlops/27/models/"
BUCKET = "mlflow-test-artifacts-quangcm"
STATE_NAME = "model-release-state-v2"
HISTORY = 100
MAX_PENDING = 20
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
REPOSITORY = "596432515999.dkr.ecr.ap-southeast-1.amazonaws.com/model-service"


def storage_uri(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("invalid_artifact_uri")
    u = urlsplit(value)
    key = u.path.lstrip("/")
    if (u.scheme != "s3" or u.netloc != BUCKET or u.query or u.fragment
            or not re.fullmatch(re.escape(ARTIFACT_PREFIX) + r"[A-Za-z0-9_-]+/artifacts", key)
            or value != f"s3://{BUCKET}/{key}"):
        raise ValueError("invalid_artifact_uri")
    return value


def runtime_image(value):
    if not isinstance(value, str) or not value.startswith(REPOSITORY + "@"):
        raise ValueError("unexpected_runtime_repository")
    if not DIGEST.fullmatch(value.split("@", 1)[1]):
        raise ValueError("invalid_runtime_digest")
    return value


@dataclass(frozen=True)
class ServingPair:
    model_version: str
    runtime_digest: str  # Full immutable repository@sha256 reference.
    storage_uri: str

    def __post_init__(self):
        numeric_version(self.model_version)
        runtime_image(self.runtime_digest)
        storage_uri(self.storage_uri)

    def record(self):
        return asdict(self)


def numeric_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", value) or int(value) > 2**31-1:
        raise ValueError("invalid_model_version")
    return int(value)


def parse_event(body):
    event = json.loads(body) if isinstance(body, (str, bytes)) else body
    if not isinstance(event, dict):
        raise ValueError("invalid_event")
    expected = dict(schema_version=1, event="model_promoted", model_name=MODEL,
                    alias="champion", workspace="mlops")
    for key, value in expected.items():
        if type(event.get(key)) is not type(value) or event[key] != value:
            raise ValueError("unsupported_" + key)
    numeric_version(event.get("model_version"))
    storage_uri(event.get("artifact_uri"))
    run = event.get("run_id")
    if not isinstance(run, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run):
        raise ValueError("invalid_run_id")
    previous = event.get("previous_version")
    if previous is not None:
        if numeric_version(previous) >= int(event["model_version"]):
            raise ValueError("invalid_previous_version")
    stamp = event.get("promoted_at")
    if not isinstance(stamp, str) or len(stamp) > 64:
        raise ValueError("invalid_promoted_at")
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise ValueError("invalid_promoted_at") from None
    return {key: event.get(key) for key in (*expected, "model_version", "artifact_uri",
                                           "run_id", "previous_version", "promoted_at")}


def identity(event):
    return {k: event[k] for k in ("model_name", "model_version", "artifact_uri", "run_id")}


def initial_state(stable=None, stable_fingerprint=None):
    if stable is not None:
        ServingPair(**stable)
        if not isinstance(stable_fingerprint, str) or not DIGEST.fullmatch(stable_fingerprint):
            raise ValueError("Stable runtime fingerprint required")
    return dict(schema_version=2, model_name=MODEL, highest_accepted=int(stable["model_version"]) if stable else 0,
                pending=[], active=None, stable=copy.deepcopy(stable), stable_fingerprint=stable_fingerprint,
                processed=[], failed=[], identities=[], audit=[],
                route=dict(weight=0, revision=0), lease=None)


def audit(state, reason, now, **fields):
    state["audit"] = (state["audit"] + [dict(time=now, reason=reason, **fields)])[-HISTORY:]


def ingest(state, event, *, now, source):
    """Mutate one snapshot only; duplicates/stale/conflicts also require CAS."""
    event = parse_event(event)
    version = int(event["model_version"])
    known = next((x for x in state["identities"] if x["model_version"] == event["model_version"]), None)
    if known:
        outcome = "duplicate" if known == identity(event) else "identity_conflict"
    elif version <= state["highest_accepted"]:
        outcome = "stale"
    else:
        if len(state["pending"]) >= MAX_PENDING:
            raise Busy("Pending promotion bound reached")
        state["pending"].append(dict(event=event, status="ACCEPTED", attempts=0, next_attempt=0))
        state["highest_accepted"] = version
        state["identities"] = (state["identities"] + [identity(event)])[-HISTORY:]
        outcome = "accepted"
    audit(state, outcome, now, model_version=event["model_version"], source=source)
    return outcome


class Store:
    def __init__(self, backend, retries=8):
        self.backend, self.retries = backend, retries

    def read(self):
        _, state = self.backend.read_state()
        self.validate(state)
        return copy.deepcopy(state)

    @staticmethod
    def validate(state):
        if state.get("schema_version") != 2 or state.get("model_name") != MODEL:
            raise ValueError("Not isolated v2 release state")
        route_state = state["route"]
        if (type(route_state["weight"]) is not int or route_state["weight"] not in (0, 10, 25, 50, 100)
                or type(route_state["revision"]) is not int or route_state["revision"] < 0):
            raise ValueError("Invalid route state")
        if state["stable"] is not None:
            ServingPair(**state["stable"])
            if not DIGEST.fullmatch(state["stable_fingerprint"]): raise ValueError("Invalid stable fingerprint")
        if state["active"]:
            ServingPair(**state["active"]["pair"])
            ServingPair(**state["active"]["previous_stable"])
            for key in ("fingerprint", "previous_fingerprint"):
                if not DIGEST.fullmatch(state["active"][key]): raise ValueError("Invalid active fingerprint")
            if state["active"]["phase"] not in ("PREPARING", "RAMP", "FINALIZING", "DRAINING", "ROLLBACK", "BLOCKED"):
                raise ValueError("Invalid release phase")
        for item in state["pending"]:
            if item["status"] in ("READY_FOR_RELEASE", "APPROVED_FOR_RELEASE"):
                pair = ServingPair(**item["pair"])
                if pair.model_version != item["event"]["model_version"] or pair.storage_uri != item["event"]["artifact_uri"]:
                    raise ValueError("Resolved candidate identity mismatch")
            if item.get("authorization") is not None or item["status"] == "APPROVED_FOR_RELEASE":
                from .authorization_v2 import validate_authorization
                if item["status"] != "APPROVED_FOR_RELEASE": raise ValueError("Authorization on unresolved candidate")
                validate_authorization(item.get("authorization"), item)
        if len(state["pending"]) > MAX_PENDING or any(len(state[k]) > HISTORY for k in ("audit", "identities", "processed", "failed")):
            raise Busy("State collection bound exceeded")
        if len(json.dumps(state, allow_nan=False).encode()) > 500_000:
            raise Busy("State byte bound exceeded")

    def update(self, mutate):
        for _ in range(self.retries):
            revision, original = self.backend.read_state()
            self.validate(original)
            state = copy.deepcopy(original)
            result = mutate(state)
            self.validate(state)
            try:
                self.backend.write_state(revision, state)
                return result
            except Conflict:
                continue
        raise Busy("CAS retries exhausted")


def enqueue(store, body, source="sqs", clock=time.time):
    event = parse_event(body)  # Invalid messages never advance state or get deleted.
    return store.update(lambda s: ingest(s, event, now=clock(), source=source))
