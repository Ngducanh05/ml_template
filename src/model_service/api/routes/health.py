from fastapi import APIRouter, Depends

from model_service.api.dependencies import get_inference_service
from model_service.api.errors import ApiError
from model_service.schemas.prediction import ErrorResponse, HealthResponse
from model_service.services.inference import InferenceService

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=HealthResponse)
def live() -> HealthResponse:
    return HealthResponse(status="live")


@router.get(
    "/ready",
    response_model=HealthResponse,
    responses={503: {"model": ErrorResponse}},
)
def ready(
    service: InferenceService = Depends(get_inference_service),
) -> HealthResponse:
    if not service.is_ready():
        raise ApiError(503, "NOT_READY", "Model service is not ready")
    return HealthResponse(status="ready")
