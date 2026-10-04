from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from model_service.api.errors import (
    ApiError,
    api_error_handler,
    validation_error_handler,
)
from model_service.api.routes.health import router as health_router
from model_service.api.routes.predict import router as predict_router
from model_service.core.config import Settings
from model_service.metrics import HttpMetricsMiddleware, ServingMetrics
from model_service.model.adapter import ModelAdapter, SklearnJoblibAdapter
from model_service.services.inference import InferenceService

AdapterFactory = Callable[[Settings], ModelAdapter]


def create_app(
    settings: Settings | None = None,
    adapter_factory: AdapterFactory | None = None,
) -> FastAPI:
    metrics = ServingMetrics()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_settings = settings or Settings.from_env()
        logging.basicConfig(
            level=resolved_settings.log_level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        adapter = (
            adapter_factory(resolved_settings)
            if adapter_factory is not None
            else SklearnJoblibAdapter(resolved_settings.model_path)
        )
        adapter.load()
        metrics.set_identity(
            adapter.contract.model_version
            if isinstance(adapter, SklearnJoblibAdapter)
            else None
        )
        app.state.inference_service = InferenceService(
            adapter=adapter,
            model_name=resolved_settings.model_name,
            metrics=metrics,
        )
        yield

    app = FastAPI(title="Model Service", version="0", lifespan=lifespan)
    app.state.metrics = metrics
    app.add_middleware(HttpMetricsMiddleware, metrics=metrics)
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(  # type: ignore[arg-type]
        RequestValidationError, validation_error_handler
    )
    app.include_router(health_router)
    app.include_router(predict_router)

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics() -> Response:
        return Response(
            generate_latest(metrics.registry),
            headers={"Content-Type": CONTENT_TYPE_LATEST},
        )

    return app


app = create_app()
