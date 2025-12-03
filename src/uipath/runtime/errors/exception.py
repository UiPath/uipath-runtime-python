"""Base exception class for UiPath runtime errors with structured error information."""

import sys
import traceback
from typing import Any

from uipath.core.errors import UiPathFaultedTriggerError

from uipath.runtime.errors.codes import UiPathErrorCode
from uipath.runtime.errors.contract import UiPathErrorCategory, UiPathErrorContract


class UiPathBaseRuntimeError(Exception):
    """Base exception class for UiPath runtime errors with structured error information."""

    def __init__(
        self,
        code: str,
        title: str,
        detail: str,
        category: UiPathErrorCategory = UiPathErrorCategory.UNKNOWN,
        status: int | None = None,
        prefix: str = "Python",
        include_traceback: bool = True,
    ):
        """Initialize the UiPathBaseRuntimeError with structured error information."""
        # Get the current traceback as a string
        if include_traceback:
            tb = traceback.format_exc()
            if (
                tb and tb.strip() != "NoneType: None"
            ):  # Ensure there's an actual traceback
                detail = f"{detail}\n\n{tb}"

        if status is None:
            status = self._extract_http_status()

        self.error_info = UiPathErrorContract(
            code=f"{prefix}.{code}",
            title=title,
            detail=detail,
            category=category,
            status=status,
        )
        super().__init__(detail)

    def _extract_http_status(self) -> int | None:
        """Extract HTTP status code from the exception chain if present."""
        exc_info = sys.exc_info()
        if not exc_info or len(exc_info) < 2 or exc_info[1] is None:
            return None

        exc: BaseException | None = exc_info[1]  # Current exception being handled
        while exc is not None:
            if hasattr(exc, "status_code"):
                return exc.status_code

            if hasattr(exc, "response") and hasattr(exc.response, "status_code"):
                return exc.response.status_code

            # Move to the next exception in the chain
            next_exc = getattr(exc, "__cause__", None) or getattr(
                exc, "__context__", None
            )

            # Ensure next_exc is a BaseException or None
            exc = (
                next_exc
                if isinstance(next_exc, BaseException) or next_exc is None
                else None
            )

        return None

    @property
    def as_dict(self) -> dict[str, Any]:
        """Get the error information as a dictionary."""
        return self.error_info.model_dump()


class UiPathRuntimeError(UiPathBaseRuntimeError):
    """Exception class for UiPath runtime errors."""

    def __init__(
        self,
        code: UiPathErrorCode,
        title: str,
        detail: str,
        category: UiPathErrorCategory = UiPathErrorCategory.UNKNOWN,
        status: int | None = None,
        prefix: str = "Python",
        include_traceback: bool = True,
    ):
        """Initialize the UiPathRuntimeError with structured error information."""
        super().__init__(
            code=code.value,
            title=title,
            detail=detail,
            category=category,
            status=status,
            prefix=prefix,
            include_traceback=include_traceback,
        )

    @classmethod
    def from_resume_trigger_error(
        cls, exc: UiPathFaultedTriggerError
    ) -> "UiPathRuntimeError":
        """Create UiPathRuntimeError from UiPathFaultedTriggerError."""
        return cls(
            code=UiPathErrorCode.RESUME_TRIGGER_ERROR,
            title="Resume trigger error",
            detail=exc.message,
            category=UiPathErrorCategory(exc.category),
        )
