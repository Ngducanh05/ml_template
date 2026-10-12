import copy
import json
from unittest.mock import Mock
import pytest
from control_plane.state_v2 import (Store, initial_state, parse_event, enqueue, Conflict, Busy, HISTORY)
from control_plane.consumer_v2 import handle_message


def event(version=3, **changes):
    return dict(schema_version=1, event="model_promoted", model_name="credit-risk-classifier",
                model_version=str(version), alias="champion", workspace="mlops", run_id=f"run-{version}",
                previous_version=None, artifact_uri=f"s3://mlflow-test-artifacts-quangcm/mlflow/workspaces/mlops/27/models/m-{version}/artifacts",
                promoted_at="2026-10-08T00:00:00Z", **changes)


class Backend:
    def __init__(self, state=None):
        self.state = state or initial_state()
        self.revision = 0
        self.conflicts = 0
        self.fail = False

    def read_state(self):
        return str(self.revision), copy.deepcopy(self.state)

    def write_state(self, revision, state):
        if self.fail:
            raise OSError("unavailable")
        if self.conflicts:
            self.conflicts -= 1
            raise Conflict()
        if revision != str(self.revision):
            raise Conflict()
        self.state = copy.deepcopy(state)
        self.revision += 1


@pytest.mark.parametrize("field,value", [("schema_version", 2), ("schema_version", True),
    ("event", "other"), ("model_name", "other"), ("workspace", "other"), ("alias", "candidate"),
    ("model_version", "0"), ("model_version", "3.1"), ("model_version", 3),
    ("artifact_uri", "s3://other/model"), ("artifact_uri", "s3://mlflow-test-artifacts-quangcm/mlflow/workspaces/mlops/27/models/../artifacts"),
    ("run_id", ""), ("promoted_at", "yesterday")])
def test_reject_contract(field, value):
    data = event(); data[field] = value
    with pytest.raises(ValueError): parse_event(data)


def test_ingestion_order_conflict_and_restart():
    b = Backend(); s = Store(b)
    data = event(); data["future_field"] = "allowed"
    assert enqueue(s, data) == "accepted"
    assert enqueue(Store(b), event()) == "duplicate"
    assert enqueue(s, event(5)) == "accepted"
    assert enqueue(s, event(4)) == "stale"
    bad = event(5); bad["run_id"] = "different"
    assert enqueue(s, bad) == "identity_conflict"
    assert s.read()["highest_accepted"] == 5
    assert len(s.read()["pending"]) == 2


def test_cas_bounds_and_no_partial_commit():
    b = Backend(); b.conflicts = 2
    assert enqueue(Store(b), event()) == "accepted"
    assert b.revision == 1
    b.conflicts = 10
    with pytest.raises(Busy): enqueue(Store(b, retries=3), event(4))
    assert b.state["highest_accepted"] == 3
    b.conflicts = 0
    for _ in range(150): enqueue(Store(b), event())
    assert len(b.state["audit"]) == HISTORY


def test_receipt_deleted_only_after_persistence():
    b = Backend(); s = Store(b); sqs = Mock()
    msg = dict(Body=json.dumps(event()), ReceiptHandle="current", MessageId="irrelevant")
    def delete(**kw):
        assert b.revision > 0 and kw["ReceiptHandle"] == "current"
    sqs.delete_message.side_effect = delete
    assert handle_message(sqs, s, msg) == "accepted"
    assert handle_message(sqs, s, msg) == "duplicate"
    msg["Body"] = json.dumps(event(2))
    assert handle_message(sqs, s, msg) == "stale"
    assert sqs.delete_message.call_count == 3
    sqs.reset_mock(); b.fail = True
    with pytest.raises(OSError): handle_message(sqs, s, msg)
    sqs.delete_message.assert_not_called()
    b.fail = False; msg["Body"] = '{}'
    with pytest.raises(ValueError): handle_message(sqs, s, msg)
    sqs.delete_message.assert_not_called()
