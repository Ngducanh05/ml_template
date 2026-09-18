import logging

from fastapi import APIRouter, Depends

from model_service.api.dependencies import get_inference_service
from model_service.api.errors import ApiError
from model_service.model.errors import (
    InferenceError,
    InputContractError,
    NotReadyError,
)
from model_service.schemas.prediction import (
    ErrorResponse,
    PredictRequest,
    PredictResponse,
)
from model_service.services.inference import InferenceService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["inference"])


@router.post(
    "/predict",
    response_model=PredictResponse,
    responses={
        422: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
def predict(
    request: PredictRequest,
    service: InferenceService = Depends(get_inference_service),
) -> PredictResponse:
    instances = [instance.model_dump() for instance in request.instances]
    try:
        predictions = service.predict(instances)
    except InputContractError as exc:
        raise ApiError(422, "INPUT_CONTRACT_ERROR", "Input is not valid for the model") from exc
    except NotReadyError as exc:
        raise ApiError(503, "NOT_READY", "Model service is not ready") from exc
    except InferenceError as exc:
        logger.exception(
            "Inference request failed",
            extra={"component": "predict_route", "batch_size": len(instances)},
        )
        raise ApiError(
            500, "INFERENCE_FAILED", "Inference could not be completed"
        ) from exc
    return PredictResponse(predictions=predictions)
