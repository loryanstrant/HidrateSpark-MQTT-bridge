"""Orchestrator wiring config + BLE + MQTT + web UI."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Optional

import uvicorn

from .ble import BottleClient
from .config import RuntimeState, Settings, load_settings, save_settings
from .mqtt import MqttPublisher
from .state import Sip, State
from .web import create_app

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("hidrate")


class Orchestrator:
    def __init__(self) -> None:
        self.settings: Settings = load_settings()
        self.state = State(
            bottle_size_ml=self.settings.bottle.size_ml,
            on_persist=self._persist_runtime,
        )
        self.state.hydrate_from_runtime(self.settings.runtime)
        self.loop = asyncio.get_event_loop()
        self.mqtt = MqttPublisher(
            self.settings.mqtt,
            self.settings.bottle.mac,
            bottle_name=f"HidrateSpark {self.settings.bottle.mac or ''}".strip(),
        )
        self.ble = BottleClient(
            mac=self.settings.bottle.mac,
            name_prefix=self.settings.bottle.name_prefix,
            size_ml=self.settings.bottle.size_ml,
            on_sip=self._on_sip,
            on_battery=self._on_battery,
            on_status=self._on_status,
            on_refill=self._on_refill,
            on_weight=self._on_weight,
            loop=self.loop,
        )

    async def _on_sip(self, ts: float, volume_ml: int, total_reported_ml: int) -> None:
        self.state.add_sip(Sip(timestamp=ts, volume_ml=volume_ml))
        try:
            self.mqtt.publish_sip(ts, volume_ml, total_reported_ml)
            self.mqtt.publish_totals(self.state.total_today(), self.state.lifetime_total_ml)
            snap = self.state.snapshot()
            self.mqtt.publish_current_fill(snap["current_fill_ml"], snap["current_fill_pct"])
        except Exception as e:
            log.warning("mqtt publish_sip failed: %s", e)

    def _persist_runtime(self) -> None:
        try:
            self.settings.runtime = RuntimeState(**self.state.to_runtime_dict())
            save_settings(self.settings)
        except Exception as e:
            log.warning("save settings failed: %s", e)

    async def _on_refill(self, source: str, weight_full_low: Optional[int] = None) -> None:
        self.state.refill(source=source, weight_full_low=weight_full_low)
        try:
            snap = self.state.snapshot()
            self.mqtt.publish_current_fill(snap["current_fill_ml"], snap["current_fill_pct"])
        except Exception as e:
            log.warning("mqtt publish refill failed: %s", e)

    async def _on_weight(self, raw_u16: int, low_byte: int) -> None:
        try:
            self.mqtt.publish_weight(raw_u16, low_byte)
        except Exception as e:
            log.debug("mqtt publish weight failed: %s", e)
        if self.state.update_weight(low_byte):
            try:
                snap = self.state.snapshot()
                self.mqtt.publish_current_fill(snap["current_fill_ml"], snap["current_fill_pct"])
            except Exception as e:
                log.debug("mqtt publish weight-fill failed: %s", e)

    async def _on_battery(self, pct: int) -> None:
        self.state.battery_pct = pct
        try:
            self.mqtt.publish_battery(pct)
        except Exception as e:
            log.warning("mqtt publish_battery failed: %s", e)

    async def _on_status(self, connected: bool, error: Optional[str]) -> None:
        self.state.connected = connected
        self.state.last_error = error
        if connected:
            self.state.handshake_path = self.ble.handshake_path
            import time
            self.state.last_seen = time.time()
        try:
            self.mqtt.publish_status(connected, error)
        except Exception as e:
            log.warning("mqtt publish_status failed: %s", e)

    async def run(self) -> None:
        # Start MQTT (no-op if disabled / no host)
        self.mqtt.start()

        app = create_app(self)
        web_cfg = uvicorn.Config(
            app,
            host=self.settings.web.host,
            port=int(os.environ.get("WEB_PORT", self.settings.web.port)),
            log_level="info",
            access_log=False,
        )
        server = uvicorn.Server(web_cfg)

        ble_task = asyncio.create_task(self.ble.run(), name="ble")
        web_task = asyncio.create_task(server.serve(), name="web")

        stop_event = asyncio.Event()

        def _signal():
            log.info("signal received, shutting down")
            stop_event.set()

        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                self.loop.add_signal_handler(s, _signal)
            except NotImplementedError:
                pass

        await stop_event.wait()
        log.info("stopping...")
        server.should_exit = True
        await self.ble.stop()
        self.mqtt.stop()
        await asyncio.gather(ble_task, web_task, return_exceptions=True)


def main() -> None:
    orch = Orchestrator()
    try:
        orch.loop.run_until_complete(orch.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
