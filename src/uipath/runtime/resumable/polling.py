"""Trigger polling utilities for resumable runtime."""

import asyncio
import logging
from typing import Any, Callable, Coroutine

from uipath.core.errors import UiPathPendingTriggerError

from uipath.runtime.resumable.protocols import UiPathResumeTriggerReaderProtocol
from uipath.runtime.resumable.trigger import UiPathResumeTrigger

logger = logging.getLogger(__name__)


class TriggerPoller:
    """Utility for polling resume triggers until completion.

    This class provides reusable polling logic for waiting on triggers
    to complete, used by both debug runtime and resumable runtime when
    wait_for_triggers is enabled.
    """

    def __init__(
        self,
        reader: UiPathResumeTriggerReaderProtocol,
        poll_interval: float = 5.0,
        on_poll_attempt: Callable[[int, str | None], Coroutine[Any, Any, None]]
        | None = None,
        should_stop: Callable[[], Coroutine[Any, Any, bool]] | None = None,
    ):
        """Initialize the trigger poller.

        Args:
            reader: The trigger reader to use for polling
            poll_interval: Seconds between poll attempts
            on_poll_attempt: Optional callback for each poll attempt (attempt_num, info)
            should_stop: Optional async callback to check if polling should stop early
        """
        self.reader = reader
        self.poll_interval = poll_interval
        self.on_poll_attempt = on_poll_attempt
        self.should_stop = should_stop

    async def poll_trigger(self, trigger: UiPathResumeTrigger) -> Any | None:
        """Poll a single trigger until data is available.

        Args:
            trigger: The trigger to poll

        Returns:
            Resume data when available, or None if polling was stopped

        Raises:
            Exception: If trigger reading fails with non-pending error
        """
        attempt = 0
        while True:
            attempt += 1

            # Check if we should stop
            if self.should_stop and await self.should_stop():
                logger.debug("Polling stopped by should_stop callback")
                return None

            try:
                resume_data = await self.reader.read_trigger(trigger)

                if resume_data is not None:
                    logger.debug(
                        f"Trigger {trigger.interrupt_id} completed after {attempt} attempts"
                    )
                    return resume_data

                # Notify about poll attempt
                if self.on_poll_attempt:
                    await self.on_poll_attempt(attempt, None)

                await asyncio.sleep(self.poll_interval)

            except UiPathPendingTriggerError as e:
                # Trigger still pending, notify and continue polling
                if self.on_poll_attempt:
                    await self.on_poll_attempt(attempt, str(e))

                await asyncio.sleep(self.poll_interval)

    async def poll_all_triggers(
        self, triggers: list[UiPathResumeTrigger]
    ) -> dict[str, Any]:
        """Poll all triggers until they complete.

        Args:
            triggers: List of triggers to poll

        Returns:
            Dict mapping interrupt_id to resume data for completed triggers
        """
        resume_map: dict[str, Any] = {}

        # Poll triggers concurrently
        async def poll_single(trigger: UiPathResumeTrigger) -> tuple[str | None, Any]:
            data = await self.poll_trigger(trigger)
            return trigger.interrupt_id, data

        results = await asyncio.gather(
            *[poll_single(trigger) for trigger in triggers],
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Trigger polling failed: {result}")
                raise result
            interrupt_id, data = result
            if interrupt_id and data is not None:
                resume_map[interrupt_id] = data

        return resume_map
