"""BLE client for HidrateSpark bottles.

Implements the HydroSync handshake (DEBUG/SET_POINT writes) so that newer
firmwares stream sip notifications. Falls back to legacy notify-only mode
on the older 016e11b1-... characteristic.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, List, Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice

log = logging.getLogger(__name__)

# Modern (HydroSync) UUIDs
SERVICE_UUID_USER = "bf2d1ba0-c473-49f2-9571-0ce69036c642"
CHAR_USER_DATA = "bf2d1ba1-c473-49f2-9571-0ce69036c642"

SERVICE_UUID_REF = "45855422-6565-4cd7-a2a9-fe8af41b85e8"
CHAR_SET_POINT = "b44b03f0-b850-4090-86eb-72863fb3618d"
CHAR_DEBUG = "e3578b0d-caa7-46d6-b7c2-7331c08de044"

# Legacy
CHAR_DATA_POINT = "016e11b1-6c8a-4074-9e5a-076053f93784"
CHAR_LED_CONTROL = "a1d9a5bf-f5d8-49f3-a440-e6bf27440cb0"

# Standard battery service
CHAR_BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"

# Discovered on this firmware (80.18, nRF52832):
#   CHAR_WEIGHT: 2-byte big-endian u16 notify, ~every 2s.
#     High byte 0x8a = upright/stable, 0x84 = lifted/tilted, 0x88 = transient.
#     Low byte tracks fluid weight on a roughly 1:1 mL scale.
#   CHAR_CAP: 4-byte notify. 0x81020000 = cap opened, 0x80020000 = cap closed.
# (CHAR_DEBUG and CHAR_CAP share the same UUID; the bottle uses it for both
# debug ack writes during handshake AND cap state notifications.)
CHAR_WEIGHT = "1807a063-4e2d-4636-981a-35e93d1c7b94"
CHAR_CAP = CHAR_DEBUG

# Drain command
DRAIN_BYTE = bytes([0x57])

# Handshake sequence from HydroSync (50 ms apart)
HANDSHAKE_COMMANDS: List[tuple[str, str]] = [
    ("DEBUG", "2100d1"),
    ("SET_POINT", "92"),
    ("DEBUG", "2200f7"),
    ("SET_POINT", "7700000032d70000"),
    ("SET_POINT", "00341b00e0790000"),
    ("SET_POINT", "02345200c0a80000"),
    ("SET_POINT", "03346e0030c00000"),
    ("SET_POINT", "04348900a0d70000"),
    ("SET_POINT", "0534a50010ef0000"),
    ("SET_POINT", "0634c00080060100"),
    ("SET_POINT", "0734dc00f01d0100"),
    ("SET_POINT", "0834000000000000"),
    ("SET_POINT", "0934000000000000"),
]

SipCallback = Callable[[float, int, int], Awaitable[None]]
"""Called as await on_sip(timestamp, volume_ml, total_reported_ml)."""

BatteryCallback = Callable[[int], Awaitable[None]]
StatusCallback = Callable[[bool, Optional[str]], Awaitable[None]]
RefillCallback = Callable[[str, Optional[int]], Awaitable[None]]
"""Called as await on_refill(source, weight_full_low) when the bottle is refilled."""
WeightCallback = Callable[[int, int], Awaitable[None]]
"""Called as await on_weight(raw_u16, low_byte) when upright weight changes."""


class BottleClient:
    def __init__(
        self,
        mac: str,
        name_prefix: str,
        size_ml: int,
        on_sip: SipCallback,
        on_battery: BatteryCallback,
        on_status: StatusCallback,
        loop: asyncio.AbstractEventLoop,
        on_refill: Optional[RefillCallback] = None,
        on_weight: Optional[WeightCallback] = None,
    ) -> None:
        self.mac = mac.upper().strip() if mac else ""
        self.name_prefix = (name_prefix or "h2o").lower()
        self.size_ml = size_ml
        self.on_sip = on_sip
        self.on_battery = on_battery
        self.on_status = on_status
        self.on_refill = on_refill
        self.on_weight = on_weight
        self.loop = loop

        self._client: Optional[BleakClient] = None
        self._stop = asyncio.Event()
        self._connected = False
        self._handshake_path: Optional[str] = None
        self._data_char: Optional[str] = None
        self._wake_event = asyncio.Event()  # poke run loop to retry sooner
        self._force_sync_event = asyncio.Event()
        # Refill detection state
        self._weight_stable_low: Optional[int] = None  # most recent stable low-byte (upright)
        self._weight_stable_streak: int = 0
        self._weight_last_low: Optional[int] = None
        self._pre_open_weight_low: Optional[int] = None  # snapshot at cap-open
        self._cap_open: bool = False
        self._refill_check_task: Optional[asyncio.Task] = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def handshake_path(self) -> Optional[str]:
        return self._handshake_path

    def update_target(self, mac: str, name_prefix: str, size_ml: int) -> None:
        self.mac = mac.upper().strip() if mac else ""
        self.name_prefix = (name_prefix or "h2o").lower()
        self.size_ml = size_ml
        self._wake_event.set()

    def request_force_sync(self) -> None:
        self._force_sync_event.set()
        self._wake_event.set()

    def request_reconnect(self) -> None:
        self._wake_event.set()

        async def _disconnect():
            if self._client and self._client.is_connected:
                try:
                    await self._client.disconnect()
                except Exception:
                    pass

        asyncio.run_coroutine_threadsafe(_disconnect(), self.loop)

    async def stop(self) -> None:
        self._stop.set()
        self._wake_event.set()
        if self._client and self._client.is_connected:
            try:
                await self._client.disconnect()
            except Exception:
                pass

    # ---------------- main loop -------------------------------------------------
    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                device = await self._find_device()
                if not device:
                    await self.on_status(False, "device not found")
                    await self._sleep_or_wake(backoff)
                    backoff = min(backoff * 2, 60.0)
                    continue

                async with BleakClient(device, timeout=20.0) as client:
                    self._client = client
                    self._connected = True
                    backoff = 1.0
                    log.info("connected to %s (%s)", device.address, device.name)
                    await self.on_status(True, None)

                    await self._after_connect(client)
                    # Re-emit status now that handshake_path is set
                    await self.on_status(True, None)
                    # Hold connection; reconnect when bottle drops
                    while client.is_connected and not self._stop.is_set():
                        # Periodic drain attempt
                        try:
                            if self._force_sync_event.is_set():
                                self._force_sync_event.clear()
                                await self._drain(client)
                        except Exception as e:
                            log.warning("drain failed: %s", e)
                        await self._sleep_or_wake(5.0)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("BLE loop error: %s", e)
                await self.on_status(False, str(e))
                await self._sleep_or_wake(backoff)
                backoff = min(backoff * 2, 60.0)
            finally:
                self._connected = False
                self._client = None
                try:
                    await self.on_status(False, None)
                except Exception:
                    pass

    async def _sleep_or_wake(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake_event.clear()

    async def _find_device(self) -> Optional[BLEDevice]:
        if self.mac:
            log.info("scanning for %s", self.mac)
            dev = await BleakScanner.find_device_by_address(self.mac, timeout=20.0)
            if dev:
                return dev
            # Fall through to name-based scan
        log.info("scanning for name prefix %r", self.name_prefix)
        devs = await BleakScanner.discover(timeout=12.0)
        for d in devs:
            if d.name and d.name.lower().startswith(self.name_prefix):
                log.info("found %s (%s)", d.address, d.name)
                return d
        return None

    async def _after_connect(self, client: BleakClient) -> None:
        # One-time service dump for diagnostics
        try:
            log.info("=== GATT services ===")
            for s in client.services:
                log.info("SVC %s", s.uuid)
                for ch in s.characteristics:
                    props = ",".join(ch.properties)
                    extra = ""
                    if "read" in ch.properties:
                        try:
                            v = await client.read_gatt_char(ch.uuid)
                            extra = f" = {v.hex()}"
                        except Exception as e:
                            extra = f" (read err: {type(e).__name__})"
                    log.info("  CHR %s [%s]%s", ch.uuid, props, extra)
            log.info("=== end GATT ===")
        except Exception as e:
            log.warning("service dump failed: %s", e)

        # Battery
        try:
            data = await client.read_gatt_char(CHAR_BATTERY_LEVEL)
            if data:
                pct = data[0]
                log.info("battery: %d%%", pct)
                await self.on_battery(pct)
            try:
                await client.start_notify(CHAR_BATTERY_LEVEL, self._on_battery_notify)
            except Exception as e:
                log.debug("battery notify not available: %s", e)
        except Exception as e:
            log.warning("battery read failed: %s", e)

        # Try modern handshake + data char
        modern_ok = False
        try:
            await self._handshake(client)
            await client.start_notify(CHAR_USER_DATA, self._on_data_notify)
            self._data_char = CHAR_USER_DATA
            self._handshake_path = "modern"
            modern_ok = True
            log.info("modern path active (USER_DATA notifications)")
        except Exception as e:
            log.warning("modern handshake/notify failed: %s -- falling back to legacy", e)

        if not modern_ok:
            try:
                await client.start_notify(CHAR_DATA_POINT, self._on_data_notify)
                self._data_char = CHAR_DATA_POINT
                self._handshake_path = "legacy"
                log.info("legacy path active (DATA_POINT notifications)")
            except Exception as e:
                log.error("legacy notify failed too: %s", e)
                self._handshake_path = None
                raise

        # Initial drain
        try:
            await self._drain(client)
        except Exception as e:
            log.warning("initial drain failed: %s", e)

        # Subscribe to all other proprietary notify-capable chars for discovery
        await self._subscribe_unknown_notifies(client)

    async def _handshake(self, client: BleakClient) -> None:
        for char_name, hex_val in HANDSHAKE_COMMANDS:
            uuid = CHAR_DEBUG if char_name == "DEBUG" else CHAR_SET_POINT
            await client.write_gatt_char(uuid, bytes.fromhex(hex_val), response=True)
            await asyncio.sleep(0.05)
        log.info("handshake complete (%d writes)", len(HANDSHAKE_COMMANDS))

    async def _drain(self, client: BleakClient) -> None:
        if not self._data_char:
            return
        try:
            await client.write_gatt_char(self._data_char, DRAIN_BYTE, response=False)
        except Exception as e:
            log.debug("drain write failed: %s", e)

    async def _subscribe_unknown_notifies(self, client: BleakClient) -> None:
        """Subscribe to every proprietary notify-capable characteristic we don't
        already handle. Logs raw payloads so we can identify refill / fill-level
        signals."""
        known = {
            CHAR_USER_DATA.lower(),
            CHAR_DATA_POINT.lower(),
            CHAR_BATTERY_LEVEL.lower(),
        }
        # Standard service UUIDs to skip (e.g. GAP, GATT)
        skip_services = {
            "00001800-0000-1000-8000-00805f9b34fb",  # GAP
            "00001801-0000-1000-8000-00805f9b34fb",  # GATT
            "0000180a-0000-1000-8000-00805f9b34fb",  # Device Info
            "0000180f-0000-1000-8000-00805f9b34fb",  # Battery
        }
        count = 0
        for svc in client.services:
            if svc.uuid.lower() in skip_services:
                continue
            for ch in svc.characteristics:
                if "notify" not in ch.properties:
                    continue
                uuid_l = ch.uuid.lower()
                if uuid_l in known:
                    continue
                try:
                    await client.start_notify(ch.uuid, self._on_unknown_notify)
                    count += 1
                except Exception as e:
                    log.debug("notify subscribe failed for %s: %s", ch.uuid, e)
        if count:
            log.info("subscribed to %d additional notify chars for discovery", count)

    # ---------------- notification handlers ------------------------------------
    def _on_battery_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        if not data:
            return
        pct = data[0]
        log.info("battery notify: %d%%", pct)
        asyncio.run_coroutine_threadsafe(self.on_battery(pct), self.loop)

    def _on_data_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        if not data:
            return
        log.info("data notify (%dB): %s", len(data), data.hex())
        try:
            self._parse_and_emit(bytes(data))
        except Exception as e:
            log.exception("parse failed: %s", e)

    def _on_unknown_notify(self, char: BleakGATTCharacteristic, data: bytearray) -> None:
        uuid_l = char.uuid.lower()
        if uuid_l == CHAR_WEIGHT:
            self._handle_weight_notify(data)
            return
        if uuid_l == CHAR_CAP and len(data) >= 1:
            self._handle_cap_notify(data)
            return
        log.info("notify [%s] (%dB): %s", char.uuid, len(data), data.hex())

    # ---- weight + cap → refill detection ----
    # Refill heuristic: when the cap opens we snapshot the last stable upright
    # low-byte weight. After the cap closes we wait for the bottle to settle
    # upright again, then compare. A sustained low-byte rise of >= REFILL_DELTA
    # is treated as a refill event.
    REFILL_DELTA_LOW = 25  # raw units; ~25 mL on this firmware
    SETTLE_READINGS = 3    # consecutive upright samples within ±2 = settled
    SETTLE_TIMEOUT_S = 30  # max wait for settle after cap close

    def _handle_weight_notify(self, data: bytearray) -> None:
        if len(data) < 2:
            return
        raw = int.from_bytes(bytes(data[:2]), "big")
        high = data[0]
        low = data[1]
        # Only track upright/stable readings (high byte 0x8a observed in field).
        if high != 0x8a:
            self._weight_stable_streak = 0
            self._weight_last_low = None
            return
        # Settle detection: low byte within ±2 of previous reading.
        if self._weight_last_low is not None and abs(low - self._weight_last_low) <= 2:
            self._weight_stable_streak += 1
        else:
            self._weight_stable_streak = 1
        self._weight_last_low = low
        if self._weight_stable_streak >= self.SETTLE_READINGS:
            self._weight_stable_low = low
            if self.on_weight:
                asyncio.run_coroutine_threadsafe(
                    self.on_weight(raw, low), self.loop
                )

    def _handle_cap_notify(self, data: bytearray) -> None:
        # 0x81xxxxxx = cap opened, 0x80xxxxxx = cap closed.
        flag = data[0]
        if flag == 0x81 and not self._cap_open:
            self._cap_open = True
            self._pre_open_weight_low = self._weight_stable_low
            log.info("cap OPEN (pre-weight low=%s)", self._pre_open_weight_low)
        elif flag == 0x80 and self._cap_open:
            self._cap_open = False
            log.info("cap CLOSE — scheduling refill check")
            if self._refill_check_task and not self._refill_check_task.done():
                self._refill_check_task.cancel()
            self._refill_check_task = asyncio.run_coroutine_threadsafe(
                self._check_refill_after_close(), self.loop
            )  # type: ignore[assignment]

    async def _check_refill_after_close(self) -> None:
        """After cap closes, wait for a fresh stable upright weight then compare."""
        pre = self._pre_open_weight_low
        # Reset settle streak so we wait for a NEW stable reading.
        self._weight_stable_streak = 0
        baseline_stable = self._weight_stable_low
        deadline = time.monotonic() + self.SETTLE_TIMEOUT_S
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(1.0)
                # Stable changed since cap close → settled.
                if (
                    self._weight_stable_low is not None
                    and self._weight_stable_low != baseline_stable
                ):
                    break
        except asyncio.CancelledError:
            return
        post = self._weight_stable_low
        if pre is None or post is None:
            log.info("refill check skipped (pre=%s post=%s)", pre, post)
            return
        delta = post - pre
        log.info("refill check: pre=%d post=%d delta=%+d", pre, post, delta)
        if delta >= self.REFILL_DELTA_LOW and self.on_refill:
            asyncio.run_coroutine_threadsafe(
                self.on_refill("auto-cap", post), self.loop
            )

    def _parse_and_emit(self, data: bytes) -> None:
        if len(data) < 1:
            return
        remaining = data[0]
        if remaining == 0:
            return

        # Always re-drain when there's something pending. Some firmwares first
        # send a short "N pending" frame with all zeros after data[0]; the real
        # sip record arrives in the next notification triggered by 0x57.
        if self._client and self._client.is_connected and self._data_char:
            asyncio.run_coroutine_threadsafe(
                self._drain(self._client), self.loop
            )

        if len(data) < 9:
            return

        pct = data[1]
        # HydroSync: total_oz @2 BE u16, secondsAgo @5 BE u32
        total_reported = int.from_bytes(data[2:4], "big")
        seconds_ago = int.from_bytes(data[5:9], "big")
        # Sanity clamp
        if seconds_ago > 10 * 365 * 24 * 3600:
            seconds_ago = 0
        ts = time.time() - seconds_ago
        volume_ml = round(self.size_ml * pct / 100)
        if volume_ml <= 0:
            return

        log.info(
            "sip: %dml (pct=%d, total_reported=%d, %ds ago)",
            volume_ml, pct, total_reported, seconds_ago,
        )
        asyncio.run_coroutine_threadsafe(
            self.on_sip(ts, volume_ml, total_reported), self.loop
        )
