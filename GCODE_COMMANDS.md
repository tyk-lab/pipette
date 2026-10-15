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

Both descent commands can use `[pipette_action]` settings
`pressure_guard_force_g`, `pressure_guard_clear_time`, and optionally
`pressure_guard_drift_g_s` (default 0). The guard is disabled when
`pressure_guard_force_g` is absent. The force limit is the **extra** absolute
force above a motion baseline, in grams, not the total ADC reading.

`pressure_guard_clear_time` is a guaranteed contact-free period from the start
of descent. The guard takes its baseline near the end of that period. A move
too short to establish it is rejected before moving. A slow baseline drift may
be compensated up to `pressure_guard_drift_g_s` only while readings remain far
below the threshold. A sustained extra force stops Z and raises a protection
error; it does not run `EXTRA_TRAVEL` or report normal pipette success. A pipette
pipette detection takes priority if both arrive together. ADS1220 measurements are
batched, so protection has more stopping delay than an MCU pin. Determine the
clear period, drift limit, force limit, and stopping distance from traces before
enabling the guard.

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
range, sampling errors, and stop reason. A CSV is written beside `klippy.log`
with sample time, raw counts, grams, and an **estimated** Z position. That Z
estimate uses commanded speed and is approximate during acceleration and
stopping. If a short move has no moving ADS1220 samples, the summary says so;
no motion baseline can be inferred from that trace.

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

# 3. In-place liquid test (ADP1000 with pressure mode 1)
PIPETTE_LIQUID_DETECT MODE=1 WAIT=1

# 4. Z-axis TIP pickup
PIPETTE_PICKUP_TIP MAX_DIST=30

# 5. Z-axis liquid probe
PIPETTE_LIQUID_PROBE SPEED=2.0 RETRACT=3.0
```
