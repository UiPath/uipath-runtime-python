"""UiPath Runtime Schema Definitions."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

COMMON_MODEL_SCHEMA = ConfigDict(
    validate_by_name=True,
    validate_by_alias=True,
    use_enum_values=True,
    arbitrary_types_allowed=True,
    extra="allow",
)


class UiPathRuntimeNode(BaseModel):
    """Represents a node in the runtime graph."""

    id: str = Field(..., description="Unique node identifier")
    name: str = Field(..., description="Display name of the node")
    type: str = Field(..., description="Node type (e.g., 'tool', 'model')")
    subgraph: UiPathRuntimeGraph | None = Field(
        None, description="Nested subgraph if this node contains one"
    )
    metadata: dict[str, Any] | None = Field(
        None, description="Additional node metadata (e.g., model config, tool names)"
    )

    model_config = COMMON_MODEL_SCHEMA


class UiPathRuntimeEdge(BaseModel):
    """Represents an edge/connection in the runtime graph."""

    source: str = Field(..., description="Source node")
    target: str = Field(..., description="Target node")
    label: str | None = Field(None, description="Edge label or condition")

    model_config = COMMON_MODEL_SCHEMA


class UiPathRuntimeGraph(BaseModel):
    """Represents the runtime structure as a graph."""

    nodes: list[UiPathRuntimeNode] = Field(default_factory=list)
    edges: list[UiPathRuntimeEdge] = Field(default_factory=list)

    model_config = COMMON_MODEL_SCHEMA


class UiPathRuntimeSchema(BaseModel):
    """Represents the UiPath runtime schema."""

    file_path: str = Field(..., alias="filePath")
    unique_id: str = Field(..., alias="uniqueId")
    type: str = Field(..., alias="type")
    input: dict[str, Any] = Field(..., alias="input")
    output: dict[str, Any] = Field(..., alias="output")
    graph: UiPathRuntimeGraph | None = Field(
        None, description="Runtime graph structure for debugging"
    )

    model_config = COMMON_MODEL_SCHEMA


__all__ = [
    "UiPathRuntimeSchema",
    "UiPathRuntimeGraph",
    "UiPathRuntimeNode",
    "UiPathRuntimeEdge",
]
