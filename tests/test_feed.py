"""Tests for the socket.io realtime feed (socket client mocked)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import itertools
import time
from typing import Any

import aiohttp
from engineio.exceptions import ConnectionError as EngineIOConnectionError
from multidict import CIMultiDict, CIMultiDictProxy
import pytest
from socketio.exceptions import ConnectionError as SioConnectionError
from yarl import URL

from custom_components.imhd import api
from custom_components.imhd.api import ImhdRealtimeFeed

from .conftest import sample_tabs


@pytest.fixture(autouse=True)
def bratislava_time() -> None:
    """Use real time here: the feed relies on asyncio timeouts."""


class FakeSioClient:
    """Minimal stand-in for socketio.AsyncClient.

    `early` events are delivered while connect() is still running, like real
    python-socketio does with `cack` (sent by the server right on connect).
    """

    def __init__(
        self,
        fail: bool = False,
        *,
        early: list[tuple[str, tuple[Any, ...]]] | None = None,
        exc: BaseException | None = None,
        hang: bool = False,
    ) -> None:
        self.fail = fail
        self.early = early or []
        self.exc = exc
        self.hang = hang
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
        if self.exc is not None:
            raise self.exc
        if self.hang:
            await asyncio.Event().wait()
        self.url, self.kwargs, self.connected = url, kwargs, True
        for event, args in self.early:
            await self.server(event, *args)

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

    def feed_admitted(self) -> None:
        self.events.append(("admitted", None))

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
        ("admitted", None),
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


def handshake_error(status: int) -> SioConnectionError:
    """Build the exception chain python-socketio raises for an HTTP 4xx handshake."""
    request = aiohttp.RequestInfo(
        url=URL("wss://imhd.sk/rt/sio2/"),
        method="GET",
        headers=CIMultiDictProxy(CIMultiDict()),
        real_url=URL("wss://imhd.sk/rt/sio2/"),
    )
    try:
        try:
            raise aiohttp.WSServerHandshakeError(request, (), status=status, message="denied")
        except aiohttp.WSServerHandshakeError:
            # engineio raises this inside its except block (implicit context).
            raise EngineIOConnectionError("Connection error")  # noqa: B904
    except EngineIOConnectionError as err:
        try:
            raise SioConnectionError("Connection error") from err
        except SioConnectionError as wrapped:
            return wrapped


class GapRecorder:
    """Client factory recording when each connection attempt starts."""

    def __init__(self, clients: list[FakeSioClient]) -> None:
        self.clients = list(clients)
        self.started: list[float] = []

    def __call__(self, _session: Any) -> FakeSioClient:
        self.started.append(time.monotonic())
        return self.clients.pop(0) if self.clients else FakeSioClient(fail=True)

    @property
    def gaps(self) -> list[float]:
        return [b - a for a, b in itertools.pairwise(self.started)]


async def _short_session(client: FakeSioClient) -> None:
    """Deliver departures, then drop the connection right away."""
    await wait_for(lambda: client.connected)
    await client.server("tabs", sample_tabs())
    await client.server("disconnect")


async def test_short_sessions_keep_backing_off() -> None:
    """accept -> tabs -> drop loops must not reconnect every 2 s (finding 2)."""
    clients = [FakeSioClient() for _ in range(4)]
    factory, listener = GapRecorder(clients), Recorder()
    feed = ImhdRealtimeFeed(
        section="ba",
        stop_id=83,
        listener=listener,
        client_factory=factory,
        backoff_min=0.05,
        backoff_max=5,
    )
    task = asyncio.create_task(feed.run())
    for client in clients:
        await _short_session(client)
    await wait_for(lambda: len(factory.started) >= 5, seconds=5)
    await feed.async_stop()
    await asyncio.wait_for(task, 1)
    gaps = factory.gaps
    # 0.05, 0.1, 0.2, 0.4 s - growing although every session delivered tabs.
    assert gaps[2] >= 0.18
    assert gaps[3] >= 0.36


async def test_stable_session_resets_backoff() -> None:
    """A session that lasted long enough resets the back-off."""
    clients = [FakeSioClient(), FakeSioClient(), FakeSioClient()]
    factory, listener = GapRecorder(clients), Recorder()
    feed = ImhdRealtimeFeed(
        section="ba",
        stop_id=83,
        listener=listener,
        client_factory=factory,
        backoff_min=0.05,
        backoff_max=5,
        stable_after=0.2,
    )
    task = asyncio.create_task(feed.run())
    await _short_session(clients[0])  # backoff 0.05 -> next 0.1
    await wait_for(lambda: clients[1].connected)
    await clients[1].server("tabs", sample_tabs())
    await asyncio.sleep(0.25)  # stable session
    await clients[1].server("disconnect")
    await wait_for(lambda: clients[2].connected)
    await feed.async_stop()
    await asyncio.wait_for(task, 1)
    assert factory.gaps[1] < 0.09 + 0.25 + 0.05  # reset to 0.05 after the stable session
    assert factory.gaps[1] >= 0.25 + 0.04


@pytest.mark.parametrize("status", [403, 429])
async def test_handshake_4xx_is_a_rejection(status: int) -> None:
    """HTTP 403/429 on the websocket handshake backs off like a cack rejection."""
    client, listener = FakeSioClient(exc=handshake_error(status)), Recorder()
    feed = make_feed([client], listener, reject_backoff=30)
    task = asyncio.create_task(feed.run())
    await wait_for(lambda: feed.rejected is not None)
    assert feed.rejected.startswith(f"HTTP {status}")
    assert ("rejected", feed.rejected) in listener.events
    await asyncio.sleep(0.1)
    assert feed.sessions == 1  # long back-off, not 0.01 s
    await feed.async_stop()
    await asyncio.wait_for(task, 1)


async def test_rejection_before_connect_returns() -> None:
    """Real ordering: cack arrives before connect() returns - no subscription then."""
    client = FakeSioClient(early=[("cack", ([-12],))])
    listener = Recorder()
    feed = make_feed([client], listener, reject_backoff=30)
    task = asyncio.create_task(feed.run())
    await wait_for(lambda: feed.rejected is not None)
    await wait_for(lambda: client.disconnects > 0)
    assert client.emitted == []
    assert "connected" not in listener.kinds()
    assert feed.sessions == 1
    await feed.async_stop()
    await asyncio.wait_for(task, 1)


async def test_admission_before_connect_returns() -> None:
    """cack(true) before connect() returns reports the admission and subscribes."""
    client = FakeSioClient(early=[("cack", (True,))])
    listener = Recorder()
    feed = make_feed([client], listener)
    feed.rejected = "-12 too many connections from this IP address"
    task = asyncio.create_task(feed.run())
    await wait_for(lambda: feed.connected)
    assert listener.kinds()[:2] == ["admitted", "connected"]
    assert feed.rejected is None
    assert [event for event, _ in client.emitted] == ["tabStart", "infoStart"]
    await feed.async_stop()
    await asyncio.wait_for(task, 1)


async def test_malformed_tab_does_not_escape() -> None:
    """A non-list `tab` never raises inside the socket.io handler."""
    client, listener = FakeSioClient(), Recorder()
    feed = make_feed([client], listener)
    task = asyncio.create_task(feed.run())
    await wait_for(lambda: feed.connected)
    await client.server("tabs", [{"zastavka": 83, "nastupiste": 213, "tab": 5}, "junk"])
    await client.server("tabs", "junk")
    assert feed.connected
    await feed.async_stop()
    await asyncio.wait_for(task, 1)


async def test_connect_timeout() -> None:
    """A hanging handshake is abandoned and retried."""
    first, second, listener = FakeSioClient(hang=True), FakeSioClient(), Recorder()
    feed = make_feed([first, second], listener, connect_timeout=0.05)
    task = asyncio.create_task(feed.run())
    await wait_for(lambda: second.connected)
    assert feed.sessions == 2
    await feed.async_stop()
    await asyncio.wait_for(task, 1)


async def test_quiet_board_watch_does_not_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once a quiet board is past stale_after the watch loop sleeps, not polls."""
    calls = 0

    class CountingTime:
        @staticmethod
        def monotonic() -> float:
            nonlocal calls
            calls += 1
            return time.monotonic()

    monkeypatch.setattr(api, "time", CountingTime)
    client, listener = FakeSioClient(), Recorder()
    feed = make_feed([client], listener, stale_after=0.01)
    task = asyncio.create_task(feed.run())
    await wait_for(lambda: feed.connected)
    await client.server("tabs", [{"zastavka": 83, "nastupiste": 213, "tab": []}])
    await asyncio.sleep(0.1)
    before = calls
    await asyncio.sleep(2.2)
    assert calls - before <= 1
    # Departures arrive: the watchdog is armed again and fires.
    await client.server("tabs", sample_tabs())
    await wait_for(lambda: feed.sessions >= 2)
    await feed.async_stop()
    await asyncio.wait_for(task, 1)


async def test_unexpected_error_traceback_once(caplog: pytest.LogCaptureFixture) -> None:
    """Unexpected connect errors log one traceback, then one-line warnings."""
    clients = [FakeSioClient(exc=RuntimeError("boom")) for _ in range(3)]
    listener = Recorder()
    feed = make_feed(clients, listener)
    task = asyncio.create_task(feed.run())
    await wait_for(lambda: feed.sessions >= 3)
    await asyncio.sleep(0.05)
    await feed.async_stop()
    await asyncio.wait_for(task, 1)
    records = [r for r in caplog.records if "boom" in r.getMessage() or r.exc_info]
    assert len([r for r in records if r.exc_info]) == 1
    assert len(records) >= 3
