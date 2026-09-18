from fastapi import Request

from model_service.services.inference import InferenceService


def get_inference_service(request: Request) -> InferenceService:
    return request.app.state.inference_service
