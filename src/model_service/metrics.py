"""Single-worker serving metrics with an isolated registry for each app."""

from __future__ import annotations

import json
import re
from pathlib import Path
from time import perf_counter

from prometheus_client import CollectorRegistry, Counter, Histogram, Info
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
)
EXCLUDED_PATHS = frozenset({"/metrics", "/health/live", "/health/ready"})
INFERENCE_OUTCOMES = ("success", "input_error", "not_ready", "error")
DURATION_BUCKETS = (
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1,
    2.5,
    5,
    10,
)
BUILD_IDENTITY_PATH = Path(__file__).with_name("build-identity.json")


class ServingMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.http_requests = Counter(
            "model_service_http_requests_total",
            "HTTP responses by normalized route, including validation and server errors; probes excluded.",
            ("method", "route", "status"),
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "model_service_http_request_duration_seconds",
            "Time from HTTP entry to final response body; includes validation, queueing and serialization.",
            ("method", "route", "status"),
            buckets=DURATION_BUCKETS,
            registry=self.registry,
        )
        self.inference_requests = Counter(
            "model_service_inference_requests_total",
            "Inference service calls by result; each batch is one call; schema rejections excluded.",
            ("outcome",),
            registry=self.registry,
        )
        self.inference_items = Counter(
            "model_service_inference_items_total",
            "Attempted batch items by call result, including items in failed calls.",
            ("outcome",),
            registry=self.registry,
        )
        self.inference_duration = Histogram(
            "model_service_inference_duration_seconds",
            "Inference service wall time including readiness, adapter and output-count checks; excludes HTTP work.",
            ("outcome",),
            buckets=DURATION_BUCKETS,
            registry=self.registry,
        )
        self.build = Info(
            "model_service_build",
            "Baked runtime source identity and loaded artifact version; unknown means unverified.",
            registry=self.registry,
        )
        # Known outcomes start at zero so no errors is distinguishable from missing
        # targets. Rate/latency queries must still check that there was traffic.
        for outcome in INFERENCE_OUTCOMES:
            self.inference_requests.labels(outcome)
            self.inference_items.labels(outcome)
            self.inference_duration.labels(outcome)
        for status in ("200", "422", "500", "503"):
            self.http_requests.labels("POST", "/api/v1/predict", status)
            self.http_duration.labels("POST", "/api/v1/predict", status)

    def record_inference(self, outcome: str, item_count: int, duration: float) -> None:
        self.inference_requests.labels(outcome).inc()
        self.inference_items.labels(outcome).inc(item_count)
        self.inference_duration.labels(outcome).observe(duration)

    def set_identity(self, model_version: str | None) -> None:
        identity = {
            "source_commit": "unknown",
            "source_digest": "unknown",
            "dirty": "unknown",
        }
        try:
            raw = json.loads(BUILD_IDENTITY_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        # Whitelist fields and their syntax: never export arbitrary file contents.
        if isinstance(raw, dict):
            commit = raw.get("source_commit")
            digest = raw.get("source_digest")
            dirty = raw.get("source_dirty")
            if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit):
                identity["source_commit"] = commit
            if isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                identity["source_digest"] = digest
            if isinstance(dirty, bool):
                identity["dirty"] = str(dirty).lower()
        identity["model_version"] = model_version or "unknown"
        self.build.info(identity)


class HttpMetricsMiddleware:
    """Pure ASGI middleware: one observation per completed response or failure."""

    def __init__(self, app: ASGIApp, metrics: ServingMetrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].rstrip("/") in EXCLUDED_PATHS:
            await self.app(scope, receive, send)
            return

        started = perf_counter()
        status = 500
        recorded = False

        def record() -> None:
            nonlocal recorded
            if recorded:
                return
            recorded = True
            route = getattr(scope.get("route"), "path", "unmatched")
            method = scope["method"] if scope["method"] in HTTP_METHODS else "OTHER"
            status_label = str(status) if 100 <= status <= 599 else "other"
            self.metrics.http_requests.labels(method, route, status_label).inc()
            self.metrics.http_duration.labels(method, route, status_label).observe(
                perf_counter() - started
            )

        async def measured_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                record()

        try:
            await self.app(scope, receive, measured_send)
        except Exception:
            # ServerErrorMiddleware sits outside user middleware. Before headers,
            # it will return 500; after headers, retain the actual status sent.
            record()
            raise
