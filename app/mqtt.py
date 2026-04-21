"""MQTT publisher with Home Assistant discovery."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import paho.mqtt.client as mqtt

from .config import MqttConfig

log = logging.getLogger(__name__)


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s).strip("_") or "hidrate"


class MqttPublisher:
    def __init__(self, cfg: MqttConfig, bottle_mac: str, bottle_name: str = "HidrateSpark"):
        self.cfg = cfg
        self.bottle_mac = bottle_mac or "unknown"
        self.bottle_name = bottle_name
        self._client: Optional[mqtt.Client] = None
        self._connected = False
        self._lock = threading.Lock()
        self._device_id = _slug(self.cfg.client_id + "_" + self.bottle_mac.replace(":", ""))

    @property
    def base(self) -> str:
        return self.cfg.base_topic.rstrip("/") or "hidrate"

    @property
    def availability_topic(self) -> str:
        return f"{self.base}/availability"

    @property
    def connected(self) -> bool:
        return self._connected

    # -------- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            self._stop_locked()
            if not self.cfg.enabled or not self.cfg.host:
                log.info("MQTT disabled or no host configured")
                return
            try:
                client = mqtt.Client(
                    client_id=self.cfg.client_id,
                    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                )
            except AttributeError:  # paho-mqtt < 2
                client = mqtt.Client(client_id=self.cfg.client_id)
            if self.cfg.username:
                client.username_pw_set(self.cfg.username, self.cfg.password or None)
            if self.cfg.tls:
                client.tls_set()
            client.will_set(self.availability_topic, "offline", retain=True)
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            try:
                client.connect_async(self.cfg.host, self.cfg.port, keepalive=60)
                client.loop_start()
                self._client = client
                log.info("MQTT connecting to %s:%d", self.cfg.host, self.cfg.port)
            except Exception as e:
                log.exception("MQTT connect failed: %s", e)

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        if self._client is not None:
            try:
                if self._connected:
                    self._client.publish(self.availability_topic, "offline", retain=True)
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None
            self._connected = False

    def update_config(self, cfg: MqttConfig, bottle_mac: str) -> None:
        with self._lock:
            self.cfg = cfg
            self.bottle_mac = bottle_mac or self.bottle_mac
            self._device_id = _slug(cfg.client_id + "_" + self.bottle_mac.replace(":", ""))
        self.start()

    # -------- callbacks ---------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if hasattr(reason_code, "is_failure") and reason_code.is_failure:
            log.error("MQTT connect failed: %s", reason_code)
            return
        log.info("MQTT connected")
        self._connected = True
        client.publish(self.availability_topic, "online", retain=True)
        if self.cfg.ha_discovery:
            self._publish_discovery()

    def _on_disconnect(self, client, userdata, *args, **kwargs):
        log.warning("MQTT disconnected")
        self._connected = False

    # -------- publish helpers --------------------------------------------------
    def _publish(self, topic: str, payload, retain: bool = False) -> None:
        if not (self._client and self._connected):
            return
        if not isinstance(payload, (str, bytes, bytearray, int, float)):
            payload = json.dumps(payload, separators=(",", ":"))
        try:
            self._client.publish(topic, payload, retain=retain, qos=0)
        except Exception as e:
            log.warning("MQTT publish to %s failed: %s", topic, e)

    def publish_battery(self, pct: int) -> None:
        self._publish(f"{self.base}/battery", pct, retain=True)

    def publish_status(self, connected: bool, error: Optional[str]) -> None:
        # Bottle connection status (separate from MQTT availability)
        self._publish(
            f"{self.base}/bottle_status",
            {"connected": connected, "error": error, "ts": time.time()},
            retain=True,
        )

    def publish_sip(self, ts: float, volume_ml: int, total_reported_ml: int) -> None:
        payload = {
            "timestamp": ts,
            "iso": datetime.fromtimestamp(ts, tz=timezone.utc)
                .astimezone()
                .isoformat(timespec="seconds"),
            "volume_ml": volume_ml,
            "device_total_ml": total_reported_ml,
        }
        self._publish(f"{self.base}/sip_event", payload, retain=False)
        self._publish(f"{self.base}/last_sip", payload, retain=True)

    def publish_totals(self, total_today_ml: int, lifetime_total_ml: int) -> None:
        self._publish(f"{self.base}/total_today", total_today_ml, retain=True)
        self._publish(f"{self.base}/total_lifetime", lifetime_total_ml, retain=True)

    def publish_current_fill(self, fill_ml: int, fill_pct: int) -> None:
        self._publish(f"{self.base}/current_fill", fill_ml, retain=True)
        self._publish(f"{self.base}/current_fill_pct", fill_pct, retain=True)

    def publish_weight(self, raw_u16: int, low_byte: int) -> None:
        """Raw 2-byte sensor value from char 1807a063 (settled, upright)."""
        self._publish(f"{self.base}/weight_raw", raw_u16, retain=True)
        self._publish(f"{self.base}/weight_low", low_byte, retain=True)

    # -------- HA discovery -----------------------------------------------------
    def _publish_discovery(self) -> None:
        prefix = self.cfg.ha_discovery_prefix.rstrip("/")
        device = {
            "identifiers": [self._device_id, self.bottle_mac],
            "name": self.bottle_name,
            "manufacturer": "Hidrate",
            "model": "HidrateSpark",
            "connections": [["mac", self.bottle_mac]] if self.bottle_mac else [],
            "configuration_url": "https://github.com/loryanstrant/HidrateSpark-MQTT-bridge",
        }
        avail = [{"topic": self.availability_topic}]

        sensors = [
            {
                "key": "battery",
                "name": "Battery",
                "state_topic": f"{self.base}/battery",
                "unit_of_measurement": "%",
                "device_class": "battery",
                "state_class": "measurement",
            },
            {
                "key": "total_today",
                "name": "Water Today",
                "state_topic": f"{self.base}/total_today",
                "unit_of_measurement": "mL",
                "icon": "mdi:cup-water",
                "state_class": "total_increasing",
            },
            {
                "key": "total_lifetime",
                "name": "Water Lifetime",
                "state_topic": f"{self.base}/total_lifetime",
                "unit_of_measurement": "mL",
                "icon": "mdi:water",
                "state_class": "total_increasing",
            },
            {
                "key": "current_fill",
                "name": "Current Fill",
                "state_topic": f"{self.base}/current_fill",
                "unit_of_measurement": "mL",
                "icon": "mdi:bottle-tonic",
                "state_class": "measurement",
            },
            {
                "key": "current_fill_pct",
                "name": "Current Fill Percent",
                "state_topic": f"{self.base}/current_fill_pct",
                "unit_of_measurement": "%",
                "icon": "mdi:bottle-tonic-outline",
                "state_class": "measurement",
            },
            {
                "key": "weight_raw",
                "name": "Bottle Weight Raw",
                "state_topic": f"{self.base}/weight_raw",
                "icon": "mdi:scale",
                "state_class": "measurement",
                "entity_category": "diagnostic",
            },
            {
                "key": "last_sip_volume",
                "name": "Last Sip Volume",
                "state_topic": f"{self.base}/last_sip",
                "value_template": "{{ value_json.volume_ml }}",
                "unit_of_measurement": "mL",
                "icon": "mdi:cup",
            },
            {
                "key": "last_sip_time",
                "name": "Last Sip Time",
                "state_topic": f"{self.base}/last_sip",
                "value_template": "{{ value_json.iso }}",
                "device_class": "timestamp",
                "icon": "mdi:clock",
            },
        ]
        for s in sensors:
            uniq = f"{self._device_id}_{s['key']}"
            cfg_topic = f"{prefix}/sensor/{self._device_id}/{s['key']}/config"
            payload = {
                "name": s["name"],
                "unique_id": uniq,
                "object_id": uniq,
                "state_topic": s["state_topic"],
                "availability": avail,
                "device": device,
            }
            for opt in (
                "unit_of_measurement",
                "device_class",
                "state_class",
                "icon",
                "value_template",
            ):
                if opt in s:
                    payload[opt] = s[opt]
            self._publish(cfg_topic, payload, retain=True)

        # Connectivity binary sensor for the bottle
        bs_uniq = f"{self._device_id}_connected"
        bs_topic = f"{prefix}/binary_sensor/{self._device_id}/connected/config"
        self._publish(
            bs_topic,
            {
                "name": "Bottle Connected",
                "unique_id": bs_uniq,
                "object_id": bs_uniq,
                "state_topic": f"{self.base}/bottle_status",
                "value_template": "{{ 'ON' if value_json.connected else 'OFF' }}",
                "device_class": "connectivity",
                "availability": avail,
                "device": device,
            },
            retain=True,
        )
        log.info("HA discovery published")
