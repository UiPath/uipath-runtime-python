from typing import Any

from pydantic import BaseModel

from uipath.runtime.schema import (
    transform_attachments,
    transform_nullable_types,
    transform_references,
)


class TestSchemaGenerationHelpers:
    def test_transform_attachments_simple(self):
        """Test transforming a simple schema with UiPathAttachment."""
        schema = {
            "type": "object",
            "properties": {
                "abc": {"type": "string"},
                "file": {
                    "title": "UiPathAttachment",
                    "type": "object",
                    "properties": {
                        "ID": {"type": "string"},
                        "FullName": {"type": "string"},
                    },
                },
            },
        }

        result = transform_attachments(schema)

        assert result["properties"]["file"] == {"$ref": "#/definitions/job-attachment"}

        # Check that abc is unchanged
        assert result["properties"]["abc"] == {"type": "string"}

        # Check that definitions were added
        assert "definitions" in result
        assert "job-attachment" in result["definitions"]

        # Check the job-attachment definition structure
        job_def = result["definitions"]["job-attachment"]
        assert job_def["type"] == "object"
        assert job_def["x-uipath-resource-kind"] == "JobAttachment"
        assert "ID" in job_def["required"]
        assert "ID" in job_def["properties"]
        assert "FullName" in job_def["properties"]
        assert "MimeType" in job_def["properties"]
        assert "Metadata" in job_def["properties"]

    def test_transform_attachments_nested(self):
        """Test transforming nested properties with UiPathAttachment."""
        schema = {
            "type": "object",
            "properties": {
                "data": {
                    "type": "object",
                    "properties": {
                        "attachment": {
                            "title": "UiPathAttachment",
                            "type": "object",
                            "properties": {},
                        }
                    },
                }
            },
        }

        result = transform_attachments(schema)

        # Check that the nested attachment was replaced
        assert result["properties"]["data"]["properties"]["attachment"] == {
            "$ref": "#/definitions/job-attachment"
        }

        # Check definitions
        assert "definitions" in result
        assert "job-attachment" in result["definitions"]

    def test_transform_attachments_multiple(self):
        """Test transforming multiple UiPathAttachment references."""
        schema = {
            "type": "object",
            "properties": {
                "file1": {
                    "title": "UiPathAttachment",
                    "type": "object",
                },
                "file2": {
                    "title": "UiPathAttachment",
                    "type": "object",
                },
            },
        }

        result = transform_attachments(schema)

        # Both should be replaced with $ref
        assert result["properties"]["file1"] == {"$ref": "#/definitions/job-attachment"}
        assert result["properties"]["file2"] == {"$ref": "#/definitions/job-attachment"}

        # Should only have one definition
        assert "definitions" in result
        assert "job-attachment" in result["definitions"]
        assert len(result["definitions"]) == 1

    def test_transform_attachments_in_array(self):
        """Test transforming UiPathAttachment inside array items."""
        schema = {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {
                        "title": "UiPathAttachment",
                        "type": "object",
                    },
                }
            },
        }

        result = transform_attachments(schema)

        # Array items should be replaced
        assert result["properties"]["files"]["items"] == {
            "$ref": "#/definitions/job-attachment"
        }

        # Check definitions
        assert "definitions" in result
        assert "job-attachment" in result["definitions"]

    def test_transform_attachments_no_attachments(self):
        """Test transforming schema without any UiPathAttachment."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
        }

        result = transform_attachments(schema)

        # Schema should be unchanged (except for deep copy)
        assert result["properties"]["name"] == {"type": "string"}
        assert result["properties"]["age"] == {"type": "integer"}

        # No definitions should be added
        assert "definitions" not in result

    def test_transform_attachments_preserves_existing_definitions(self):
        """Test that existing definitions are preserved."""
        schema = {
            "type": "object",
            "properties": {
                "file": {
                    "title": "UiPathAttachment",
                    "type": "object",
                }
            },
            "definitions": {
                "custom-type": {
                    "type": "string",
                    "enum": ["a", "b"],
                }
            },
        }

        result = transform_attachments(schema)

        # Check that both definitions exist
        assert "definitions" in result
        assert "job-attachment" in result["definitions"]
        assert "custom-type" in result["definitions"]

        # Check that custom-type is unchanged
        assert result["definitions"]["custom-type"] == {
            "type": "string",
            "enum": ["a", "b"],
        }

    def test_transform_attachments_immutable(self):
        """Test that the original schema is not modified."""

        schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "file": {
                    "title": "UiPathAttachment",
                    "type": "object",
                }
            },
        }

        original_properties = schema["properties"]["file"].copy()
        result = transform_attachments(schema)

        # Original should be unchanged
        assert schema["properties"]["file"] == original_properties
        assert "definitions" not in schema

        # Result should be transformed
        assert result["properties"]["file"] == {"$ref": "#/definitions/job-attachment"}
        assert "definitions" in result

    def test_transform_attachments_only_with_title_and_type(self):
        """Test that only objects with both title and type=object are transformed."""
        schema = {
            "type": "object",
            "properties": {
                "only_title": {
                    "title": "UiPathAttachment",
                    # Missing type: "object"
                },
                "only_type": {
                    "type": "object",
                    # Missing title
                },
                "wrong_title": {
                    "title": "SomethingElse",
                    "type": "object",
                },
                "correct": {
                    "title": "UiPathAttachment",
                    "type": "object",
                },
            },
        }

        result = transform_attachments(schema)

        # Only the correct one should be transformed
        assert "only_title" in result["properties"]
        assert result["properties"]["only_title"].get("$ref") is None

        assert "only_type" in result["properties"]
        assert result["properties"]["only_type"].get("$ref") is None

        assert "wrong_title" in result["properties"]
        assert result["properties"]["wrong_title"].get("$ref") is None

        assert result["properties"]["correct"] == {
            "$ref": "#/definitions/job-attachment"
        }

        # Should have definitions
        assert "definitions" in result
        assert "job-attachment" in result["definitions"]

    def test_transform_references_and_transform_nullable_types(self):
        """Test that transform_references and transform_nullable_types work correctly together."""

        class InnerModel(BaseModel):
            inner_model_property: str

        class TestModel(BaseModel):
            test_model_property: str | None = None
            inner_model_instance: InnerModel

        resolved_schema, _ = transform_references(TestModel.model_json_schema())
        result = transform_nullable_types(resolved_schema)

        expected = {
            "$defs": {
                "InnerModel": {
                    "properties": {
                        "inner_model_property": {
                            "title": "Inner Model Property",
                            "type": "string",
                        }
                    },
                    "required": ["inner_model_property"],
                    "title": "InnerModel",
                    "type": "object",
                }
            },
            "properties": {
                "test_model_property": {"type": "string"},
                "inner_model_instance": {
                    "properties": {
                        "inner_model_property": {
                            "title": "Inner Model Property",
                            "type": "string",
                        }
                    },
                    "required": ["inner_model_property"],
                    "title": "InnerModel",
                    "type": "object",
                },
            },
            "required": ["inner_model_instance"],
            "title": "TestModel",
            "type": "object",
        }

        assert result == expected
