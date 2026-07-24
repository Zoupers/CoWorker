"""Channel protocol and lightweight inline implementation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from coworker.channels.inbound import InboundEnvelope
from coworker.channels.runtime import DEFAULT_RUNTIME, ChannelRuntime
from coworker.core.types import CommunicateRequest, IncomingEvent, ToolResult

InboundHandler = Callable[[IncomingEvent], Awaitable[Any]]


@dataclass(frozen=True)
class ConnectionInfo:
    """A reachable communication participant on some channel."""

    participant_id: str
    channel: str  # "stream" / "wecom" / "desktop"
    kind: str  # "websocket" / "sse" / "wecom:single" / "wecom:group" / "desktop:actor"
    display_name: str = ""
    active: bool = False  # online now (stream WS/SSE) vs known-reachable (wecom/desktop)
    last_sent_at: str | None = None
    last_received_at: str | None = None


class ParticipantIdResolutionError(ValueError):
    """Raised when a shorthand participant ID cannot be resolved unambiguously."""


class BaseChannel(ABC):
    """Default implementation for the non-transport parts of a Channel."""

    name = ""
    participant_prefix = ""

    def __init__(
        self,
        *,
        runtime: ChannelRuntime | None = None,
        supports_extra: bool = False,
    ) -> None:
        self._runtime = runtime or DEFAULT_RUNTIME
        self._supports_extra = supports_extra
        self._last_sent_at: dict[str, str] = {}
        self._last_received_at: dict[str, str] = {}
        self._inbound_handler: InboundHandler | None = None

    @property
    def runtime(self) -> ChannelRuntime:
        return self._runtime

    def resolve(self, participant_id: str) -> str | None:
        return None

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        self._inbound_handler = handler

    async def publish_inbound(self, event: IncomingEvent) -> None:
        if self._inbound_handler is None:
            raise RuntimeError(f"channel {self.name} has no inbound handler")
        await self._inbound_handler(event)

    async def receive_raw(self, envelope: InboundEnvelope) -> None:
        raise NotImplementedError(f"channel {self.name} does not accept raw inbound payloads")

    @abstractmethod
    async def send(self, request: CommunicateRequest) -> ToolResult:
        """Deliver a request to this channel."""

    def record_received(self, participant_id: str) -> None:
        self._last_received_at[participant_id] = _activity_timestamp()

    def activity_for(self, participant_id: str) -> tuple[str | None, str | None]:
        return self._last_sent_at.get(participant_id), self._last_received_at.get(participant_id)

    def supports_extra_for(self, participant_id: str) -> bool:
        return self._supports_extra

    def list_connections(self) -> list[ConnectionInfo]:
        return []

    def _record_sent(self, participant_id: str) -> None:
        self._last_sent_at[participant_id] = _activity_timestamp()


class InlineChannel(BaseChannel):
    """Minimal :class:`Channel` wrapping a sender callable.

    The simplest way to register a channel: a prefix, an async sender, an
    optional shorthand resolver, and a static ``supports_extra`` flag. Real channels
    (desktop, wecom, stream) provide richer ``list_connections`` / lifecycle,
    but anything that just routes by prefix and sends can use this.
    """

    def __init__(
        self,
        prefix: str,
        sender: Callable[[CommunicateRequest], Awaitable[ToolResult]],
        resolver: Callable[[str], str | None] | None = None,
        *,
        supports_extra: bool = False,
        name: str | None = None,
        runtime: ChannelRuntime | None = None,
    ) -> None:
        super().__init__(runtime=runtime, supports_extra=supports_extra)
        self.name = name or prefix.rstrip(":") or "inline"
        self.participant_prefix = prefix
        self._sender = sender
        self._resolver = resolver

    def resolve(self, participant_id: str) -> str | None:
        return self._resolver(participant_id) if self._resolver is not None else None

    async def send(self, request: CommunicateRequest) -> ToolResult:
        result = await self._sender(request)
        if not result.is_error:
            self._record_sent(request.participant_id)
        return result

@runtime_checkable
class Channel(Protocol):
    """One communication transport.

    Subclass :class:`BaseChannel` for default inbound, capability, connection,
    and activity behavior. ``participant_prefix`` routes full IDs; an empty
    prefix identifies the fallback channel. ``resolve`` canonicalizes shorthand
    IDs and returns ``None`` when the channel does not claim one.
    """

    name: str
    participant_prefix: str

    @property
    def runtime(self) -> ChannelRuntime:
        """Stateful runtime used by this channel profile."""
        ...

    def resolve(self, participant_id: str) -> str | None:
        """Canonicalize a shorthand participant_id, or None if not claimed."""
        ...

    async def send(self, request: CommunicateRequest) -> ToolResult:
        """Deliver a request to this channel's participant."""
        ...

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        """Attach the host-owned handler for normalized inbound events."""
        ...

    async def receive_raw(self, envelope: InboundEnvelope) -> None:
        """Normalize a raw protocol envelope and publish it through this channel."""
        ...

    def supports_extra_for(self, participant_id: str) -> bool:
        """Whether this channel accepts structured ``extra`` for the participant.

        Static for prefix-routed channels (desktop yes, wecom no); per-participant
        for the stream fallback (only live WS/SSE connections accept extra).
        """
        ...

    def list_connections(self) -> list[ConnectionInfo]:
        """Reachable participants on this channel."""
        ...

    def record_received(self, participant_id: str) -> None:
        """Record an inbound message for a participant."""
        ...

def _activity_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
