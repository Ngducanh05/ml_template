"""Explicit audited v2 resolution recovery; no resolver or deployment effects."""
import argparse
import json
import time
from .state_v2 import Store, audit, numeric_version


def recover_resolution(store, version, *, operator, note, clock=time.time):
    numeric_version(version)
    for name, value in (("operator", operator), ("note", note)):
        if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
            raise ValueError("invalid_" + name)
    def mutate(state):
        matches = [item for item in state["pending"] if item["event"]["model_version"] == version]
        if len(matches) != 1:
            raise ValueError("resolution_version_not_unique")
        item = matches[0]
        if item["status"] != "RESOLUTION_BLOCKED":
            raise ValueError("resolution_not_blocked")
        previous_attempts = item["attempts"]
        previous_error = item.pop("error_type", None)
        now = clock()
        item.update(status="RESOLUTION_RETRY", attempts=0, next_attempt=now)
        audit(state, "operator_resolution_retry", now, model_version=version,
              operator=operator, note=note, previous_status="RESOLUTION_BLOCKED",
              previous_attempts=previous_attempts, previous_error_type=previous_error)
        return {"model_version": version, "status": "RESOLUTION_RETRY"}
    return store.update(mutate)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["recover-resolution"])
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--note", required=True, help="Audit justification; never include credentials")
    parser.add_argument("--enable-live", action="store_true")
    args = parser.parse_args(argv)
    if not args.enable_live:
        parser.error("Live effects disabled; --enable-live is required")
    from .kube_v2 import KubeV2
    result = recover_resolution(Store(KubeV2()), args.model_version, operator=args.operator, note=args.note)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
