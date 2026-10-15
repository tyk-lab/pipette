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

The script links this directory's modules, including `load_cell_probe.py`, into
`~/klipper/klippy/extras/` and restarts Klipper. If a target is a regular file,
the script saves it with a timestamped `.bak` suffix before creating the link.

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

# 3. In-place liquid detection (ADP1000 pressure mode 1; SP20 ignores MODE)
PIPETTE_LIQUID_DETECT MODE=1 WAIT=1

# 4. Z-axis TIP pickup (moves Z down until TIP detected)
PIPETTE_PICKUP_TIP MAX_DIST=30

# 5. Z-axis liquid probe (moves Z down until liquid detected)
PIPETTE_LIQUID_PROBE SPEED=2.0 RETRACT=3.0
```

See [GCODE_COMMANDS.md](GCODE_COMMANDS.md) for the full command reference.

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
| `pressure_guard_force_g` | *(none)* | Extra-force limit in g; absent disables the guard |
| `pressure_guard_clear_time` | *(none)* | Required contact-free baseline period when guard is enabled |
| `pressure_guard_drift_g_s` | 0 | Maximum slow motion-baseline drift compensation |

When a trigger pin is configured, that pin alone decides whether to stop the
corresponding Z descent. Without a pin, device-status polling is used instead.
SP20 liquid sensing still needs USB commands to start and stop detection.

Use `PIPETTE_PRESSURE_TEST` to measure motion preload before configuring the
guard. Moving tests and automatic descents require a configured `min_safe_z`. See
[GCODE_COMMANDS.md](GCODE_COMMANDS.md) for the command and safety details.
