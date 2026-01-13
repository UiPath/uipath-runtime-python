"""UiPath Runtime Schema Definitions."""

from __future__ import annotations

import copy
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
    metadata: dict[str, Any] | None = Field(
        None, description="Additional metadata for the runtime schema"
    )

    model_config = COMMON_MODEL_SCHEMA


def _get_job_attachment_definition() -> dict[str, Any]:
    """Get the job-attachment definition schema for UiPath attachments.

    Returns:
        The JSON schema definition for a UiPath job attachment.
    """
    return {
        "type": "object",
        "required": ["ID"],
        "x-uipath-resource-kind": "JobAttachment",
        "properties": {
            "ID": {"type": "string"},
            "FullName": {"type": "string"},
            "MimeType": {"type": "string"},
            "Metadata": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
        },
    }


def transform_attachments(schema: dict[str, Any]) -> dict[str, Any]:
    """Transform UiPathAttachment references in a JSON schema to use $ref.

    This function recursively traverses a JSON schema and replaces any objects
    with title="UiPathAttachment" with a $ref to "#/definitions/job-attachment",
    adding the job-attachment definition to the schema's definitions section.

    Args:
        schema: The JSON schema to transform (will not be modified in-place).

    Returns:
        A new schema with UiPathAttachment references replaced by $ref.

    Example:
        >>> schema = {
        ...     "type": "object",
        ...     "properties": {
        ...         "file": {
        ...             "title": "UiPathAttachment",
        ...             "type": "object",
        ...             "properties": {...}
        ...         }
        ...     }
        ... }
        >>> result = transform_attachments(schema)
        >>> result["properties"]["file"]
        {"$ref": "#/definitions/job-attachment"}
        >>> "job-attachment" in result["definitions"]
        True
    """
    result = copy.deepcopy(schema)
    has_attachments = False

    def transform_recursive(obj: Any) -> Any:
        """Recursively transform the schema object."""
        nonlocal has_attachments

        if isinstance(obj, dict):
            if obj.get("title") == "UiPathAttachment" and obj.get("type") == "object":
                has_attachments = True
                return {"$ref": "#/definitions/job-attachment"}

            return {key: transform_recursive(value) for key, value in obj.items()}

        elif isinstance(obj, list):
            return [transform_recursive(item) for item in obj]

        else:
            # Return primitive values as-is
            return obj

    result = transform_recursive(result)

    # add the job-attachment definition if any are present
    if has_attachments:
        if "definitions" not in result:
            result["definitions"] = {}
        result["definitions"]["job-attachment"] = _get_job_attachment_definition()

    return result


def transform_references(schema, root=None, visited=None):
    """Recursively resolves $ref references in a JSON schema, handling circular references.

    Returns:
        tuple: (resolved_schema, has_circular_dependency)
    """
    if root is None:
        root = schema

    if visited is None:
        visited = set()

    has_circular = False

    if isinstance(schema, dict):
        if "$ref" in schema:
            ref_path = schema["$ref"]

            if ref_path in visited:
                # Circular dependency detected
                return {
                    "type": "object",
                    "description": f"Circular reference to {ref_path}",
                }, True

            visited.add(ref_path)

            # Resolve the reference
            ref_parts = ref_path.lstrip("#/").split("/")
            ref_schema = root
            for part in ref_parts:
                ref_schema = ref_schema.get(part, {})

            result, circular = transform_references(ref_schema, root, visited)
            has_circular = has_circular or circular

            # Remove from visited after resolution (allows the same ref in different branches)
            visited.discard(ref_path)

            return result, has_circular

        resolved_dict = {}
        for k, v in schema.items():
            resolved_value, circular = transform_references(v, root, visited)
            resolved_dict[k] = resolved_value
            has_circular = has_circular or circular
        return resolved_dict, has_circular

    elif isinstance(schema, list):
        resolved_list = []
        for item in schema:
            resolved_item, circular = transform_references(item, root, visited)
            resolved_list.append(resolved_item)
            has_circular = has_circular or circular
        return resolved_list, has_circular

    return schema, False


def transform_nullable_types(
    schema: dict[str, Any] | list[Any] | Any,
) -> dict[str, Any] | list[Any]:
    """Process the schema to handle nullable types by removing anyOf with null and keeping the base type."""
    if isinstance(schema, dict):
        if "anyOf" in schema and len(schema["anyOf"]) == 2:
            types = [t.get("type") for t in schema["anyOf"]]
            if "null" in types:
                non_null_type = next(
                    t for t in schema["anyOf"] if t.get("type") != "null"
                )
                return non_null_type

        return {k: transform_nullable_types(v) for k, v in schema.items()}
    elif isinstance(schema, list):
        return [transform_nullable_types(item) for item in schema]
    return schema


__all__ = [
    "UiPathRuntimeSchema",
    "UiPathRuntimeGraph",
    "UiPathRuntimeNode",
    "UiPathRuntimeEdge",
    "transform_nullable_types",
    "transform_references",
    "transform_attachments",
]
