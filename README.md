# Klipper Pipette Driver

Unified Klipper extras for **ADP1000** (Modbus RTU) and **SP20** (KT_OEM) pipette devices.  
Switch between devices by changing a single `device_type` line — no command changes needed.

---

## Architecture

```
Klipper (Linux host)
    │
    ├── pipette_driver.py   ← [pipette] section — protocol driver, 3 G-code commands
    │       ├── adp1000_backend.py  (loaded when device_type: adp1000)
    │       └── sp20_backend.py     (loaded when device_type: sp20)
    │
    └── pipette_action.py   ← [pipette_action] section — Z-axis movement and pressure test
            (calls pipette_driver public API; device-agnostic)
                │
                │  USB CDC (115200 baud — constant)
                ▼
          RP2040 firmware
                │  RS485 (115200 baud for ADP1000 / 38400 baud for SP20)
                ▼
          ADP1000 / SP20 device
```

The RP2040 firmware handles the RS485 baud rate difference internally.  
Klipper always communicates with the RP2040 at 115200 baud over USB CDC.

---

## Files

| File | Purpose |
|------|---------|
| `pipette_driver.py` | Klipper module entry — reads config, opens serial, loads backend, registers 3 G-code commands |
| `adp1000_backend.py` | ADP1000 Modbus RTU protocol (FC03/FC06, CRC16, register map) |
| `sp20_backend.py` | SP20 KT_OEM binary frame protocol (frame builder/parser, state machine) |
| `pipette_action.py` | Z-axis TIP pickup and liquid probe with software/hardware endstop |
| `pipette_pressure_guard.py` | Optional ADS1220 extra-force stop during Z descent |
| `pipette_pressure_trace.py` | Bounded ADS1220 trace and CSV export |
| `load_cell_probe.py` | Klipper load-cell module with `get_load_cell()` for pipette pressure sensing |
| `pipette.cfg` | Example configuration — include in `printer.cfg` |
| `flash_rp2040_bootsel.sh` | Helper script to flash RP2040 firmware via BOOTSEL |

---

## Installation

### 1. Flash RP2040 firmware

Select the matching device macro in `config.h` before compiling:

```c
// For ADP1000:
#define DEVICE_ADP1000

// For SP20:
// #define DEVICE_SP20
```

Also ensure USB passthrough is enabled:

```c
#define USB_PASSTHROUGH_ENABLED
```

Flash the firmware:

```bash
chmod +x flash_rp2040_bootsel.sh
./flash_rp2040_bootsel.sh
```

### 2. Deploy extras to Klipper

```bash
./deploy_to_klipper.sh
```

The script links this directory's plugin modules, including
`load_cell_probe.py`, into `~/klipper/klippy/extras/` and restarts Klipper.
Existing regular plugin files get timestamped `.bak` copies. It does not
check the Klipper Git version or modify `klippy/mcu.py` and
`klippy/extras/trigger_analog.py`.

TIP and liquid pressure protection runs in the pipette plugin and needs no
Klipper core changes. Remove any old `pressure_guard_*_mcu_force_g` options
from the active `pipette.cfg` before restarting Klipper.

### 3. Configure printer.cfg

```ini
[include pipette.cfg]
```

Edit `pipette.cfg` to set the correct serial port and device type:

```ini
[pipette]
device_type: adp1000          # or: sp20
serial: /dev/serial/by-id/usb-LDO_MOTORS-if00
device_addr: 1
```

### 4. Restart Klipper

```
FIRMWARE_RESTART
```

---

## Switching Devices

Change exactly one line in `pipette.cfg`:

```ini
device_type: adp1000    # ← change to sp20 for SP20 device
```

Then restart Klipper. No G-code commands or `[pipette_action]` parameters need to change.

---

## Quick Test

Paste these commands into the Klipper console (Mainsail / Fluidd terminal):

```
# 1. Verify connection and initialize
PIPETTE_INIT WAIT=1

# 2. Check TIP sensor
PIPETTE_READ_TIP

# 3. Z-axis TIP pickup (moves Z down until TIP detected)
PIPETTE_PICKUP_TIP MAX_DIST=30

# 4. In-place liquid detection (requires TIP; SP20 ignores MODE)
PIPETTE_LIQUID_DETECT MODE=1 WAIT=1

# 5. Z-axis liquid probe (requires TIP; moves Z down until liquid detected)
PIPETTE_LIQUID_PROBE SPEED=2.0 RETRACT=3.0
```

See [GCODE_COMMANDS.md](GCODE_COMMANDS.md) for the full command reference.

---

## ADS1220 Pressure Test and Protection

Pressure sensing requires a configured `[load_cell_probe]` using ADS1220, plus a
calibrated and tared load cell. Both the test command and the optional protection
check these conditions before use. The deployed `load_cell_probe.py` exposes the
sensor to `pipette_action.py`.

### Measure force before enabling protection

`PIPETTE_PRESSURE_TEST` records ADS1220 force for choosing protection settings.
It is a diagnostic command: **it does not enable the force limit or stop Z on
force alone**. Use it first at rest, then during a bounded descent:

```gcode
PIPETTE_PRESSURE_TEST DIST=0 HOLD=1
PIPETTE_PRESSURE_TEST DIST=20 SPEED=5 SOURCE=TIP
PIPETTE_PRESSURE_TEST DIST=20 SPEED=5 SOURCE=LIQUID
```

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `DIST` | `0` | `0` records without moving; `0`–`100` mm sets the maximum downward travel. |
| `HOLD` | `1` s | Recording time, `0.2`–`10` s; used only when `DIST=0`. |
| `SPEED` | `probe_speed` | Descent speed, `0.1`–`100` mm/s; used when `DIST>0`. |
| `SOURCE` | `TIP` | `TIP` or `LIQUID`: sensor that stops a moving test. |

For `DIST>0`, Z must be homed and `min_safe_z` must be set so the target stays
above that limit. `DIST / SPEED` may not exceed 30 seconds. The selected sensor
stops the descent if it triggers; otherwise Z may travel the full `DIST` and the
result is `distance_reached`. A configured pin is used instead of device-status
polling for that sensor. `SOURCE=LIQUID` starts and stops liquid sensing through
USB. The command does not perform `EXTRA_TRAVEL` or retract automatically, so
Z remains at the stopping position until another command moves it.

The console reports `stationary`, `pipette_sensor`, `distance_reached`, or
`error`, plus the baseline, sample counts, force range, and sampling errors.
A CSV in `pipette_pressure_reports/` beside `klippy.log` contains sample time,
phase, raw counts, force in grams, and an **estimated** Z position. This estimate
is less accurate during acceleration and stopping. Sensor errors, overflows,
or a truncated recording make the test
report an error. If there are no moving samples, the trace cannot establish a
motion baseline. Use moving traces to identify a guaranteed contact-free
interval, normal motion preload, and drift; measure actual stopping distance
separately. A stationary trace alone is insufficient for selecting the guard
limit.

### Enable protection after measuring

In `[pipette_action]`, configure the force limits
`pressure_guard_tip_force_g` / `pressure_guard_liquid_force_g` and their
corresponding `*_clear_time` values. The guard measures **absolute force change
from its moving baseline** on the Klipper host. A missing limit disables that
source's guard. Each host clear time must be verified contact-free. The optional
`pressure_guard_drift_g_s` defaults to 0; increase it only if moving traces
show repeatable slow baseline drift. See the commented settings in
[`pipette.cfg`](pipette.cfg).

When enabled, the guard learns the motion baseline during the clear interval.
A sustained excess force, invalid readings, or lost ADS1220 samples stops Z and
reports an error. A protection stop does not run `EXTRA_TRAVEL` or retract
automatically. ADS1220 readings arrive in batches, so this stop can lag a
physical pin trigger. The guard ends when the selected TIP or liquid signal triggers;
TIP's subsequent 3 mm seating move is outside the guard. Measure actual
stopping distance before relying on a threshold.

The machine example in `../config/pipette.cfg` enables provisional limits of
250 g TIP and 5 g liquid. These values still need real stopping-distance
verification. The 0.15 s baseline interval covers about 0.75 mm at 5 mm/s.

Use `PIPETTE_PRESSURE_GUARD_SET` to change the TIP/liquid limits, clear times,
or drift for subsequent probes without editing cfg. It prints current values
with no parameters and resets to cfg after Klipper restarts. See
[GCODE_COMMANDS.md](GCODE_COMMANDS.md) for syntax.

After positioning Z near the test object and checking `min_safe_z`, the protected
commands can be exercised with bounded travel, for example:

```gcode
PIPETTE_LIQUID_PROBE MAX_DIST=5 SPEED=1 EXTRA_TRAVEL=0 RETRACT=0
PIPETTE_PICKUP_TIP MAX_DIST=5 SPEED=1 EXTRA_TRAVEL=0 RETRACT=0
```

Run one source at a time. On a pressure stop, Klipper reports
`PIPETTE_PRESSURE_GUARD: stop_reason=...`, its reported end Z, and a CSV path
inside `pipette_pressure_reports/`. The CSV contains the descent force samples
and an estimated Z; it is saved on a protected-descent error, not on an ordinary
sensor trigger.
`PIPETTE_PRESSURE_TEST` remains diagnostic and does not stop on force.

New filenames include report type and sensor, for example
`pipette-pressure-guard-tip-*.csv` or `pipette-pressure-test-liquid-*.csv`.
Every sample row also records the original G-code command, selected sensor and
pin/device trigger source, device type, stop reason and detail, Klipper-reported
start/end Z, target Z and `min_safe_z`, effective motion settings, and (for guard
stops) the force limit, motion baseline, clear time, and trip time. The first
six sample columns retain their previous names; existing CSV files are not changed.
A run with no samples
still writes one metadata row with empty sample fields.

---

## Device Protocols

| | ADP1000 | SP20 |
|--|---------|------|
| Protocol | Modbus RTU (binary) | KT_OEM (binary frame + ASCII payload) |
| RS485 baud | 115200 | 38400 |
| Frame structure | FC03/FC06 + CRC16 | `[0xAA][seq][addr][len][payload][checksum]` |
| TIP register | `0x0231` = 0x0001 | `Rr3` returns `"1"` |
| Liquid register | `0x0204` = 0x0003 | `Rr2` returns `"1"` |
| Init command | Write `0x020C` = 1 | `It16000,100,0` |
| Liquid probe start | Write `0x0221` = mode | `Ld1,<timeout_ms>` |

---

## Configuration Reference

### [pipette]

| Parameter | Default | Description |
|-----------|---------|-------------|
| `device_type` | **required** | `adp1000` or `sp20` |
| `serial` | **required** | USB CDC serial port path |
| `device_addr` | 1 | Modbus slave / KT_OEM device address |
| `response_timeout` | 1.5 s | Single-request timeout |
| `poll_interval` | 0.02 s | Polling interval in wait loops |
| `action_timeout` | 15.0 s | Maximum wait for init / liquid detect |
| `retry_count` | 2 | USB-level retransmissions on timeout |
| `auto_init` | False | Auto-home device after Klipper connects |

### [pipette_action]

| Parameter | Default | Description |
|-----------|---------|-------------|
| `pipette` | **required** | Name of the `[pipette]` section |
| `probe_speed` | 5.0 mm/s | Z descent speed |
| `max_distance` | 50.0 mm | Maximum Z travel before abort |
| `tip_retract_dist` | 20.0 mm | Retract distance after TIP detected |
| `tip_extra_travel` | 0.0 mm | Extra downward travel after TIP trigger |
| `liquid_retract_dist` | 2.0 mm | Retract distance after liquid detected |
| `retract_speed` | 10.0 mm/s | Retract speed |
| `init_timeout` | 30.0 s | Init timeout when `INIT=1` used |
| `liquid_detect_mode` | 1 | ADP1000 pressure mode (SP20 ignores) |
| `probe_timeout` | 15.0 s | Device liquid-detection timeout; SP20 supports at most 20 s |
| `min_safe_z` | *(required for automatic descent)* | Lowest Z allowed at both stations |
| `tip_pin` | *(none)* | Hardware endstop pin for TIP detection |
| `liquid_pin` | *(none)* | Hardware endstop pin for liquid detection |
| `pressure_guard_tip_force_g` | *(none)* | TIP abnormal-force limit before TIP detection; absent disables TIP guard |
| `pressure_guard_tip_clear_time` | *(none)* | Required TIP contact-free baseline period |
| `pressure_guard_liquid_force_g` | *(none)* | Liquid collision-force limit before liquid detection; absent disables liquid guard |
| `pressure_guard_liquid_clear_time` | *(none)* | Required liquid contact-free baseline period |
| `pressure_guard_drift_g_s` | 0 | Maximum slow motion-baseline drift compensation |

For pipette detection, a configured trigger pin replaces device-status polling
as the Z stop source. Without a pin, device-status polling is used instead.
SP20 liquid sensing still needs USB commands to start and stop detection. An
enabled ADS1220 pressure guard is a separate stop source for either mode. See
[GCODE_COMMANDS.md](GCODE_COMMANDS.md) for the full command and safety details.
