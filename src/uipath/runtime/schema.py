"""UiPath Runtime Schema Definitions."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

COMMON_MODEL_SCHEMA = ConfigDict(
    validate_by_name=True,
    validate_by_alias=True,
    use_enum_values=True,
    arbitrary_types_allowed=True,
    extra="allow",
)


class UiPathRuntimeSchema(BaseModel):
    """Represents the UiPath runtime schema."""

    file_path: str = Field(..., alias="filePath")
    unique_id: str = Field(..., alias="uniqueId")
    type: str = Field(..., alias="type")
    input: dict[str, Any] = Field(..., alias="input")
    output: dict[str, Any] = Field(..., alias="output")

    model_config = COMMON_MODEL_SCHEMA


__all__ = [
    "UiPathRuntimeSchema",
]
