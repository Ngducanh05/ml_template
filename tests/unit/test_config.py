from pathlib import Path

import pytest

from model_service.core.config import ConfigurationError, Settings


def test_settings_load_required_path_and_defaults() -> None:
    settings = Settings.from_env({"MODEL_PATH": "artifacts/model.joblib"})

    assert settings.model_path == Path("artifacts/model.joblib")
    assert settings.model_name == "iris-demo-v1"
    assert settings.log_level == "INFO"


def test_settings_normalize_explicit_values() -> None:
    settings = Settings.from_env(
        {
            "MODEL_PATH": " /models/model.joblib ",
            "MODEL_NAME": " iris-production ",
            "LOG_LEVEL": " warning ",
        }
    )

    assert settings.model_path == Path("/models/model.joblib")
    assert settings.model_name == "iris-production"
    assert settings.log_level == "WARNING"


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"MODEL_PATH": "   "},
        {"MODEL_PATH": "model.joblib", "MODEL_NAME": "   "},
        {"MODEL_PATH": "model.joblib", "LOG_LEVEL": "verbose"},
    ],
)
def test_settings_reject_invalid_environment(environ: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError):
        Settings.from_env(environ)
