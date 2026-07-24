from __future__ import annotations

from typing import Protocol

from coworker.channels.base import ChannelCapabilities, ConnectionInfo
from coworker.channels.inbound import InboundEnvelope
from coworker.channels.stream.runtime import StreamRuntime
from coworker.core.types import CommunicateRequest, IncomingEvent, ToolResult


class StreamProfile(Protocol):
    """Protocol behavior layered on the shared stream transport."""

    name: str
    participant_prefix: str

    def capabilities_for(self, participant_id: str) -> ChannelCapabilities: ...

    async def send(
        self,
        request: CommunicateRequest,
        runtime: StreamRuntime,
    ) -> ToolResult: ...

    def normalize_inbound(
        self,
        envelope: InboundEnvelope,
        runtime: StreamRuntime,
    ) -> IncomingEvent | None: ...

    def list_connections(self, runtime: StreamRuntime) -> list[ConnectionInfo]: ...
