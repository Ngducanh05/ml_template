"""Bounded valid inference load; run inside Dev against the ClusterIP DNS name.

Every request opens a new connection so Service balancing can select every pod.
JSONL is client-side evidence, not proof of per-pod distribution or HPA success.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import json
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

INSTANCE = {"sepal_length": 5.1, "sepal_width": 3.5,
            "petal_length": 1.4, "petal_width": 0.2}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def request(url, payload, timeout):
    start = time.monotonic()
    status = "transport_error"
    try:
        req = Request(url + "/api/v1/predict", data=payload,
                      headers={"Content-Type": "application/json", "Connection": "close"})
        with urlopen(req, timeout=timeout) as response:
            status = str(response.status)
            result = json.load(response)
            # This is a serving contract check for the trusted Iris input.
            if result != {"predictions": ["setosa"] * len(json.loads(payload)["instances"])}:
                status = "contract_error"
    except HTTPError as exc:
        status = str(exc.code)
        exc.close()
    except ValueError:
        status = "contract_error"
    except (URLError, TimeoutError, OSError):
        status = "transport_error"
    return status, time.monotonic() - start


def summarize(samples):
    counts = Counter(status for status, _ in samples)
    durations = sorted(duration for _, duration in samples)
    return {"requests": len(samples), "status_counts": dict(counts),
            "errors": len(samples) - counts["200"],
            "p95_seconds": durations[math.ceil(len(durations) * .95) - 1] if durations else None,
            "max_seconds": max(durations) if durations else None}


def run_load(url, *, duration, rps, concurrency, batch_size, timeout,
             error_fraction, p95_seconds, emit=print):
    payload = json.dumps({"instances": [INSTANCE] * batch_size}).encode()
    started = time.monotonic()
    deadline = started + duration
    next_send = started
    next_report = started + 10
    samples, interval_samples, pending = [], [], set()
    aborted = False
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        while time.monotonic() < deadline or pending:
            now = time.monotonic()
            if now < deadline and now >= next_send and len(pending) < concurrency:
                pending.add(pool.submit(request, url, payload, timeout))
                # No catch-up bursts if requests are slower than the requested rate.
                next_send = now + 1 / rps
            if pending:
                done, pending = wait(pending, timeout=min(.05, 1 / rps),
                                     return_when=FIRST_COMPLETED)
                for future in done:
                    sample = future.result()
                    samples.append(sample)
                    interval_samples.append(sample)
            else:
                time.sleep(min(.05, max(0, next_send - now)))
            if time.monotonic() >= next_report:
                interval = summarize(interval_samples)
                emit(json.dumps({"type": "interval", "timestamp": timestamp(), **interval}), flush=True)
                # Stop an unhealthy demo early, but keep evidence and drain in-flight work.
                if interval["requests"] and interval["errors"] / interval["requests"] > error_fraction:
                    deadline = time.monotonic()
                    aborted = True
                interval_samples = []
                next_report = time.monotonic() + 10
    elapsed = time.monotonic() - started
    summary = summarize(samples)
    passed = (bool(samples) and not aborted
              and summary["errors"] / len(samples) <= error_fraction
              and summary["p95_seconds"] <= p95_seconds)
    emit(json.dumps({"type": "summary", "timestamp": timestamp(), **summary,
                     "elapsed_seconds": elapsed, "achieved_rps": len(samples) / elapsed,
                     "aborted": aborted, "status": "PASS" if passed else "FAIL"}), flush=True)
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--namespace", help="Verified Dev namespace; client must run in cluster")
    target.add_argument("--local-only", type=int, metavar="PORT",
                        help="Local baseline only; never Kubernetes balancing evidence")
    parser.add_argument("--duration", type=int, default=180)
    parser.add_argument("--rps", type=float, default=100)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=2)
    parser.add_argument("--max-error-fraction", type=float, default=.01)
    parser.add_argument("--max-p95-seconds", type=float, default=1)
    args = parser.parse_args()
    if not (1 <= args.duration <= 600 and 0 < args.rps <= 500
            and 1 <= args.concurrency <= 16 and 1 <= args.batch_size <= 100
            and 0 < args.timeout <= 5 and 0 <= args.max_error_fraction <= .05
            and 0 < args.max_p95_seconds <= 5):
        parser.error("Bounds: duration 1..600, rps (0,500], concurrency 1..16, batch 1..100, "
                     "timeout/P95 (0,5], errors 0..0.05")
    if args.namespace:
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?", args.namespace):
            parser.error("Invalid namespace")
        url = f"http://model-service.{args.namespace}.svc.cluster.local:8000"
    else:
        if not 1024 <= args.local_only <= 65535:
            parser.error("Invalid local port")
        url = f"http://127.0.0.1:{args.local_only}"
    print(json.dumps({"type": "start", "timestamp": timestamp(), "url": url,
                      "transport": "cluster-service" if args.namespace else "local-only",
                      "settings": vars(args)}), flush=True)
    return run_load(url, duration=args.duration, rps=args.rps, concurrency=args.concurrency,
                    batch_size=args.batch_size, timeout=args.timeout,
                    error_fraction=args.max_error_fraction, p95_seconds=args.max_p95_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
