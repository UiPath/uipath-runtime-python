"""UiPath Runtime Schema Definitions."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

COMMON_MODEL_SCHEMA = ConfigDict(
    validate_by_name=True,
    validate_by_alias=True,
    use_enum_values=True,
    arbitrary_types_allowed=True,
    extra="allow",
)


class UiPathRuntimeEntrypoint(BaseModel):
    """Represents an entrypoint in the UiPath runtime schema."""

    file_path: str = Field(..., alias="filePath")
    unique_id: str = Field(..., alias="uniqueId")
    type: str = Field(..., alias="type")
    input: Dict[str, Any] = Field(..., alias="input")
    output: Dict[str, Any] = Field(..., alias="output")

    model_config = COMMON_MODEL_SCHEMA


class UiPathRuntimeBindingValue(BaseModel):
    """Represents a binding value in the UiPath runtime schema."""

    default_value: str = Field(..., alias="defaultValue")
    is_expression: bool = Field(..., alias="isExpression")
    display_name: str = Field(..., alias="displayName")

    model_config = COMMON_MODEL_SCHEMA


# TODO: create stronger binding resource definition with discriminator based on resource enum.
class UiPathRuntimeBindingResource(BaseModel):
    """Represents a binding resource in the UiPath runtime schema."""

    resource: str = Field(..., alias="resource")
    key: str = Field(..., alias="key")
    value: dict[str, UiPathRuntimeBindingValue] = Field(..., alias="value")
    metadata: Any = Field(..., alias="metadata")

    model_config = COMMON_MODEL_SCHEMA


class UiPathRuntimeBindings(BaseModel):
    """Represents the bindings section in the UiPath runtime schema."""

    version: str = Field(..., alias="version")
    resources: List[UiPathRuntimeBindingResource] = Field(..., alias="resources")

    model_config = COMMON_MODEL_SCHEMA


class UiPathRuntimeInternalArguments(BaseModel):
    """Represents internal runtime arguments in the UiPath runtime schema."""

    resource_overwrites: dict[str, Any] = Field(..., alias="resourceOverwrites")

    model_config = COMMON_MODEL_SCHEMA


class UiPathRuntimeArguments(BaseModel):
    """Represents runtime arguments in the UiPath runtime schema."""

    internal_arguments: Optional[UiPathRuntimeInternalArguments] = Field(
        default=None, alias="internalArguments"
    )

    model_config = COMMON_MODEL_SCHEMA


class UiPathRuntimeSchema(BaseModel):
    """Represents the overall UiPath runtime schema."""

    runtime: Optional[UiPathRuntimeArguments] = Field(default=None, alias="runtime")
    entrypoints: List[UiPathRuntimeEntrypoint] = Field(..., alias="entryPoints")
    bindings: UiPathRuntimeBindings = Field(
        default=UiPathRuntimeBindings(version="2.0", resources=[]), alias="bindings"
    )
    settings: Optional[Dict[str, Any]] = Field(default=None, alias="setting")

    model_config = COMMON_MODEL_SCHEMA


__all__ = [
    "UiPathRuntimeSchema",
    "UiPathRuntimeEntrypoint",
    "UiPathRuntimeBindings",
    "UiPathRuntimeBindingResource",
    "UiPathRuntimeBindingValue",
    "UiPathRuntimeArguments",
    "UiPathRuntimeInternalArguments",
]
