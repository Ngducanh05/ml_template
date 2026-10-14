"""GitHub-side, fail-closed trigger for exact BUILD_REQUIRED contracts.

The trigger owns no Kubernetes mutation and no build logic.  It reads the
isolated v2 state, checks the immutable ECR catalog, and dispatches the
existing runtime-build workflow once for an exact technical fingerprint.
GitHub workflow history is the durable dispatch receipt.  Any earlier
publication attempt for that fingerprint blocks automatic retry so a
Security or infrastructure failure cannot become a build loop.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

from control_plane.authorization_backend import AuthorizationBackend
from control_plane.build_contract import validate_request
from control_plane.runtime_catalog import Catalog
from control_plane.state_v2 import Store

REPOSITORY = "Ngducanh05/ml_template"
WORKFLOW = "runtime-build.yml"
RUN_PREFIX = "Runtime build publish-true "
ACTIVE = frozenset(("queued", "in_progress", "pending", "requested", "waiting"))
TERMINAL_FAILURE = frozenset(("failure", "cancelled", "timed_out", "action_required", "stale", "skipped"))
OUT = Path("evidence/runtime-build-trigger.json")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical_request(state):
    """Return only the latest BUILD_REQUIRED technical contract."""
    Store.validate(state)
    matches = [item for item in state["pending"]
               if item["status"] == "BUILD_REQUIRED"
               and int(item["event"]["model_version"]) == state["highest_accepted"]]
    require(len(matches) <= 1, "Ambiguous latest BUILD_REQUIRED candidate")
    if not matches:
        return None
    item = matches[0]
    require("pair" not in item and item.get("authorization") is None,
            "BUILD_REQUIRED candidate already resolved or authorized")
    request = item.get("build_request")
    validate_request(request)
    require(request["fingerprint"] == item.get("fingerprint"),
            "Candidate fingerprint does not match build contract")
    # The exact serialization becomes the workflow input; no model identity or
    # artifact location is added to the technical build boundary.
    return json.dumps(request, sort_keys=True, separators=(",", ":"))


def expected_title(fingerprint):
    return RUN_PREFIX + fingerprint


def previous_attempt(runs, fingerprint):
    title = expected_title(fingerprint)
    matches = [run for run in runs if run.get("display_title") == title]
    for run in matches:
        require(run.get("event") == "workflow_dispatch", "Unexpected runtime build event")
        require(run.get("head_branch") == "main", "Runtime build attempt did not use main")
        status = run.get("status")
        conclusion = run.get("conclusion")
        if status in ACTIVE:
            return "ACTIVE", run
        require(status == "completed", "Unknown runtime build status")
        if conclusion == "success":
            return "SUCCEEDED_WAITING_FOR_CATALOG", run
        require(conclusion in TERMINAL_FAILURE, "Unknown runtime build conclusion")
        return "BLOCKED_PREVIOUS_ATTEMPT", run
    return None, None


def decide(state, catalog, runs):
    request_json = canonical_request(state)
    if request_json is None:
        return {"outcome": "NO_BUILD_REQUIRED"}
    request = json.loads(request_json)
    fingerprint = request["fingerprint"]
    image = catalog.lookup(fingerprint)  # Operational AWS errors propagate; never become MISS.
    if image is not None:
        return {"outcome": "CATALOG_HIT", "fingerprint": fingerprint, "image": image}
    outcome, run = previous_attempt(runs, fingerprint)
    if outcome:
        return {"outcome": outcome, "fingerprint": fingerprint, "run_id": str(run["id"])}
    return {"outcome": "DISPATCH", "fingerprint": fingerprint,
            "technical_contract": request_json}


def github_request(path, *, method="GET", data=None):
    require(os.environ.get("GITHUB_REPOSITORY") == REPOSITORY, "Unexpected GitHub repository")
    api = os.environ.get("GITHUB_API_URL")
    token = os.environ.get("GH_TOKEN")
    require(api == "https://api.github.com" and token, "Trusted GitHub API context required")
    body = None if data is None else json.dumps(data, separators=(",", ":")).encode()
    request = Request(f"{api}/repos/{REPOSITORY}/{path}", method=method, data=body, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urlopen(request, timeout=30) as response:
        raw = response.read(1_048_577)
        require(len(raw) <= 1_048_576, "GitHub response exceeds bound")
        return json.loads(raw) if raw else None


def workflow_runs():
    result = github_request(f"actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&branch=main&per_page=100")
    require(isinstance(result, dict) and isinstance(result.get("workflow_runs"), list),
            "Malformed GitHub workflow history")
    require(result.get("total_count", 0) <= 100,
            "Runtime build history exceeds bounded automatic dedup window")
    return result["workflow_runs"]


def state_store():
    encoded = os.environ.get("RELEASE_STATE_READER_B64", "")
    require(len(encoded) <= 131072, "Release reader configuration exceeds bound")
    try:
        config = json.loads(base64.b64decode(encoded, validate=True))
    except Exception as exc:
        raise ValueError("Invalid release reader configuration") from exc
    return Store(AuthorizationBackend(config))


def catalog():
    import boto3
    require(bool(os.environ.get("AWS_SESSION_TOKEN")), "Short-lived OIDC credentials required")
    return Catalog(boto3.client("ecr", region_name="ap-southeast-1"))


def dispatch(technical_contract):
    github_request(f"actions/workflows/{WORKFLOW}/dispatches", method="POST",
                   data={"ref": "main", "inputs": {
                       "technical_contract": technical_contract, "publish": True}})


def write_evidence(result):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # Never retain the technical request here; the dispatched workflow validates
    # and stores it as its own bounded runtime-context evidence.
    safe = {key: value for key, value in result.items() if key != "technical_contract"}
    OUT.write_text(json.dumps(safe, sort_keys=True, separators=(",", ":")))


def main():
    require(os.environ.get("GITHUB_EVENT_NAME") == "schedule", "Only scheduled main workflow may auto-dispatch")
    require(os.environ.get("GITHUB_REF") == "refs/heads/main", "Runtime trigger requires main")
    result = decide(state_store().read(), catalog(), workflow_runs())
    if result["outcome"] == "DISPATCH":
        dispatch(result["technical_contract"])
        result = {**result, "outcome": "DISPATCHED"}
    write_evidence(result)
    print(json.dumps({key: value for key, value in result.items() if key != "technical_contract"}, sort_keys=True))


if __name__ == "__main__":
    main()
