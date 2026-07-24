"""Generic WS/SSE protocol channel."""

from __future__ import annotations

import json
from typing import Any, cast

from coworker.channels.base import BaseChannel, ChannelCapabilities, ConnectionInfo
from coworker.channels.inbound import InboundEnvelope
from coworker.channels.stream.profile import StreamProfile
from coworker.channels.stream.runtime import StreamRuntime
from coworker.core.types import CommunicateRequest, IncomingEvent, ToolResult


class StreamChannel(BaseChannel):
    """Normalize stream messages while delegating state to ``StreamRuntime``."""

    name = "stream"
    participant_prefix = ""

    def __init__(self, runtime: StreamRuntime) -> None:
        super().__init__(runtime=runtime)
        self._profiles: list[StreamProfile] = []

    @property
    def runtime(self) -> StreamRuntime:
        return cast(StreamRuntime, self._runtime)

    def resolve(self, participant_id: str) -> str | None:
        return None

    def register_profile(self, profile: StreamProfile) -> None:
        if not isinstance(profile.name, str):
            raise TypeError("stream profile name must be a string")
        if not profile.name.strip():
            raise ValueError("stream profile name is required")
        if not isinstance(profile.participant_prefix, str):
            raise TypeError("stream profile participant_prefix must be a string")
        if not profile.participant_prefix:
            raise ValueError("stream profile participant_prefix is required")
        if profile in self._profiles:
            raise ValueError(f"stream profile already registered: {profile.name}")
        if any(existing.name == profile.name for existing in self._profiles):
            raise ValueError(f"stream profile name already registered: {profile.name}")
        if any(
            existing.participant_prefix == profile.participant_prefix
            for existing in self._profiles
        ):
            raise ValueError(
                "stream profile participant prefix already registered: "
                f"{profile.participant_prefix!r}"
            )
        self._profiles.append(profile)

    async def receive_raw(self, envelope: InboundEnvelope) -> None:
        self.record_received(envelope.participant_id)
        profile = self._profile_for(envelope.participant_id)
        if profile is not None:
            event = profile.normalize_inbound(envelope, self.runtime)
            if event is not None:
                await self.publish_inbound(event)
            return
        content, conversation_id, raw_attachments = self._parse_generic_inbound(envelope)
        attachments = [
            self.runtime.save_attachment(
                item,
                keep_inline_data=True,
            )
            for item in raw_attachments
        ]
        await self.publish_inbound(
            IncomingEvent(
                participant_id=envelope.participant_id,
                content=content,
                conversation_id=conversation_id,
                source=envelope.source,
                attachments=attachments,
            )
        )

    def capabilities_for(self, participant_id: str) -> ChannelCapabilities:
        profile = self._profile_for(participant_id)
        if profile is not None:
            return profile.capabilities_for(participant_id)
        if self.runtime.supports_message_extra(participant_id):
            return ChannelCapabilities(conversation_id=True, attachments=True, extra=True)
        return ChannelCapabilities()

    async def send(self, request: CommunicateRequest) -> ToolResult:
        profile = self._profile_for(request.participant_id)
        if profile is not None:
            return await profile.send(request, self.runtime)
        return await self.runtime.send(request)

    def list_connections(self) -> list[ConnectionInfo]:
        connections = [
            connection
            for connection in self.runtime.list_connections()
            if self._profile_for(connection.participant_id) is None
        ]
        for profile in self._profiles:
            connections.extend(profile.list_connections(self.runtime))
        return connections

    def record_received(self, participant_id: str) -> None:
        self.runtime.record_received(participant_id)

    @staticmethod
    def _parse_generic_inbound(
        envelope: InboundEnvelope,
    ) -> tuple[str, str | None, list[dict[str, Any]]]:
        raw = envelope.payload
        if envelope.source == "websocket":
            text = str(raw.get("text") or "") if isinstance(raw, dict) else str(raw)
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return text, None, []
            if not isinstance(parsed, dict) or not any(
                key in parsed for key in ("message", "conversation_id", "attachments")
            ):
                return text, None, []
            payload = parsed
            content = str(payload.get("message") or "")
        else:
            payload = raw if isinstance(raw, dict) else {}
            content = str(payload.get("content") or "")

        conversation = payload.get("conversation_id")
        conversation_id = conversation if isinstance(conversation, str) else None
        raw_attachments = [
            item for item in payload.get("attachments", []) if isinstance(item, dict)
        ]
        return content, conversation_id, raw_attachments

    def _profile_for(self, participant_id: str) -> StreamProfile | None:
        matched: StreamProfile | None = None
        for profile in self._profiles:
            if participant_id.startswith(profile.participant_prefix) and (
                matched is None
                or len(profile.participant_prefix) > len(matched.participant_prefix)
            ):
                matched = profile
        return matched
