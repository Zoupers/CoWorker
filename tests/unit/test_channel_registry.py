"""Unit tests for channel routing and shared runtime orchestration."""

from __future__ import annotations

import pytest

from coworker.channels.base import (
    BaseChannel,
    ChannelCapabilities,
    ConnectionInfo,
    ParticipantIdResolutionError,
)
from coworker.channels.registry import ChannelRegistry
from coworker.core.types import CommunicateRequest, ToolResult


class _FakeChannel:
    def __init__(
        self,
        name: str,
        prefix: str,
        *,
        supports_extra: bool = False,
        resolver=None,
        live: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.participant_prefix = prefix
        self.runtime = self
        self._supports_extra = supports_extra
        self._resolver = resolver or (lambda participant_id: None)
        self._live = set(live)
        self.sent: list[CommunicateRequest] = []
        self.started = False
        self.stopped = False

    def set_inbound_handler(self, handler) -> None:
        self.inbound_handler = handler

    def resolve(self, participant_id: str) -> str | None:
        return self._resolver(participant_id)

    async def send(self, request: CommunicateRequest) -> ToolResult:
        self.sent.append(request)
        return ToolResult(tool_call_id="", content=f"sent:{self.name}")

    def capabilities_for(self, participant_id: str) -> ChannelCapabilities:
        rich = (
            participant_id in self._live
            if self.participant_prefix == ""
            else self._supports_extra
        )
        return ChannelCapabilities(
            conversation_id=rich,
            attachments=rich,
            extra=rich,
        )

    def list_connections(self) -> list[ConnectionInfo]:
        return [
            ConnectionInfo(
                participant_id=participant_id,
                channel=self.name,
                kind="fake",
                active=participant_id in self._live,
            )
            for participant_id in self._live
        ]

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class _MinimalChannel(BaseChannel):
    name = "minimal"
    participant_prefix = "minimal:"

    async def send(self, request: CommunicateRequest) -> ToolResult:
        return ToolResult(tool_call_id="", content=request.message)


class _FailingRuntimeChannel(_MinimalChannel):
    name = "failing"
    participant_prefix = "failing:"

    def __init__(self) -> None:
        super().__init__(runtime=self)

    async def start(self) -> None:
        raise OSError("cannot connect")

    async def stop(self) -> None:
        pass


@pytest.fixture()
def registry() -> ChannelRegistry:
    return ChannelRegistry()


@pytest.mark.asyncio
async def test_base_channel_only_requires_outbound_implementation(
    registry: ChannelRegistry,
) -> None:
    channel = _MinimalChannel()
    registry.register(channel)

    result = await registry.send(
        CommunicateRequest(participant_id="minimal:alice", message="hello")
    )

    assert result.content == "hello"
    assert channel.list_connections() == []
    assert channel.resolve("alice") is None


@pytest.mark.asyncio
async def test_base_channel_from_sender_is_minimal_registration_path(
    registry: ChannelRegistry,
) -> None:
    requests: list[CommunicateRequest] = []

    async def sender(request: CommunicateRequest) -> ToolResult:
        requests.append(request)
        return ToolResult(tool_call_id="", content="sent")

    registry.register(BaseChannel.from_sender("team:", sender))

    result = await registry.send(
        CommunicateRequest(participant_id="team:alice", message="hello")
    )

    assert not result.is_error
    assert requests[0].message == "hello"


@pytest.mark.asyncio
async def test_longest_prefix_wins(registry: ChannelRegistry) -> None:
    generic = _FakeChannel("generic", "rich:")
    specific = _FakeChannel("specific", "rich:team:")
    registry.register(generic)
    registry.register(specific)

    result = await registry.send(
        CommunicateRequest(participant_id="rich:team:alice", message="hi")
    )

    assert not result.is_error
    assert specific.sent and not generic.sent
    assert specific.sent[0].participant_id == "rich:team:alice"


@pytest.mark.asyncio
async def test_prefix_match_bypasses_resolver(registry: ChannelRegistry) -> None:
    channel = _FakeChannel(
        "channel",
        "channel:",
        resolver=lambda participant_id: (
            f"channel:{participant_id}" if not participant_id.startswith("channel:") else None
        ),
    )
    registry.register(channel)

    await registry.send(CommunicateRequest(participant_id="channel:alice", message="hi"))

    assert channel.sent[0].participant_id == "channel:alice"


@pytest.mark.asyncio
async def test_no_prefix_single_match_auto_routes(registry: ChannelRegistry) -> None:
    channel = _FakeChannel(
        "channel",
        "channel:",
        resolver=lambda participant_id: (
            f"channel:single:{participant_id}" if participant_id == "alice" else None
        ),
    )
    registry.register(channel)

    await registry.send(CommunicateRequest(participant_id="alice", message="hi"))

    assert channel.sent[0].participant_id == "channel:single:alice"


def test_resolve_participant_id_expands_and_passes_through(
    registry: ChannelRegistry,
) -> None:
    channel = _FakeChannel(
        "channel",
        "channel:",
        resolver=lambda participant_id: (
            f"channel:single:{participant_id}" if participant_id == "alice" else None
        ),
    )
    registry.register(channel)

    assert registry.resolve_participant_id("alice") == "channel:single:alice"
    assert registry.resolve_participant_id("channel:single:alice") == "channel:single:alice"
    assert registry.resolve_participant_id("unknown") == "unknown"


@pytest.mark.asyncio
async def test_no_prefix_multi_match_raises(registry: ChannelRegistry) -> None:
    channel_a = _FakeChannel(
        "channel_a",
        "channel_a:",
        resolver=lambda participant_id: (
            f"channel_a:{participant_id}" if participant_id == "alice" else None
        ),
    )
    channel_b = _FakeChannel(
        "channel_b",
        "channel_b:",
        resolver=lambda participant_id: (
            f"channel_b:{participant_id}" if participant_id == "alice" else None
        ),
    )
    registry.register(channel_a)
    registry.register(channel_b)

    with pytest.raises(ParticipantIdResolutionError) as error:
        await registry.send(CommunicateRequest(participant_id="alice", message="hi"))

    message = str(error.value)
    assert "多个信道" in message
    assert "channel_a:alice" in message
    assert "channel_b:alice" in message


@pytest.mark.asyncio
async def test_no_prefix_no_match_falls_back_to_stream(registry: ChannelRegistry) -> None:
    stream = _FakeChannel("stream", "")
    registry.register(stream)

    await registry.send(CommunicateRequest(participant_id="unknown_user", message="hello"))

    assert stream.sent and stream.sent[0].participant_id == "unknown_user"


def test_supports_extra_follows_selected_transport(registry: ChannelRegistry) -> None:
    plain = _FakeChannel("plain", "plain:")
    rich = _FakeChannel("rich", "rich:", supports_extra=True)
    stream = _FakeChannel("stream", "", live=("stream-client",))
    registry.register(plain)
    registry.register(rich)
    registry.register(stream)

    assert not registry.supports_message_extra("plain:alice")
    assert registry.supports_message_extra("rich:alice")
    assert registry.supports_message_extra("stream-client")
    assert not registry.supports_message_extra("offline-client")


def test_list_connections_aggregates_across_channels(registry: ChannelRegistry) -> None:
    stream = _FakeChannel("stream", "", live=("a",))
    wecom = _FakeChannel("wecom", "wecom:")
    registry.register(stream)
    registry.register(wecom)

    connections = registry.list_connections()

    assert [connection.participant_id for connection in connections] == ["a"]
    assert connections[0].channel == "stream"


@pytest.mark.asyncio
async def test_shared_runtime_starts_and_stops_once(registry: ChannelRegistry) -> None:
    stream = _FakeChannel("stream", "")
    desktop = _FakeChannel("desktop", "coworker-desktop:")
    desktop.runtime = stream.runtime
    registry.register(stream)
    registry.register(desktop)

    await registry.start()
    await registry.stop()

    assert stream.started
    assert stream.stopped
    assert not desktop.started
    assert not desktop.stopped


@pytest.mark.asyncio
async def test_stop_before_start_is_a_noop(registry: ChannelRegistry) -> None:
    channel = _FakeChannel("stream", "")
    registry.register(channel)

    await registry.stop()

    assert not channel.stopped


def test_duplicate_fallback_is_rejected(registry: ChannelRegistry) -> None:
    registry.register(_FakeChannel("stream", ""))

    with pytest.raises(ValueError, match="fallback channel already registered"):
        registry.register(_FakeChannel("other", ""))


def test_duplicate_name_is_rejected(registry: ChannelRegistry) -> None:
    registry.register(_FakeChannel("chat", "chat:"))

    with pytest.raises(ValueError, match="channel name already registered"):
        registry.register(_FakeChannel("chat", "other:"))


def test_duplicate_prefix_is_rejected(registry: ChannelRegistry) -> None:
    registry.register(_FakeChannel("chat", "chat:"))

    with pytest.raises(ValueError, match="participant prefix already registered"):
        registry.register(_FakeChannel("other", "chat:"))


def test_invalid_runtime_is_rejected_during_registration(
    registry: ChannelRegistry,
) -> None:
    channel = _MinimalChannel()
    setattr(channel, "_runtime", object())

    with pytest.raises(TypeError, match="must implement ChannelRuntime"):
        registry.register(channel)


@pytest.mark.asyncio
async def test_registration_after_start_is_rejected(registry: ChannelRegistry) -> None:
    registry.register(_FakeChannel("stream", ""))
    await registry.start()

    with pytest.raises(RuntimeError, match="while the registry is running"):
        registry.register(_FakeChannel("late", "late:"))

    await registry.stop()


@pytest.mark.asyncio
async def test_immediate_runtime_start_failure_is_reported(
    registry: ChannelRegistry,
) -> None:
    registry.register(_FailingRuntimeChannel())

    with pytest.raises(RuntimeError, match="cannot connect"):
        await registry.start()
