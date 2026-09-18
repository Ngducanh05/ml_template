import math

import pytest
from pydantic import ValidationError

from model_service.model.contract import IrisClassName
from model_service.schemas.prediction import IrisInstance, PredictRequest, PredictResponse


VALID_INSTANCE = {
    "sepal_length": 5.1,
    "sepal_width": 3.5,
    "petal_length": 1.4,
    "petal_width": 0.2,
}


@pytest.mark.parametrize("invalid_value", [0, -1, math.inf, -math.inf, math.nan, True])
def test_instance_rejects_values_outside_positive_finite_numbers(
    invalid_value: object,
) -> None:
    payload = {**VALID_INSTANCE, "sepal_length": invalid_value}

    with pytest.raises(ValidationError):
        IrisInstance.model_validate(payload)


def test_instance_rejects_missing_and_extra_features() -> None:
    with pytest.raises(ValidationError):
        IrisInstance.model_validate(
            {
                key: value
                for key, value in VALID_INSTANCE.items()
                if key != "petal_width"
            }
        )
    with pytest.raises(ValidationError):
        IrisInstance.model_validate({**VALID_INSTANCE, "unexpected": 1.0})


def test_instance_accepts_integer_or_float_numbers() -> None:
    instance = IrisInstance.model_validate(
        {
            "sepal_length": 5,
            "sepal_width": 3.5,
            "petal_length": 1,
            "petal_width": 0.2,
        }
    )

    assert instance.sepal_length == 5.0
    assert instance.petal_length == 1.0


@pytest.mark.parametrize("batch_size", [1, 100])
def test_request_accepts_verified_batch_boundaries(batch_size: int) -> None:
    request = PredictRequest.model_validate({"instances": [VALID_INSTANCE] * batch_size})

    assert len(request.instances) == batch_size


@pytest.mark.parametrize("batch_size", [0, 101])
def test_request_rejects_values_outside_verified_batch_boundaries(
    batch_size: int,
) -> None:
    with pytest.raises(ValidationError):
        PredictRequest.model_validate({"instances": [VALID_INSTANCE] * batch_size})


def test_response_restricts_predictions_to_public_class_names() -> None:
    response = PredictResponse(predictions=[class_name.value for class_name in IrisClassName])
    assert [prediction.value for prediction in response.predictions] == [
        class_name.value for class_name in IrisClassName
    ]

    with pytest.raises(ValidationError):
        PredictResponse(predictions=["unknown"])
