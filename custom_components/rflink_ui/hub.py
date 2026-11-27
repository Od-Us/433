"""Connection management for the RFLink gateway.

This module has no Home Assistant dependency: Home Assistant glue is provided
through the ``on_packet`` / ``on_connection`` callbacks. It is therefore unit
testable with plain asyncio.

Reliability features:
* a single command is in flight at a time (lock), so frames sent in quick
  succession are not interleaved or dropped by the gateway;
* every ``10;`` frame waits for the gateway ``20;xx;OK;`` acknowledgement and a
  ``CMD UNKNOWN`` answer is reported as an error;
* optional repetition of each command (for receivers with poor reception);
* explicit errors when the gateway is disconnected instead of silently
  pretending the command was sent;
* handshake (``10;VERSION;``) after opening the port, which also waits for the
  Arduino to finish rebooting (opening the port resets most boards);
* PING/PONG watchdog: if the gateway stops answering, the connection is
  dropped and re-opened;
* exponential back-off on reconnection.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from .protocol import (
    KIND_BANNER,
    KIND_DEVICE,
    KIND_RESPONSE,
    RESPONSE_UNKNOWN,
    RESPONSE_VERSION,
    Packet,
    parse_line,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_BAUDRATE = 57600
DEFAULT_ACK_TIMEOUT = 2.0
DEFAULT_KEEPALIVE_INTERVAL = 60.0
DEFAULT_RECONNECT_DELAY = 5.0
MAX_RECONNECT_DELAY = 60.0
HANDSHAKE_ATTEMPTS = 3
HANDSHAKE_TIMEOUT = 2.5
RECENT_DEVICES_MAX = 50
HISTORY_MAX = 300
DEBUG_AUTO_OFF = 600.0  # seconds

DIR_IN = "in"
DIR_OUT = "out"

# Raw debug modes: name -> firmware setting
DEBUG_MODES = {"unknown": "RFUDEBUG", "all": "RFDEBUG"}

Opener = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, Any]]]


class RFLinkCommandError(Exception):
    """Raised when a command could not be delivered to the gateway."""


class RFLinkHub:
    """Owns the serial link to one RFLink gateway."""

    def __init__(
        self,
        port: str,
        *,
        opener: Opener,
        on_packet: Callable[[Packet], None],
        on_connection: Callable[[bool], None],
        baudrate: int = DEFAULT_BAUDRATE,
        repeats: int = 1,
        ack_timeout: float = DEFAULT_ACK_TIMEOUT,
        keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL,
        reconnect_delay: float = DEFAULT_RECONNECT_DELAY,
        handshake_timeout: float = HANDSHAKE_TIMEOUT,
        create_task: Callable[[Awaitable[None], str], asyncio.Task] | None = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.repeats = max(1, int(repeats))
        self.ack_timeout = ack_timeout
        self.keepalive_interval = keepalive_interval
        self.reconnect_delay = reconnect_delay
        self.handshake_timeout = handshake_timeout

        self._opener = opener
        self._create_task = create_task or (
            lambda coro, name: asyncio.get_running_loop().create_task(coro, name=name)
        )
        self._on_packet = on_packet
        self._on_connection = on_connection

        self._writer: Any = None
        self._connected = False
        self._stopping = False
        self._run_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._pending: asyncio.Future[Packet] | None = None

        self.firmware: str | None = None
        self.last_received: float | None = None
        self.stats = {
            "sent": 0,
            "acked": 0,
            "no_ack": 0,
            "rejected": 0,
            "reconnects": 0,
        }
        self.recent_devices: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # Every line exchanged with the gateway, for the signal monitor.
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY_MAX)
        self.debug_mode: str | None = None
        self._debug_off_handle: asyncio.TimerHandle | None = None

    # ------------------------------------------------------------------ state

    @property
    def connected(self) -> bool:
        """Return True when the gateway is connected and answered the handshake."""
        return self._connected

    def _set_connected(self, value: bool) -> None:
        if self._connected == value:
            return
        self._connected = value
        if not value:
            # Opening the port reboots the board, which clears its debug mode.
            self.debug_mode = None
        try:
            self._on_connection(value)
        except Exception:  # pragma: no cover - defensive
            _LOGGER.exception("Error in RFLink connection callback")

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Start the background connection loop."""
        if self._run_task is None:
            self._stopping = False
            self._run_task = self._create_task(
                self._run(), f"rflink_ui connection {self.port}"
            )

    async def stop(self) -> None:
        """Stop the connection loop and close the port."""
        self._stopping = True
        if self._debug_off_handle is not None:
            self._debug_off_handle.cancel()
            self._debug_off_handle = None
        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            self._run_task = None
        await self._close_writer()
        self._set_connected(False)

    async def _run(self) -> None:
        delay = self.reconnect_delay
        first_failure = True
        while not self._stopping:
            try:
                reader, writer = await self._opener(self.port, self.baudrate)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - any serial error
                log = _LOGGER.error if first_failure else _LOGGER.debug
                log(
                    "Cannot open RFLink port %s: %s (retry in %ss)",
                    self.port,
                    err,
                    delay,
                )
                first_failure = False
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RECONNECT_DELAY)
                continue

            self._writer = writer
            read_task = asyncio.create_task(self._read_loop(reader))
            keepalive_task: asyncio.Task | None = None
            try:
                await self._handshake()
                _LOGGER.info(
                    "Connected to RFLink on %s (firmware %s)",
                    self.port,
                    self.firmware or "unknown",
                )
                first_failure = True
                delay = self.reconnect_delay
                self._set_connected(True)
                keepalive_task = asyncio.create_task(self._keep_alive())
                done, _ = await asyncio.wait(
                    {read_task, keepalive_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    if not task.cancelled() and (exc := task.exception()):
                        _LOGGER.warning(
                            "RFLink connection on %s lost: %s", self.port, exc
                        )
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - e.g. write error during handshake
                _LOGGER.warning("RFLink connection on %s failed: %s", self.port, err)
            finally:
                for task in (read_task, keepalive_task):
                    if task is not None and not task.done():
                        task.cancel()
                self._set_connected(False)
                await self._close_writer()

            if self._stopping:
                break
            self.stats["reconnects"] += 1
            _LOGGER.warning("Reconnecting to RFLink on %s in %ss", self.port, delay)
            await asyncio.sleep(delay)

    async def _close_writer(self) -> None:
        writer, self._writer = self._writer, None
        if writer is None:
            return
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 2)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Error while closing RFLink port: %s", err)

    async def _handshake(self) -> None:
        """Ask for the firmware version; also waits for the board to finish booting."""
        for _ in range(HANDSHAKE_ATTEMPTS):
            response = await self._transmit("10;VERSION;\n", self.handshake_timeout)
            if response is not None:
                if response.response == RESPONSE_VERSION:
                    fields = response.fields
                    self.firmware = " ".join(
                        part
                        for part in (
                            fields.get("VER"),
                            f"R{fields['REV']}" if "REV" in fields else None,
                            f"build {fields['BUILD']}" if "BUILD" in fields else None,
                        )
                        if part
                    )
                return
        _LOGGER.warning(
            "RFLink on %s did not answer the VERSION request; continuing anyway",
            self.port,
        )

    async def _keep_alive(self) -> None:
        """Ping the gateway; return (=> reconnect) if it stops answering."""
        while True:
            await asyncio.sleep(self.keepalive_interval)
            for _ in range(2):
                if await self._transmit("10;PING;\n", self.ack_timeout) is not None:
                    break
            else:
                raise ConnectionError("gateway did not answer PING")

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            line = await reader.readline()
            if not line:
                raise ConnectionError("serial port closed")
            decoded = line.decode("utf-8", errors="ignore").strip()
            if decoded:
                self.handle_line(decoded)

    # ------------------------------------------------------------- receiving

    def handle_line(self, line: str) -> Packet | None:
        """Process one line received from (or simulated for) the gateway."""
        _LOGGER.debug("RFLink <- %s", line)
        now = time.time()
        packet = parse_line(line)
        self.history.append(
            {
                "ts": now,
                "dir": DIR_IN,
                "raw": line,
                "kind": packet.kind if packet else None,
                "protocol": packet.protocol if packet else None,
                "device_id": packet.device_id if packet else None,
                "fields": packet.fields if packet else {},
            }
        )
        if packet is None:
            return None
        self.last_received = now

        if packet.kind == KIND_RESPONSE:
            if self._pending is not None and not self._pending.done():
                self._pending.set_result(packet)
            return packet

        if packet.kind == KIND_BANNER:
            _LOGGER.info("RFLink gateway: %s", packet.protocol)
            return packet

        if packet.kind == KIND_DEVICE:
            assert packet.device_id is not None
            previous = self.recent_devices.pop(packet.device_id, None) or {}
            self.recent_devices[packet.device_id] = {
                "type": packet.device_type,
                "protocol": packet.protocol,
                "data": packet.fields,
                "first_seen": previous.get("first_seen", now),
                "last_seen": now,
                "count": previous.get("count", 0) + 1,
            }
            while len(self.recent_devices) > RECENT_DEVICES_MAX:
                self.recent_devices.popitem(last=False)
            try:
                self._on_packet(packet)
            except Exception:  # pragma: no cover - defensive
                _LOGGER.exception("Error while dispatching RFLink packet %s", line)
        return packet

    # --------------------------------------------------------------- sending

    async def _transmit(self, frame: str, timeout: float) -> Packet | None:
        """Write a frame and wait for the gateway response (None on timeout)."""
        async with self._send_lock:
            writer = self._writer
            if writer is None:
                raise RFLinkCommandError("RFLink gateway is not connected")
            loop = asyncio.get_running_loop()
            self._pending = loop.create_future()
            try:
                _LOGGER.debug("RFLink -> %s", frame.strip())
                self.history.append(
                    {
                        "ts": time.time(),
                        "dir": DIR_OUT,
                        "raw": frame.strip(),
                        "kind": None,
                        "protocol": None,
                        "device_id": None,
                        "fields": {},
                    }
                )
                writer.write(frame.encode("utf-8"))
                await writer.drain()
                return await asyncio.wait_for(asyncio.shield(self._pending), timeout)
            except asyncio.TimeoutError:
                return None
            except Exception as err:  # serial libraries raise own types
                raise RFLinkCommandError(f"Error writing to RFLink: {err}") from err
            finally:
                self._pending = None

    async def async_send_command(self, frame: str, repeats: int | None = None) -> None:
        """Send a ``10;...;`` frame, waiting for the acknowledgement of each repeat."""
        if not frame.endswith("\n"):
            frame += "\n"
        if not self._connected:
            raise RFLinkCommandError("RFLink gateway is not connected")

        acked = 0
        for _ in range(max(1, repeats or self.repeats)):
            self.stats["sent"] += 1
            response = await self._transmit(frame, self.ack_timeout)
            if response is None:
                self.stats["no_ack"] += 1
                _LOGGER.warning("RFLink did not acknowledge %s", frame.strip())
                continue
            if response.response == RESPONSE_UNKNOWN:
                self.stats["rejected"] += 1
                raise RFLinkCommandError(f"RFLink rejected command '{frame.strip()}'")
            self.stats["acked"] += 1
            acked += 1

        if not acked:
            raise RFLinkCommandError(f"RFLink did not acknowledge '{frame.strip()}'")

    # ------------------------------------------------------------ debug mode

    async def async_set_debug_mode(
        self, mode: str | None, auto_off: float | None = DEBUG_AUTO_OFF
    ) -> None:
        """Enable a raw pulse debug mode of the gateway (None: disable).

        ``unknown`` (RFUDEBUG) prints the pulses of signals the firmware does
        not recognise; ``all`` (RFDEBUG) prints the pulses of every signal.
        The mode is switched off automatically after ``auto_off`` seconds.
        """
        if mode is not None and mode not in DEBUG_MODES:
            raise ValueError(f"Unknown debug mode {mode}")
        if self._debug_off_handle is not None:
            self._debug_off_handle.cancel()
            self._debug_off_handle = None
        if mode == self.debug_mode:
            if mode is not None:
                self._schedule_debug_off(auto_off)
            return
        if self.debug_mode is not None:
            await self.async_send_command(
                f"10;{DEBUG_MODES[self.debug_mode]}=OFF;", repeats=1
            )
            self.debug_mode = None
        if mode is not None:
            await self.async_send_command(f"10;{DEBUG_MODES[mode]}=ON;", repeats=1)
            self.debug_mode = mode
            self._schedule_debug_off(auto_off)

    def _schedule_debug_off(self, delay: float | None) -> None:
        if not delay:
            return

        def _off() -> None:
            self._debug_off_handle = None
            self._create_task(self.async_disable_debug(), "rflink_ui debug off")

        self._debug_off_handle = asyncio.get_running_loop().call_later(delay, _off)

    async def async_disable_debug(self) -> None:
        try:
            await self.async_set_debug_mode(None)
        except RFLinkCommandError as err:
            _LOGGER.debug("Could not disable RFLink debug mode: %s", err)
            # The gateway resets its debug mode when it reboots/reconnects.
            self.debug_mode = None
