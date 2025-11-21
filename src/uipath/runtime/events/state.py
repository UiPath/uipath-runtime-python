"""Events related to agent messages and state updates."""

from typing import Any

from pydantic import Field

from uipath.runtime.events.base import UiPathRuntimeEvent, UiPathRuntimeEventType


class UiPathRuntimeMessageEvent(UiPathRuntimeEvent):
    """Event emitted when a message is created or streamed.

    Wraps framework-specific message objects (e.g., LangChain BaseMessage,
    CrewAI messages, AutoGen messages, etc.) without converting them.

    Attributes:
        payload: The framework-specific message object
        event_type: Automatically set to AGENT_MESSAGE
        metadata: Additional context

    Example:
        # LangChain
        event = UiPathRuntimeMessageEvent(
            payload=AIMessage(content="Hello"),
            metadata={"additional_prop": "123"}
        )

        # Access the message
        message = event.payload  # BaseMessage
        print(message.content)
    """

    payload: Any = Field(description="Framework-specific message object")
    event_type: UiPathRuntimeEventType = Field(
        default=UiPathRuntimeEventType.RUNTIME_MESSAGE, frozen=True
    )


class UiPathRuntimeStateEvent(UiPathRuntimeEvent):
    """Event emitted when agent state is updated.

    Wraps framework-specific state update objects, preserving the original
    structure and data from the framework.

    Attributes:
        payload: The framework-specific state update (e.g., LangGraph state dict)
        node_name: Name of the node/agent that produced this update (if available)
        event_type: Automatically set to RUNTIME_STATE
        metadata: Additional context

    Example:
        # LangGraph
        event = UiPathRuntimeStateEvent(
            payload={"messages": [...], "context": "..."},
            node_name="agent_node",
            metadata={"additional_prop": "123"}
        )

        # Access the state
        state = event.payload  # dict
        messages = state.get("messages", [])
    """

    payload: dict[str, Any] = Field(description="Framework-specific state update")
    node_name: str | None = Field(
        default=None, description="Name of the node/agent that caused this update"
    )
    event_type: UiPathRuntimeEventType = Field(
        default=UiPathRuntimeEventType.RUNTIME_STATE, frozen=True
    )


__all__ = ["UiPathRuntimeMessageEvent", "UiPathRuntimeStateEvent"]
