# Pipette G-code Command Reference

Unified commands for **ADP1000** (Modbus RTU) and **SP20** (KT_OEM) pipette devices.  
The active device is selected by `device_type` in `[pipette]` — no command changes needed when switching.

---

## Command Overview

| Command | Module | Description |
|---------|--------|-------------|
| `PIPETTE_INIT` | pipette_driver | Home / initialize the device |
| `PIPETTE_READ_TIP` | pipette_driver | Read TIP presence (no Z movement) |
| `PIPETTE_LIQUID_DETECT` | pipette_driver | In-place liquid detection (no Z movement) |
| `PIPETTE_PICKUP_TIP` | pipette_action | Z-axis TIP pickup (Z moves down until TIP detected) |
| `PIPETTE_LIQUID_PROBE` | pipette_action | Z-axis liquid probe (Z moves down until liquid detected) |
| `PIPETTE_READ_TIP_PIN` | pipette_action | Read physical `tip_pin` state (pin mode only) |
| `PIPETTE_READ_LIQUID_PIN` | pipette_action | Read physical `liquid_pin` state (pin mode only) |
| `PIPETTE_PRESSURE_TEST` | pipette_action | Record ADS1220 force at rest or during a bounded Z descent |
| `PIPETTE_PRESSURE_GUARD_SET` | pipette_action | Change pressure limits for subsequent TIP/liquid probes |

---

## pipette_driver commands

### PIPETTE_INIT

Home / initialize the pipette device.

```
PIPETTE_INIT [WAIT=1] [WAIT_TIMEOUT=<s>]
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `WAIT` | 1 | 1 = wait for homing to complete before returning |
| `WAIT_TIMEOUT` | `action_timeout` | Maximum wait time in seconds |

**ADP1000:** Writes `REG_INIT_CMD (0x020C) = 1`, polls `REG_RESET_STATUS (0x020D)` until `0x0001` (OK).  
**SP20:** Sends `It16000,100,0`, polls status register (`Rr1`) until complete.

---

### PIPETTE_READ_TIP

Read TIP sensor state without moving Z.

```
PIPETTE_READ_TIP
```

**Response:** `TIP PRESENT` or `TIP MISSING`

**ADP1000:** Reads `REG_TIP_PRESENCE (0x0231)`. Value `0x0001` = present.  
**SP20:** Sends `Rr3`. Data `"1"` = present.

---

### PIPETTE_LIQUID_DETECT

Trigger in-place liquid detection without moving Z.

```
PIPETTE_LIQUID_DETECT [MODE=<0~5>] [WAIT=1] [WAIT_TIMEOUT=<s>]
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MODE` | 1 | ADP1000 pressure detection mode (SP20 ignores this) |
| `WAIT` | 1 | 1 = wait for detection result before returning |
| `WAIT_TIMEOUT` | `action_timeout` | Maximum wait time in seconds |

**ADP1000 MODE values:**

| MODE | Name | Description |
|------|------|-------------|
| 0 | Off | Disable detection |
| 1 | Pressure (default) | Standard gas pressure; works for 90% of liquids |
| 2 | Capacitive | Non-conductive liquids, oils, organic solvents |
| 3 | Precision prep | Prepare stable pressure baseline |
| 4 | Precision | High sensitivity; requires MODE=3 first |
| 5 | Precision bubble-through | Penetrates foam / bubble layers |

**SP20:** Sends `Ld1,<timeout_ms>` and polls `Rr1` until complete; MODE is ignored.

Starting liquid detection requires a TIP. `PIPETTE_LIQUID_DETECT`,
`PIPETTE_START_LIQUID_DETECT`, `PIPETTE_LIQUID_PROBE`, and a moving
`PIPETTE_PRESSURE_TEST SOURCE=LIQUID` read device TIP status before sensing.
Missing TIP or a failed TIP read aborts before sensing or Z descent. ADP1000
`PIPETTE_LIQUID_DETECT MODE=0` only turns detection off and remains available
without a TIP.

---

## pipette_action commands

These commands combine device sensing with Klipper Z-axis homing movement.  
Z descends continuously; a background timer polls the device sensor.  
When triggered, Z stops immediately and retracts.

### PIPETTE_PICKUP_TIP

Move Z downward until TIP is detected, then retract.

```
PIPETTE_PICKUP_TIP [INIT=0] [INIT_TIMEOUT=<s>]
                   [MAX_DIST=<mm>] [SPEED=<mm/s>]
                   [EXTRA_TRAVEL=<mm>]
                   [RETRACT=<mm>] [RETRACT_SPEED=<mm/s>]
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `INIT` | 0 | 1 = run `PIPETTE_INIT` before pickup |
| `INIT_TIMEOUT` | `init_timeout` | Homing completion timeout (s) |
| `MAX_DIST` | `max_distance` | Maximum Z descent (mm); abort if not triggered |
| `SPEED` | `probe_speed` | Z descent speed (mm/s) |
| `EXTRA_TRAVEL` | `tip_extra_travel` | Extra downward travel after trigger to seat TIP firmly |
| `RETRACT` | `tip_retract_dist` | Retract distance after trigger (mm) |
| `RETRACT_SPEED` | `retract_speed` | Retract speed (mm/s) |

**Trigger source:** With `tip_pin` configured, only the MCU pin is used for
the pre-check, Z stop, and post-trigger confirmation. Klipper does not poll
device TIP status during the move. Without the pin, device polling stops Z instead.
The RP2040 still reads device TIP status over RS485 to drive its output pin;
the pin is not an independent sensor. Both automated descent commands require
`min_safe_z`.

---

### PIPETTE_LIQUID_PROBE

Move Z downward until liquid is detected, then retract.

```
PIPETTE_LIQUID_PROBE [INIT=0] [INIT_TIMEOUT=<s>] [MODE=<0~5>]
                     [MAX_DIST=<mm>] [SPEED=<mm/s>]
                     [RETRACT=<mm>] [RETRACT_SPEED=<mm/s>]
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `INIT` | 0 | 1 = run `PIPETTE_INIT` before probe |
| `INIT_TIMEOUT` | `init_timeout` | Homing completion timeout (s) |
| `MODE` | `liquid_detect_mode` | ADP1000 pressure mode (SP20 ignores) |
| `MAX_DIST` | `max_distance` | Maximum Z descent (mm) |
| `SPEED` | `probe_speed` | Z descent speed (mm/s) |
| `RETRACT` | `liquid_retract_dist` | Retract distance after trigger (mm) |
| `RETRACT_SPEED` | `retract_speed` | Retract speed (mm/s) |

Before starting Z movement, liquid sensing is activated automatically  
(`start_liquid_probe` → ADP1000 writes `REG_PRESSURE_DETECT`; SP20 sends `Ld1,<timeout_ms>`).  
After Z movement completes, sensing is stopped automatically.
With `liquid_pin` configured, only PC13 is used for the pre-check, Z stop,
and post-trigger confirmation. Device liquid-status polling is used only when
the pin is absent. USB communication is still required to start and stop liquid
sensing. For SP20, `probe_timeout` must be at most 20 s and exceed
`MAX_DIST / SPEED` by more than 2 s.

### Optional ADS1220 pressure protection

Each source has a force limit (`pressure_guard_tip_force_g` or
`pressure_guard_liquid_force_g`) checked by the Klipper host. The limit is the
absolute change from a moving baseline. The selected TIP or liquid signal is
normal success; excess force stops with an error. A missing limit disables
that source's protection. The optional `pressure_guard_drift_g_s` defaults to 0.

Each `pressure_guard_*_clear_time` is a guaranteed contact-free period from
the start of that descent. A move too short to establish it is rejected before
moving. A slow baseline drift may
be compensated up to `pressure_guard_drift_g_s` only while readings remain far
below the threshold. A sustained extra force stops Z and raises a protection
error; it does not run `EXTRA_TRAVEL` or report normal pipette success. The
selected TIP or liquid detection ends the guard; TIP seating after detection
is outside it. The selected sensor and host force guard can each stop Z.
Determine limits and stopping distance from traces before relying on the guard.

### PIPETTE_PRESSURE_GUARD_SET

This built-in G-code changes the limits for the **next and later** probes in
the current Klipper session. A restart restores the cfg values. No arguments
prints the active values. Settings for both sources update together only after
all supplied values pass validation.

```gcode
PIPETTE_PRESSURE_GUARD_SET
PIPETTE_PRESSURE_GUARD_SET TIP_FORCE_G=250
PIPETTE_PRESSURE_GUARD_SET LIQUID_FORCE_G=5
PIPETTE_PRESSURE_GUARD_SET TIP_CLEAR_TIME=0.15 LIQUID_CLEAR_TIME=0.15 DRIFT_G_S=0
```

`*_FORCE_G=0` disables that source's force guard. `*_CLEAR_TIME` must be
0.15–5 s; force values above zero may be at most 5000 g. The former
`*_MCU_FORCE_G` parameters are no longer supported.

The protected `PIPETTE_PICKUP_TIP` and `PIPETTE_LIQUID_PROBE` commands save a
force CSV in `pipette_pressure_reports/` beside `klippy.log` when the protected
descent raises an error. The console prints `PIPETTE_PRESSURE_GUARD:` with
`stop_reason=...`, reported end Z, sample count, and CSV
path. The CSV Z column is estimated. A normal sensor trigger does not create
this error CSV. The filename identifies `guard-tip` or `guard-liquid`, and each
sample row contains the original command, stop reason and detail, effective
motion settings, force limit, baseline, target Z, `min_safe_z`, and reported
start/end Z. The separate `PIPETTE_PRESSURE_TEST` command does not stop on force.

### PIPETTE_PRESSURE_TEST

Record a stationary trace or a bounded descent without installing a pressure
threshold. The selected pipette sensor remains the descent endstop. A normal
no-contact test may reach `DIST` without triggering. The command never performs
extra downward travel or retracts automatically.

```
PIPETTE_PRESSURE_TEST DIST=0 HOLD=1
PIPETTE_PRESSURE_TEST DIST=20 SPEED=5 SOURCE=TIP
PIPETTE_PRESSURE_TEST DIST=20 SPEED=5 SOURCE=LIQUID
```

`DIST` is 0–100 mm; a moving test may last at most 30 s. `HOLD` (0.2–10 s)
applies only when `DIST=0`. Z must be homed and `min_safe_z` must be configured
for a moving test. The command checks that limit and refuses an already triggered selected
sensor. Its console summary reports the baseline, moving sample count, force
range, sampling errors, and stop reason. A CSV is written in
`pipette_pressure_reports/` beside `klippy.log` with sample time, raw counts,
grams, and an **estimated** Z position. That Z
estimate uses commanded speed and is approximate during acceleration and
stopping. If a short move has no moving ADS1220 samples, the summary says so;
no motion baseline can be inferred from that trace. Its filename identifies
`test-tip` or `test-liquid`; each sample row includes the original command,
effective settings, trigger source, stop reason, and reported start/end Z.

---

### PIPETTE_READ_TIP_PIN

Read current state of the physical `tip_pin` endstop.  
Only available when `tip_pin` is configured in `[pipette_action]`.

```
PIPETTE_READ_TIP_PIN
```

**Response:** `state=1(TRIGGERED)` or `state=0(OPEN)`

---

### PIPETTE_READ_LIQUID_PIN

Read current state of the physical `liquid_pin` endstop.  
Only available when `liquid_pin` is configured in `[pipette_action]`.

```
PIPETTE_READ_LIQUID_PIN
```

**Response:** `state=1(TRIGGERED)` or `state=0(OPEN)`

---

## Device Behavior Differences

| Feature | ADP1000 | SP20 |
|---------|---------|------|
| Protocol | Modbus RTU (binary) | KT_OEM (binary frame + ASCII payload) |
| RS485 baud | 115200 | 38400 |
| TIP detection | `REG_TIP_PRESENCE (0x0231)` = 0x0001 | `Rr3` returns `"1"` |
| Liquid detection | `REG_DEVICE_STATUS (0x0204)` = 0x0003 | `Rr2` returns `"1"` |
| Liquid sensing activation | `REG_PRESSURE_DETECT (0x0221)` = MODE | `Ld1,<timeout_ms>` command |
| Init command | `REG_INIT_CMD (0x020C)` = 1 | `It16000,100,0` |
| Abort condition | `DEVICE_STATUS (0x0204)` = 0x0004 (max dist) | Error state ≥ 10 in status register |
| `PIPETTE_LIQUID_DETECT MODE=` | Selects pressure mode 0~5 | Ignored |

---

## Quick Test Sequence

```
# 1. Verify connection
PIPETTE_INIT WAIT=1

# 2. Check TIP sensor
PIPETTE_READ_TIP

# 3. Z-axis TIP pickup
PIPETTE_PICKUP_TIP MAX_DIST=30

# 4. In-place liquid test (requires TIP)
PIPETTE_LIQUID_DETECT MODE=1 WAIT=1

# 5. Z-axis liquid probe (requires TIP)
PIPETTE_LIQUID_PROBE SPEED=2.0 RETRACT=3.0
```
