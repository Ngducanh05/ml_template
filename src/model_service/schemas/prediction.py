from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from model_service.model.contract import IrisClassName


class IrisInstance(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    sepal_length: float = Field(gt=0, strict=True)
    sepal_width: float = Field(gt=0, strict=True)
    petal_length: float = Field(gt=0, strict=True)
    petal_width: float = Field(gt=0, strict=True)


class PredictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instances: list[IrisInstance] = Field(min_length=1, max_length=100)


class PredictResponse(BaseModel):
    predictions: list[IrisClassName]


class HealthResponse(BaseModel):
    status: Literal["live", "ready"]


class ErrorResponse(BaseModel):
    code: str
    message: str
