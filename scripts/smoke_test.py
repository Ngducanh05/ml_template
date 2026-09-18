from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


VALID_INSTANCE = {
    "sepal_length": 5.1,
    "sepal_width": 3.5,
    "petal_length": 1.4,
    "petal_width": 0.2,
}
PUBLIC_CLASS_NAMES = {"setosa", "versicolor", "virginica"}


class SmokeTestError(RuntimeError):
    """Raised when the running service does not satisfy its HTTP contract."""


def _request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float,
) -> tuple[int, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        urljoin(f"{base_url.rstrip('/')}/", path.lstrip("/")),
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            raw_body = response.read().decode("utf-8")
    except HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise SmokeTestError(
            f"{method} {path} returned HTTP {exc.code}: {response_body}"
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise SmokeTestError(f"{method} {path} could not reach the service: {exc}") from exc

    try:
        return status, json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise SmokeTestError(
            f"{method} {path} returned invalid JSON: {raw_body!r}"
        ) from exc


def run_smoke_test(base_url: str, timeout: float) -> None:
    status, body = _request_json(base_url, "/health/live", timeout=timeout)
    if status != 200 or body != {"status": "live"}:
        raise SmokeTestError(
            f"GET /health/live failed contract check: status={status}, body={body!r}"
        )
    print("PASS: GET /health/live")

    status, body = _request_json(base_url, "/health/ready", timeout=timeout)
    if status != 200 or body != {"status": "ready"}:
        raise SmokeTestError(
            f"GET /health/ready failed contract check: status={status}, body={body!r}"
        )
    print("PASS: GET /health/ready")

    status, body = _request_json(
        base_url,
        "/api/v1/predict",
        method="POST",
        payload={"instances": [VALID_INSTANCE]},
        timeout=timeout,
    )
    predictions = body.get("predictions") if isinstance(body, dict) else None
    if (
        status != 200
        or not isinstance(predictions, list)
        or len(predictions) != 1
        or predictions[0] not in PUBLIC_CLASS_NAMES
    ):
        raise SmokeTestError(
            f"POST /api/v1/predict failed contract check: "
            f"status={status}, body={body!r}"
        )
    print(f"PASS: POST /api/v1/predict -> {predictions[0]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a running model service")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Service base URL (default: http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Per-request timeout in seconds (default: 5)",
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    try:
        run_smoke_test(args.base_url, args.timeout)
    except SmokeTestError as exc:
        print(f"SMOKE TEST FAILED: {exc}", file=sys.stderr)
        return 1

    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
