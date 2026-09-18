from enum import Enum


class IrisClassName(str, Enum):
    SETOSA = "setosa"
    VERSICOLOR = "versicolor"
    VIRGINICA = "virginica"


VERIFIED_FEATURE_NAMES = (
    "sepal_length",
    "sepal_width",
    "petal_length",
    "petal_width",
)
VERIFIED_TARGET_NAMES = tuple(class_name.value for class_name in IrisClassName)

