"""Tests for the RFLink connection hub using a fake gateway."""

import asyncio

import pytest
from conftest import hub as h


class FakeGateway:
    """Simulates an RFLink board on the other end of the serial link."""

    def __init__(self, *, answer=True, reject=False, version=True):
        self.reader = asyncio.StreamReader()
        self.written: list[str] = []
        self.answer = answer
        self.reject = reject
        self.version = version
        self.closed = False
        self.counter = 0

    # StreamWriter interface
    def write(self, data: bytes) -> None:
        if self.closed:
            raise ConnectionError("closed")
        frame = data.decode().strip()
        self.written.append(frame)
        if not self.answer:
            return
        self.counter += 1
        if frame == "10;PING;":
            reply = "PONG"
        elif frame == "10;VERSION;":
            if not self.version:
                return
            reply = "VER=1.1;REV=48;BUILD=0c"
        elif self.reject:
            reply = "CMD UNKNOWN"
        else:
            reply = "OK"
        self.feed(f"20;{self.counter:02X};{reply};")

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True
        self.reader.feed_eof()

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)

    def feed(self, line: str) -> None:
        self.reader.feed_data((line + "\r\n").encode())


def make_hub(gateway, **kwargs):
    packets, states = [], []

    async def opener(port, baudrate):
        return gateway.reader, gateway

    hub = h.RFLinkHub(
        "COM1",
        opener=opener,
        on_packet=packets.append,
        on_connection=states.append,
        ack_timeout=0.05,
        handshake_timeout=0.05,
        reconnect_delay=0.01,
        **kwargs,
    )
    return hub, packets, states


async def wait_for(predicate, timeout=1.0):
    async def _loop():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_loop(), timeout)


async def test_connect_handshake_and_send():
    gw = FakeGateway()
    hub, _, states = make_hub(gw)
    hub.start()
    await wait_for(lambda: hub.connected)
    assert hub.firmware == "1.1 R48 build 0c"
    assert states == [True]

    await hub.async_send_command("10;Kaku;41;1;ON;")
    assert gw.written[-1] == "10;Kaku;41;1;ON;"
    assert hub.stats["acked"] == 1
    await hub.stop()
    assert states == [True, False]


async def test_send_when_disconnected_raises():
    hub, _, _ = make_hub(FakeGateway())
    with pytest.raises(h.RFLinkCommandError):
        await hub.async_send_command("10;Kaku;41;1;ON;")


async def test_rejected_command_raises():
    gw = FakeGateway(reject=True)
    hub, _, _ = make_hub(gw)
    hub.start()
    await wait_for(lambda: hub.connected)
    with pytest.raises(h.RFLinkCommandError, match="rejected"):
        await hub.async_send_command("10;Bogus;1;1;ON;")
    await hub.stop()


async def test_no_ack_raises_and_repeats():
    gw = FakeGateway()
    hub, _, _ = make_hub(gw, repeats=3)
    hub.start()
    await wait_for(lambda: hub.connected)
    await hub.async_send_command("10;Kaku;41;1;ON;")
    assert gw.written.count("10;Kaku;41;1;ON;") == 3

    gw.answer = False
    with pytest.raises(h.RFLinkCommandError, match="acknowledge"):
        await hub.async_send_command("10;Kaku;41;1;OFF;", repeats=1)
    await hub.stop()


async def test_concurrent_commands_are_serialised():
    gw = FakeGateway()
    hub, _, _ = make_hub(gw)
    hub.start()
    await wait_for(lambda: hub.connected)
    frames = [f"10;Kaku;41;{i};ON;" for i in range(5)]
    await asyncio.gather(*(hub.async_send_command(f) for f in frames))
    assert sorted(gw.written[1:]) == sorted(frames)
    assert hub.stats["acked"] == 5
    await hub.stop()


async def test_packets_dispatched_and_recent_devices():
    gw = FakeGateway()
    hub, packets, _ = make_hub(gw)
    hub.start()
    await wait_for(lambda: hub.connected)
    gw.feed("20;01;Kaku;ID=41;SWITCH=1;CMD=ON;")
    gw.feed("20;02;Oregon TempHygro;ID=0A4C;TEMP=00ba;HUM=40;BAT=OK;")
    gw.feed("20;03;Kaku;ID=41;SWITCH=1;CMD=OFF;")
    await wait_for(lambda: len(packets) == 3)
    assert list(hub.recent_devices) == ["Oregon TempHygro_0A4C", "Kaku_41_1"]
    assert hub.recent_devices["Kaku_41_1"]["data"]["CMD"] == "OFF"
    await hub.stop()


async def test_reconnects_after_port_loss():
    gateways = [FakeGateway(), FakeGateway()]
    opened = []

    async def opener(port, baudrate):
        gw = gateways[len(opened)]
        opened.append(gw)
        return gw.reader, gw

    states = []
    hub = h.RFLinkHub(
        "COM1",
        opener=opener,
        on_packet=lambda p: None,
        on_connection=states.append,
        ack_timeout=0.05,
        handshake_timeout=0.05,
        reconnect_delay=0.01,
    )
    hub.start()
    await wait_for(lambda: hub.connected)
    gateways[0].reader.feed_eof()  # cable unplugged
    await wait_for(lambda: len(opened) == 2 and hub.connected)
    assert states == [True, False, True]
    assert hub.stats["reconnects"] == 1
    await hub.stop()


async def test_watchdog_drops_silent_gateway():
    gw = FakeGateway()
    hub, _, states = make_hub(gw, keepalive_interval=0.02)
    hub.start()
    await wait_for(lambda: hub.connected)
    gw.answer = False  # board hangs, port stays open
    await wait_for(lambda: False in states)
    await hub.stop()


async def test_connects_even_without_version_answer():
    gw = FakeGateway(version=False)
    hub, _, _ = make_hub(gw)
    hub.start()
    await wait_for(lambda: hub.connected)
    assert hub.firmware is None
    assert gw.written.count("10;VERSION;") == h.HANDSHAKE_ATTEMPTS
    await hub.stop()


async def test_open_failure_retries():
    attempts = []
    gw = FakeGateway()

    async def opener(port, baudrate):
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("no such device")
        return gw.reader, gw

    hub = h.RFLinkHub(
        "COM1",
        opener=opener,
        on_packet=lambda p: None,
        on_connection=lambda s: None,
        ack_timeout=0.05,
        handshake_timeout=0.05,
        reconnect_delay=0.001,
    )
    hub.start()
    await wait_for(lambda: hub.connected)
    assert len(attempts) == 3
    await hub.stop()


async def test_write_error_during_handshake_reconnects():
    class BrokenGateway(FakeGateway):
        def write(self, data):
            raise OSError("write failed")

    gateways = [BrokenGateway(), FakeGateway()]
    opened = []

    async def opener(port, baudrate):
        gw = gateways[min(len(opened), 1)]
        opened.append(gw)
        return gw.reader, gw

    hub = h.RFLinkHub(
        "COM1",
        opener=opener,
        on_packet=lambda p: None,
        on_connection=lambda s: None,
        ack_timeout=0.05,
        handshake_timeout=0.05,
        reconnect_delay=0.01,
    )
    hub.start()
    await wait_for(lambda: hub.connected)
    assert len(opened) == 2
    await hub.stop()
