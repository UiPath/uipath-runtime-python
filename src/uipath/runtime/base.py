"""Base runtime class and async context manager implementation."""

import json
import logging
import os
from abc import ABC, abstractmethod
from typing import AsyncGenerator

from uipath.runtime.context import UiPathRuntimeContext
from uipath.runtime.errors import (
    UiPathErrorCategory,
    UiPathErrorCode,
    UiPathErrorContract,
    UiPathRuntimeError,
)
from uipath.runtime.events import (
    UiPathRuntimeEvent,
)
from uipath.runtime.logging._interceptor import UiPathRuntimeLogsInterceptor
from uipath.runtime.result import UiPathRuntimeResult, UiPathRuntimeStatus
from uipath.runtime.schema import (
    UiPathRuntimeSchema,
)

logger = logging.getLogger(__name__)


class UiPathRuntimeStreamNotSupportedError(NotImplementedError):
    """Raised when a runtime does not support streaming."""

    pass


class UiPathBaseRuntime(ABC):
    """Base runtime class implementing the async context manager protocol.

    This allows using the class with 'async with' statements.
    """

    def __init__(self, context: UiPathRuntimeContext):
        """Initialize the runtime with the provided context."""
        self.context = context

    async def get_schema(self) -> UiPathRuntimeSchema:
        """Get schema for this runtime.

        Returns: The runtime's schema (entrypoint type, input/output json schema).
        """
        raise NotImplementedError()

    async def __aenter__(self):
        """Async enter method called when entering the 'async with' block.

        Initializes and prepares the runtime environment.

        Returns:
            The runtime instance
        """
        # Read the input from file if provided
        if self.context.input_file:
            _, file_extension = os.path.splitext(self.context.input_file)
            if file_extension != ".json":
                raise UiPathRuntimeError(
                    code=UiPathErrorCode.INVALID_INPUT_FILE_EXTENSION,
                    title="Invalid Input File Extension",
                    detail="The provided input file must be in JSON format.",
                )
            with open(self.context.input_file) as f:
                self.context.input = f.read()

        try:
            if isinstance(self.context.input, str):
                if self.context.input.strip():
                    self.context.input = json.loads(self.context.input)
                else:
                    self.context.input = {}
            elif self.context.input is None:
                self.context.input = {}
            # else: leave it as-is (already a dict, list, bool, etc.)
        except json.JSONDecodeError as e:
            raise UiPathRuntimeError(
                UiPathErrorCode.INPUT_INVALID_JSON,
                "Invalid JSON input",
                f"The input data is not valid JSON: {str(e)}",
                UiPathErrorCategory.USER,
            ) from e

        await self.validate()

        # Intercept all stdout/stderr/logs
        # Write to file (runtime), stdout (debug) or log handler (if provided)
        self.logs_interceptor = UiPathRuntimeLogsInterceptor(
            min_level=self.context.logs_min_level,
            dir=self.context.runtime_dir,
            file=self.context.logs_file,
            job_id=self.context.job_id,
            execution_id=self.context.execution_id,
            log_handler=self.context.log_handler,
        )
        self.logs_interceptor.setup()

        return self

    @abstractmethod
    async def execute(self) -> UiPathRuntimeResult:
        """Execute with the provided context.

        Returns:
            Dictionary with execution results

        Raises:
            RuntimeError: If execution fails
        """
        pass

    async def stream(
        self,
    ) -> AsyncGenerator[UiPathRuntimeEvent, None]:
        """Stream execution events in real-time.

        This is an optional method that runtimes can implement to support streaming.
        If not implemented, only the execute() method will be available.

        Yields framework-agnostic BaseEvent instances during execution,
        with the final event being UiPathRuntimeResult.

        Yields:
            UiPathRuntimeEvent subclasses: Framework-agnostic events (UiPathRuntimeMessageEvent,
                                  UiPathRuntimeStateEvent, etc.)
            Final yield: UiPathRuntimeResult (or its subclass UiPathBreakpointResult)

        Raises:
            UiPathRuntimeStreamNotSupportedError: If the runtime doesn't support streaming
            RuntimeError: If execution fails

        Example:
            async for event in runtime.stream():
                if isinstance(event, UiPathRuntimeResult):
                    # Last event - execution complete
                    print(f"Status: {event.status}")
                    break
                elif isinstance(event, UiPathRuntimeMessageEvent):
                    # Handle message event
                    print(f"Message: {event.payload}")
                elif isinstance(event, UiPathRuntimeStateEvent):
                    # Handle state update
                    print(f"State updated by: {event.node_name}")
        """
        raise UiPathRuntimeStreamNotSupportedError(
            f"{self.__class__.__name__} does not implement streaming. "
            "Use execute() instead."
        )
        # This yield is unreachable but makes this a proper generator function
        # Without it, the function wouldn't match the AsyncGenerator return type
        yield

    @abstractmethod
    async def validate(self):
        """Validate runtime inputs."""
        pass

    @abstractmethod
    async def cleanup(self):
        """Cleaup runtime resources."""
        pass

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async exit method called when exiting the 'async with' block.

        Cleans up resources and handles any exceptions.

        Always writes output file regardless of whether execution was successful,
        suspended, or encountered an error.
        """
        try:
            if self.context.result is None:
                execution_result = UiPathRuntimeResult()
            else:
                execution_result = self.context.result

            if exc_type:
                # Create error info from exception
                if isinstance(exc_val, UiPathRuntimeError):
                    error_info = exc_val.error_info
                else:
                    # Generic error
                    error_info = UiPathErrorContract(
                        code=f"ERROR_{exc_type.__name__}",
                        title=f"Runtime error: {exc_type.__name__}",
                        detail=str(exc_val),
                        category=UiPathErrorCategory.UNKNOWN,
                    )

                execution_result.status = UiPathRuntimeStatus.FAULTED
                execution_result.error = error_info

            content = execution_result.to_dict()

            # Always write output file at runtime, except for inner runtimes
            # Inner runtimes have execution_id
            if self.context.job_id and not self.context.execution_id:
                with open(self.context.result_file_path, "w") as f:
                    json.dump(content, f, indent=2, default=str)

            # Write the execution output to file if requested
            if self.context.output_file:
                with open(self.context.output_file, "w") as f:
                    f.write(content.get("output", "{}"))

            # Don't suppress exceptions
            return False

        except Exception as e:
            logger.error(f"Error during runtime shutdown: {str(e)}")

            # Create a fallback error result if we fail during cleanup
            if not isinstance(e, UiPathRuntimeError):
                error_info = UiPathErrorContract(
                    code="RUNTIME_SHUTDOWN_ERROR",
                    title="Runtime shutdown failed",
                    detail=f"Error: {str(e)}",
                    category=UiPathErrorCategory.SYSTEM,
                )
            else:
                error_info = e.error_info

            # Last-ditch effort to write error output
            try:
                error_result = UiPathRuntimeResult(
                    status=UiPathRuntimeStatus.FAULTED, error=error_info
                )
                error_result_content = error_result.to_dict()
                if self.context.job_id:
                    with open(self.context.result_file_path, "w") as f:
                        json.dump(error_result_content, f, indent=2, default=str)
            except Exception as write_error:
                logger.error(f"Failed to write error output file: {str(write_error)}")
                raise

            # Re-raise as RuntimeError if it's not already a UiPathRuntimeError
            if not isinstance(e, UiPathRuntimeError):
                raise RuntimeError(
                    error_info.code,
                    error_info.title,
                    error_info.detail,
                    error_info.category,
                ) from e
            raise
        finally:
            # Restore original logging
            if hasattr(self, "logs_interceptor"):
                self.logs_interceptor.teardown()

            await self.cleanup()
