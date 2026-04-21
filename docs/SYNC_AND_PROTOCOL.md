# HidrateSpark — BLE protocol notes & sync model

This document captures everything we've reverse-engineered about the
HidrateSpark BLE interface as implemented by this bridge, plus the data-flow
model so other projects can benefit. All measurements were taken against
firmware **`80.18`** on the **nRF52832** chipset (HidrateSpark "Steel" / "PRO").
Older or newer firmwares may differ.

## Bluetooth handshake

The bottle ships sip data on either a "modern" characteristic
(`bf2d1ba1-c473-49f2-9571-0ce69036c642`) or a "legacy" one
(`016e11b1-6c8a-4074-9e5a-076053f93784`). On firmware `80.18` the modern
characteristic is **absent**, so we fall back to legacy.

Either way, the bottle stays silent until you perform a 13-step handshake
(originally documented by [HydroSync](https://github.com/maxperron/HydroSync)).
Writes are split between two characteristics in service `45855422-…`:

| # | Characteristic | UUID | Hex payload |
|---|---|---|---|
| 1 | DEBUG | `e3578b0d-…` | `2100d1` |
| 2 | SET_POINT | `b44b03f0-…` | `92` |
| 3 | DEBUG | `e3578b0d-…` | `2200f7` |
| 4..13 | SET_POINT | `b44b03f0-…` | (see [`app/ble.py`](../app/ble.py) `HANDSHAKE_COMMANDS`) |

Each write is 50 ms apart. After the last write, the bottle starts streaming
sip notifications on the data characteristic.

## Sip frames

Sips arrive as 20-byte notifications on the data characteristic. Layout:

```
byte  0    : N pending sips remaining (0..255)
bytes 1    : record type / flags
bytes 2..3 : total reported volume so far, big-endian u16 (mL)
bytes 4..7 : sip timestamp, big-endian u32 (Unix epoch seconds)
bytes 8..9 : sip volume, big-endian u16 (mL)
bytes 10.. : padding / unknown
```

### The "drain" trick

The first frame after subscribing is **always all-zeros with N=0**. To make
the bottle send the buffered sips, write a single-byte `0x57` to the same
characteristic. After every notification where `N > 0`, drain again — that
acks the record and asks for the next one. Stop draining when `N == 0`.

This bridge does the drain unconditionally on every `data[0] > 0` (not just
on successful parse), which fixed a class of bugs where the very first
real-data frame had a non-standard layout and would otherwise stall the queue.

## Cap state

Characteristic `e3578b0d-caa7-46d6-b7c2-7331c08de044` (the same one we use
for DEBUG handshake writes) **also doubles as a notify channel for cap state**:

| Notification | Meaning |
|---|---|
| `81 02 00 00` | Cap **opened** |
| `80 02 00 00` | Cap **closed** |

Bit 0 of byte 0 is the open/closed flag (1 = open, 0 = closed).

## Weight sensor

Characteristic `1807a063-4e2d-4636-981a-35e93d1c7b94` (in service
`f65399a1-…`) emits 2-byte big-endian u16 notifications roughly every 2 s.
Decoded as a **(orientation, weight)** pair:

| High byte | Meaning |
|---|---|
| `0x8a` | Bottle upright & stable |
| `0x84` | Bottle tilted / lifted |
| `0x88` | Transient (settling) |

The **low byte** correlates with fluid weight on a roughly **1 unit ≈ 1 mL**
scale. (Field calibration: a `+189` low-byte rise corresponded to ~190 mL of
water added.)

Only the *low byte under high byte `0x8a`* is meaningful for fill tracking
— other orientations give garbage.

## Refill detection

We use a two-signal heuristic:

1. **Cap open notify** (`81…`) → snapshot the most recent stable upright
   weight (`pre`).
2. **Cap close notify** (`80…`) → schedule a settle check.
3. Wait up to 30 s for a *new* stable upright reading (`post`),
   measured as 3 consecutive samples within ±2 raw units.
4. If `post − pre ≥ 25` raw units (~25 mL) → emit **REFILL**.

Implementation: [`app/ble.py`](../app/ble.py) → `BottleClient._handle_cap_notify` and `_check_refill_after_close`.

A negative or near-zero delta means "you opened the cap to drink" or "to
inspect" — no refill emitted.

## Fill-level calculation

Once a refill is observed, we treat the post-refill `weight_low` value as
the **`weight_full_low`** anchor (= 100 % full). Persisted in
`config/config.yaml` under `runtime.weight_full_low`.

For every subsequent stable upright reading:

```
delta_units = weight_full_low - current_low      # positive when you've drunk
current_fill_ml = clamp(0, bottle_size_ml, bottle_size_ml - delta_units)
current_fill_pct = round(100 * current_fill_ml / bottle_size_ml)
```

Until the first refill happens after install, `weight_full_low` is `null`
and the bridge falls back to a sip-decrement estimate
(`current_fill_ml -= sip_volume_ml`). The first auto-refill switches
permanently to the weight-based reading.

## Other interesting characteristics

| UUID | Service | Notes |
|---|---|---|
| `0x2A19` | Battery (`0x180F`) | Standard battery %, also notifies on change |
| `0x2A6E` | Environmental Sensing (`0x181A`) | Temperature, notify-only — not yet used |
| `a1d9a5bf-…` | LED (`4f817071-…`) | LED control, write-only |
| `b810e826-…` | LED service | LED state read-back (`01000000` observed) |
| `316c4914-…` | Reference (`45855422-…`) | 6-byte read/write, contents unknown |
| `6e400003-…` | Nordic UART | Notify, never observed firing on `80.18` |
| `d3d46a35-…` | `75c276c3-…` | Notify, never observed firing |
| `230c4427-…` | `22669e4c-…` | Notify, never observed firing |
| `da2e7828-…` | `8d53dc1d-…` | Notify, never observed firing |

The four "never observed firing" notify chars are subscribed by the bridge
anyway, with raw-byte logging at `INFO` level — useful for further
reverse-engineering on firmwares we haven't seen.

## Offline behaviour

When the bottle is out of BLE range, this is what happens to each data
stream — and what is recovered on reconnect:

| Data | Buffered on bottle? | Recovered on reconnect? | Notes |
|---|---|---|---|
| Sips (volume + timestamp) | ✅ Yes | ✅ Fully | Replayed via the drain mechanism with original timestamps. Daily totals land in the correct day. |
| Daily total | derived | ✅ Correct | Recomputed from replayed sips. |
| Lifetime total | derived | ✅ Correct | Persisted in `config.yaml`, incremented as replays happen. |
| Battery | snapshot | ✅ Correct | Re-read on connect; also notifies on change. |
| Current fill | live sensor | ✅ Correct (after ~2 s) | First stable upright reading after reconnect snaps to true value. |
| Cap open / close events | notify-only | ❌ Lost | BLE notifications are not buffered. |
| Refill events that happened while away | notify-only | ❌ Lost | We can't reconstruct *when* the refill happened, but the resulting fill level is recovered. |

### Sip de-duplication

Each replayed sip is checked against the most recent 10 records: if the same
`(timestamp, volume_ml)` already exists, it's silently dropped. This means a
partial resync that re-delivers a few sips is safe — no double counting.

### Day rollover

`total_today_ml` is keyed by `today_date` in the persisted runtime block.
On the first sip of a new day, the counter resets atomically. If the
container is offline at midnight, the first sip after midnight resets the
counter — sips that happened before midnight are not retroactively moved
between days (they go into "today").

## State persistence

Everything in this table survives a `docker compose down && docker compose up`:

| Field | Where | Source |
|---|---|---|
| `current_fill_ml` | `config.yaml` runtime | sips + weight sensor |
| `lifetime_total_ml` | `config.yaml` runtime | accumulated from sips |
| `today_date` + `total_today_ml` | `config.yaml` runtime | sip aggregation, day-bucketed |
| `last_refill_ts` | `config.yaml` runtime | auto-cap detection |
| `weight_full_low` | `config.yaml` runtime | latched on each refill |
| `bottle.size_ml` | `config.yaml` (user) | manual |

Sip *history* (the recent-sips list shown in the dashboard) is in-memory
only — it is **not** persisted. After a restart the dashboard list starts
empty even though totals are correct.

## Single-connection constraint

A HidrateSpark bottle accepts **one BLE central at a time**. If the
HidrateSpark phone app, this bridge, and a debugging tool all try to
connect, only the first one wins; the others get
`BleakDeviceNotFoundError` or similar.

Practical implications:

- If you want to keep using the phone app for occasional firmware updates,
  force-quit it before relying on the bridge.
- You cannot run a second copy of this bridge against the same bottle.
- To do any GATT exploration of your own, stop the bridge container first
  (or instrument it from inside, like the discovery mode we ship in
  `BottleClient._subscribe_unknown_notifies`).
