"""Standard error contract used across the runtime."""

from enum import Enum

from pydantic import BaseModel


class UiPathErrorCategory(str, Enum):
    """Categories of runtime errors."""

    DEPLOYMENT = "Deployment"  # Configuration, licensing, or permission issues
    SYSTEM = "System"  # Unexpected internal errors or infrastructure issues
    UNKNOWN = "Unknown"  # Default category when the error type is not specified
    USER = "User"  # Business logic or domain-level errors


class UiPathErrorContract(BaseModel):
    """Standard error contract used across the runtime."""

    code: str  # Human-readable code uniquely identifying this error type across the platform.
    # Format: <Component>.<PascalCaseErrorCode> (e.g. LangGraph.InvaliGraphReference)
    # Only use alphanumeric characters [A-Za-z0-9] and periods. No whitespace allowed.

    title: str  # Short, human-readable summary of the problem that should remain consistent
    # across occurrences.

    detail: (
        str  # Human-readable explanation specific to this occurrence of the problem.
    )
    # May include context, recommended actions, or technical details like call stacks
    # for technical users.

    category: UiPathErrorCategory = (
        UiPathErrorCategory.UNKNOWN
    )  # Classification of the error:
    # - User: Business logic or domain-level errors
    # - Deployment: Configuration, licensing, or permission issues
    # - System: Unexpected internal errors or infrastructure issues

    status: int | None = (
        None  # HTTP status code, if relevant (e.g., when forwarded from a web API)
    )
