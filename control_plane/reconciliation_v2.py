"""Bounded champion-pointer reconciliation through the exact same ingestion path."""
import json
import time
from .state_v2 import enqueue, BUCKET, parse_event
from .telemetry import log

CHAMPION_KEY = "mlflow/serving/mlops/credit-risk-classifier/champion.json"


class Reconciler:
    def __init__(self, store, source, adapter=parse_event, clock=time.time, interval=900):
        # Verified producer champion.json uses model_promoted schema v1.
        # The same strict parser and ingestion handle both sources.
        self.store, self.source, self.adapter = store, source, adapter
        self.clock, self.interval, self.next_attempt = clock, interval, 0

    def tick(self):
        if self.clock() < self.next_attempt: return None
        self.next_attempt = self.clock() + self.interval
        try:
            outcome = enqueue(self.store, self.adapter(self.source()), source="champion", clock=self.clock)
            log("RECONCILIATION_SUCCEEDED", outcome=outcome)
            return outcome
        except Exception as exc:
            log("RECONCILIATION_FAILED", error_type=type(exc).__name__)
            return "blocked"


def s3_pointer(s3):
    response = s3.get_object(Bucket=BUCKET, Key=CHAMPION_KEY)
    with response["Body"] as stream:
        raw = stream.read(65537)
    if len(raw) > 65536: raise ValueError("Champion pointer exceeds size bound")
    return json.loads(raw)
