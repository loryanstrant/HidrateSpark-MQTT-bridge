"""In-memory state for sips and totals."""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, date, timezone
from threading import Lock
from typing import Callable, Deque, List, Optional

log = logging.getLogger(__name__)


@dataclass
class Sip:
    timestamp: float  # unix seconds
    volume_ml: int

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "iso": datetime.fromtimestamp(self.timestamp, tz=timezone.utc)
                .astimezone()
                .isoformat(timespec="seconds"),
            "volume_ml": self.volume_ml,
        }


class State:
    def __init__(
        self,
        max_sips: int = 500,
        bottle_size_ml: int = 591,
        on_persist: Optional[Callable[[], None]] = None,
    ):
        self._lock = Lock()
        self.sips: Deque[Sip] = deque(maxlen=max_sips)
        self.lifetime_total_ml: int = 0
        self._total_today_ml: int = 0
        self._today_date: str = ""
        self.battery_pct: Optional[int] = None
        self.connected: bool = False
        self.last_error: Optional[str] = None
        self.last_handshake_ok: Optional[bool] = None
        self.handshake_path: Optional[str] = None
        self.last_seen: Optional[float] = None
        self.bottle_size_ml: int = bottle_size_ml
        self.current_fill_ml: int = bottle_size_ml
        self.last_refill_ts: Optional[float] = None
        # Weight-based fill: full anchor + most recent reading.
        self.weight_full_low: Optional[int] = None
        self.weight_low: Optional[int] = None
        self._on_persist = on_persist

    # --- persistence helpers ---------------------------------------------------
    def hydrate_from_runtime(self, runtime) -> None:
        """Restore from RuntimeState pydantic model."""
        with self._lock:
            if runtime.current_fill_ml is not None:
                self.current_fill_ml = max(0, min(runtime.current_fill_ml, self.bottle_size_ml))
            self.lifetime_total_ml = runtime.lifetime_total_ml or 0
            self.last_refill_ts = runtime.last_refill_ts
            self._today_date = runtime.today_date or ""
            self._total_today_ml = runtime.total_today_ml or 0
            self.weight_full_low = getattr(runtime, "weight_full_low", None)

    def to_runtime_dict(self) -> dict:
        with self._lock:
            return {
                "current_fill_ml": self.current_fill_ml,
                "lifetime_total_ml": self.lifetime_total_ml,
                "last_refill_ts": self.last_refill_ts,
                "today_date": self._today_date,
                "total_today_ml": self._total_today_ml,
                "weight_full_low": self.weight_full_low,
            }

    def _persist(self) -> None:
        if self._on_persist:
            try:
                self._on_persist()
            except Exception as e:
                log.warning("persist failed: %s", e)

    # --- mutations -------------------------------------------------------------
    def set_bottle_size(self, size_ml: int) -> None:
        with self._lock:
            self.bottle_size_ml = size_ml
            if self.current_fill_ml > size_ml:
                self.current_fill_ml = size_ml
        self._persist()

    def refill(self, source: str = "manual", weight_full_low: Optional[int] = None) -> None:
        with self._lock:
            self.current_fill_ml = self.bottle_size_ml
            self.last_refill_ts = time.time()
            if weight_full_low is not None:
                self.weight_full_low = weight_full_low
        log.info("REFILL (%s) -> %dml (full_low=%s)",
                 source, self.bottle_size_ml, self.weight_full_low)
        self._persist()

    def update_weight(self, low_byte: int) -> bool:
        """Update current fill from a stable upright weight reading.

        Returns True if current_fill_ml was changed."""
        with self._lock:
            self.weight_low = low_byte
            if self.weight_full_low is None:
                return False
            # 1 raw unit ≈ 1 mL; clamp to [0, bottle_size_ml].
            delta = self.weight_full_low - low_byte
            new_fill = max(0, min(self.bottle_size_ml, self.bottle_size_ml - delta))
            if new_fill == self.current_fill_ml:
                return False
            self.current_fill_ml = new_fill
        self._persist()
        return True

    def _roll_day_locked(self) -> None:
        today = date.today().isoformat()
        if self._today_date != today:
            self._today_date = today
            self._total_today_ml = 0

    def add_sip(self, sip: Sip) -> None:
        refilled = False
        with self._lock:
            for existing in list(self.sips)[-10:]:
                if (
                    abs(existing.timestamp - sip.timestamp) < 2
                    and existing.volume_ml == sip.volume_ml
                ):
                    return
            self.sips.append(sip)
            self.lifetime_total_ml += sip.volume_ml
            self._roll_day_locked()
            self._total_today_ml += sip.volume_ml
            self.current_fill_ml -= sip.volume_ml
            if self.current_fill_ml < 0:
                # Sip exceeds current fill: bottle was clearly refilled at some
                # point; reset to full minus this sip.
                self.current_fill_ml = max(0, self.bottle_size_ml - sip.volume_ml)
                self.last_refill_ts = sip.timestamp
                refilled = True
        if refilled:
            log.info("REFILL (auto: sip exceeded fill) -> %dml after %dml sip",
                     self.bottle_size_ml, sip.volume_ml)
        self._persist()

    def total_today(self) -> int:
        with self._lock:
            self._roll_day_locked()
            return self._total_today_ml

    def recent_sips(self, n: int = 50) -> List[dict]:
        with self._lock:
            return [s.to_dict() for s in list(self.sips)[-n:][::-1]]

    def snapshot(self) -> dict:
        with self._lock:
            last = self.sips[-1].to_dict() if self.sips else None
            self._roll_day_locked()
            today_total = self._total_today_ml
        return {
            "connected": self.connected,
            "battery_pct": self.battery_pct,
            "total_today_ml": today_total,
            "lifetime_total_ml": self.lifetime_total_ml,
            "bottle_size_ml": self.bottle_size_ml,
            "current_fill_ml": self.current_fill_ml,
            "current_fill_pct": (
                round(100 * self.current_fill_ml / self.bottle_size_ml)
                if self.bottle_size_ml else 0
            ),
            "last_refill_ts": self.last_refill_ts,
            "last_sip": last,
            "last_error": self.last_error,
            "handshake_path": self.handshake_path,
            "last_seen": self.last_seen,
            "now": time.time(),
        }
