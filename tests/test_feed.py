"""Tests for the socket.io realtime feed (socket client mocked)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from socketio.exceptions import ConnectionError as SioConnectionError

from custom_components.imhd.api import ImhdRealtimeFeed

from .conftest import sample_tabs


@pytest.fixture(autouse=True)
def bratislava_time() -> None:
    """Use real time here: the feed relies on asyncio timeouts."""


class FakeSioClient:
    """Minimal stand-in for socketio.AsyncClient."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.handlers: dict[str, Callable[..., Any]] = {}
        self.emitted: list[tuple[str, Any]] = []
        self.url: str | None = None
        self.kwargs: dict[str, Any] = {}
        self.connected = False
        self.disconnects = 0

    def on(self, event: str, handler: Callable[..., Any]) -> None:
        self.handlers[event] = handler

    async def connect(self, url: str, **kwargs: Any) -> None:
        if self.fail:
            raise SioConnectionError("refused")
        self.url, self.kwargs, self.connected = url, kwargs, True

    async def emit(self, event: str, data: Any = None) -> None:
        self.emitted.append((event, data))

    async def disconnect(self) -> None:
        self.disconnects += 1
        self.connected = False

    async def server(self, event: str, *args: Any) -> None:
        """Simulate an event sent by the server."""
        await self.handlers[event](*args)


class Recorder:
    """FeedListener recording every callback."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def feed_connected(self) -> None:
        self.events.append(("connected", None))

    def feed_disconnected(self) -> None:
        self.events.append(("disconnected", None))

    def feed_tabs(self, payload: Any) -> None:
        self.events.append(("tabs", payload))

    def feed_vehicle(self, payload: Any) -> None:
        self.events.append(("vehicle", payload))

    def feed_info(self, payload: Any) -> None:
        self.events.append(("info", payload))

    def feed_rejected(self, reason: str) -> None:
        self.events.append(("rejected", reason))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]


async def wait_for(condition: Callable[[], bool], seconds: float = 2) -> None:
    """Wait until condition() is true."""
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(0.005)


def make_feed(clients: list[FakeSioClient], listener: Recorder, **kwargs: Any) -> ImhdRealtimeFeed:
    """Create a feed whose client factory hands out `clients` in order."""
    pending = list(clients)

    def factory(_session: Any) -> FakeSioClient:
        return pending.pop(0) if pending else FakeSioClient(fail=True)

    options: dict[str, Any] = {"backoff_min": 0.01, "backoff_max": 0.02, **kwargs}
    return ImhdRealtimeFeed(
        section="ba", stop_id=83, listener=listener, client_factory=factory, **options
    )


async def test_subscribe_and_receive() -> None:
    """The feed subscribes after connecting and forwards server events."""
    client, listener = FakeSioClient(), Recorder()
    feed = make_feed([client], listener)
    task = asyncio.create_task(feed.run())
    await wait_for(lambda: feed.connected)

    assert client.url == "https://imhd.sk/"
    assert client.kwargs["socketio_path"] == "/rt/sio2"
    assert client.kwargs["transports"] == ["websocket"]
    assert client.kwargs["headers"]["Origin"] == "https://imhd.sk"
    assert client.kwargs["headers"]["Referer"] == (
        "https://imhd.sk/ba/online-zastavkova-tabula?st=83"
    )
    assert "HomeAssistant" in client.kwargs["headers"]["User-Agent"]
    assert client.emitted == [("tabStart", [83, ["*"], "ba"]), ("infoStart", None)]

    payload = sample_tabs()
    await client.server("cack", True)
    await client.server("tabs", payload)
    await client.server("vInfo", {"issi": "1:4401", "lf": 1})
    await client.server("iText", ["", "Výluka"])
    assert listener.events == [
        ("connected", None),
        ("tabs", payload),
        ("vehicle", {"issi": "1:4401", "lf": 1}),
        ("info", ["", "Výluka"]),
    ]

    await feed.async_stop()
    await asyncio.wait_for(task, 1)
    assert client.disconnects >= 1
    assert listener.kinds()[-1] == "disconnected"
    assert feed.sessions == 1
    assert feed.reconnects == 0


async def test_rejection_backs_off_long() -> None:
    """A refused admission (cack != true) waits the long back-off."""
    first, second, listener = FakeSioClient(), FakeSioClient(), Recorder()
    feed = make_feed([first, second], listener, reject_backoff=0.2)
    task = asyncio.create_task(feed.run())
    await wait_for(lambda: feed.connected)
    await first.server("cack", [-12])
    await wait_for(lambda: not feed.connected)
    assert ("rejected", "-12 too many connections from this IP address") in listener.events
    assert feed.rejected == "-12 too many connections from this IP address"
    await asyncio.sleep(0.05)
    assert feed.sessions == 1  # still backing off (normal back-off is 0.01 s)
    await wait_for(lambda: second.connected)
    await second.server("cack", True)
    assert feed.rejected is None
    await feed.async_stop()
    await asyncio.wait_for(task, 1)


async def test_unknown_rejection_payload() -> None:
    """Unknown rejection payloads are reported verbatim."""
    client, listener = FakeSioClient(), Recorder()
    feed = make_feed([client], listener, reject_backoff=30)
    task = asyncio.create_task(feed.run())
    await wait_for(lambda: feed.connected)
    await client.server("cack", [False, "x"])
    await wait_for(lambda: feed.rejected is not None)
    assert feed.rejected == '[false, "x"]'
    await feed.async_stop()
    await asyncio.wait_for(task, 1)


async def test_reconnect_after_failure_and_disconnect() -> None:
    """Connection failures and server disconnects are retried with backoff."""
    second, listener = FakeSioClient(), Recorder()
    feed = make_feed([FakeSioClient(fail=True), second], listener)
    task = asyncio.create_task(feed.run())
    await wait_for(lambda: feed.connected)
    assert feed.sessions == 2
    assert feed.reconnects == 1
    assert listener.kinds()[:2] == ["disconnected", "connected"]

    await second.server("disconnect")
    await wait_for(lambda: feed.sessions >= 3)
    await feed.async_stop()
    await asyncio.wait_for(task, 1)


async def test_stale_watchdog_reconnects() -> None:
    """A board with departures that goes silent forces a new connection."""
    first, second, listener = FakeSioClient(), FakeSioClient(), Recorder()
    feed = make_feed([first, second], listener, stale_after=0.05)
    task = asyncio.create_task(feed.run())
    await wait_for(lambda: first.connected)
    await first.server("tabs", sample_tabs())
    await wait_for(lambda: second.connected, seconds=3)
    assert first.disconnects >= 1
    await feed.async_stop()
    await asyncio.wait_for(task, 1)


async def test_quiet_board_is_not_stale() -> None:
    """Silence on a board without departures is normal (no reconnect loop)."""
    client, listener = FakeSioClient(), Recorder()
    feed = make_feed([client], listener, stale_after=0.05)
    task = asyncio.create_task(feed.run())
    await wait_for(lambda: client.connected)
    await client.server("tabs", [{"zastavka": 83, "nastupiste": 213, "tab": []}])
    await asyncio.sleep(0.2)
    assert feed.sessions == 1
    assert client.connected
    await feed.async_stop()
    await asyncio.wait_for(task, 1)


async def test_request_reconnect_skips_backoff() -> None:
    """request_reconnect drops the session and connects again right away."""
    first, second, listener = FakeSioClient(), FakeSioClient(), Recorder()
    feed = make_feed([first, second], listener, backoff_min=30, backoff_max=30)
    task = asyncio.create_task(feed.run())
    await wait_for(lambda: first.connected)
    await first.server("tabs", [])
    feed.request_reconnect()
    await wait_for(lambda: second.connected)
    await feed.async_stop()
    await asyncio.wait_for(task, 1)


async def test_listener_errors_do_not_break_feed(caplog: pytest.LogCaptureFixture) -> None:
    """An exception in a listener callback is logged, the session continues."""
    client, listener = FakeSioClient(), Recorder()

    def broken(_payload: Any) -> None:
        raise ValueError("bad payload")

    listener.feed_tabs = broken  # type: ignore[method-assign]
    feed = make_feed([client], listener)
    task = asyncio.create_task(feed.run())
    await wait_for(lambda: feed.connected)
    await client.server("tabs", [])
    assert feed.connected
    assert "error handling event" in caplog.text
    await feed.async_stop()
    await asyncio.wait_for(task, 1)
