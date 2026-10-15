"""
pipette_driver.py - Unified Klipper driver for ADP1000 and SP20 pipette devices.

Configuration section: [pipette]
Required parameters: device_type (adp1000 | sp20), serial

Registers device-level G-code commands:
  PIPETTE_INIT           - home / initialize the device
  PIPETTE_READ_TIP       - read TIP presence state
  PIPETTE_READ_LIQUID    - read liquid state (pin first in AUTO mode)
  PIPETTE_UNLOAD_TIP     - unload/eject current TIP
  PIPETTE_ASPIRATE       - aspirate liquid
  PIPETTE_DISPENSE       - dispense liquid
  PIPETTE_VOLUME_STATUS  - read used/available volume status
  PIPETTE_LIQUID_DETECT  - trigger in-place liquid detection (no Z movement)
  PIPETTE_START_LIQUID_DETECT - start liquid sensing without Z movement
  PIPETTE_STOP_LIQUID_DETECT  - stop liquid sensing without Z movement

Exposes public methods for pipette_action.py:
  get_tip_present()         -> bool
  get_liquid_detected()     -> bool  (raises RuntimeError on abort condition)
  do_init(timeout_s)
  start_liquid_probe(mode, probe_timeout_s)
  stop_liquid_probe()

Target path: ~/klipper/klippy/extras/pipette_driver.py
Also copy adp1000_backend.py and sp20_backend.py to the same directory.
"""

import logging
import importlib.util
import os
import serial


def _load_sibling_module(name):
    """Load a module from the same directory as this file by filename."""
    here = os.path.dirname(os.path.realpath(__file__))
    path = os.path.join(here, name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PipetteDriver:

    def __init__(self, config):
        self.printer       = config.get_printer()
        self.reactor       = self.printer.get_reactor()
        self.gcode         = self.printer.lookup_object("gcode")
        self.command_error = self.printer.command_error

        self.device_type = config.get("device_type").lower().strip()
        if self.device_type not in ("adp1000", "sp20"):
            raise config.error(
                "pipette: device_type must be 'adp1000' or 'sp20', got '%s'"
                % self.device_type)

        self.serial_port      = config.get("serial")
        self.device_addr      = config.getint("device_addr",      1,    minval=1,   maxval=247)
        self.response_timeout = config.getfloat("response_timeout", 1.5, minval=0.05, maxval=20.0)
        self.poll_interval    = config.getfloat("poll_interval",    0.02, minval=0.001, maxval=1.0)
        self.action_timeout   = config.getfloat("action_timeout",   60.0, minval=0.1,  maxval=300.0)
        self.retry_count      = config.getint("retry_count",       2,    minval=0,   maxval=5)
        self.auto_init        = config.getboolean("auto_init",     False)

        self._ser           = None
        self._backend       = None
        self._auto_init_done = False

        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self._register_commands()

        logging.info(
            "PipetteDriver: init  device_type=%s  serial=%s  addr=%d  "
            "timeout=%.3fs  poll=%.3fs  retry=%d",
            self.device_type, self.serial_port, self.device_addr,
            self.response_timeout, self.poll_interval, self.retry_count)

    # ================================================================
    #  Reactor helpers passed to backends
    # ================================================================

    def _sleep(self, delay_s):
        self.reactor.pause(self.reactor.monotonic() + delay_s)

    # ================================================================
    #  Klipper connect handler
    # ================================================================

    def _handle_connect(self):
        try:
            self._ser = serial.Serial(self.serial_port, 115200, timeout=0)
        except serial.SerialException as e:
            raise self.printer.config_error(
                "pipette: failed to open serial port %s: %s" % (self.serial_port, e))

        backend_kwargs = dict(
            ser=self._ser,
            addr=self.device_addr,
            response_timeout=self.response_timeout,
            poll_interval=self.poll_interval,
            retry_count=self.retry_count,
            monotonic_fn=self.reactor.monotonic,
            sleep_fn=self._sleep,
        )

        if self.device_type == "adp1000":
            mod = _load_sibling_module("adp1000_backend")
            self._backend = mod.Adp1000Backend(**backend_kwargs)
        else:
            mod = _load_sibling_module("sp20_backend")
            self._backend = mod.Sp20Backend(**backend_kwargs)

        try:
            self._backend.verify_connection()
            logging.info(
                "PipetteDriver: connection OK  device_type=%s", self.device_type)
        except Exception as e:
            raise self.printer.config_error(
                "pipette: connection check failed "
                "(check USB cable and RP2040 firmware): %s" % e)

        if self.auto_init and not self._auto_init_done:
            self._auto_init_done = True
            try:
                self._backend.do_init(wait=True, timeout_s=self.action_timeout)
                logging.info("PipetteDriver: auto-initialization completed")
            except Exception as e:
                logging.error(
                    "PipetteDriver: auto-initialization failed; "
                    "run PIPETTE_INIT WAIT=1 manually: %s", e)

    # ================================================================
    #  G-code command registration
    # ================================================================

    def _register_commands(self):
        reg = self.gcode.register_command
        reg("PIPETTE_INIT", self.cmd_pipette_init,
            desc="Home/initialize the pipette device. "
                 "WAIT=1 (default) waits for completion. "
                 "WAIT_TIMEOUT=s overrides action_timeout. "
                 "TIMEOUT=s overrides response_timeout.")
        reg("PIPETTE_READ_TIP", self.cmd_pipette_read_tip,
            desc="Read TIP presence state from the pipette device. "
                 "TIMEOUT=s overrides response_timeout.")
        reg("PIPETTE_READ_LIQUID", self.cmd_pipette_read_liquid,
            desc="Read liquid-detect state from the pipette device. "
                 "SOURCE=auto|pin|device. AUTO uses liquid_pin when configured.")
        reg("PIPETTE_REQUIRE_TIP", self.cmd_pipette_require_tip,
            desc="Assert current TIP state. EXPECT=present|missing. "
                 "FORCE=1 only warns and continues; FORCE=0 (default) aborts.")
        reg("PIPETTE_UNLOAD_TIP", self.cmd_pipette_unload_tip,
            desc="Unload/eject the current TIP using the pipette device. "
                 "WAIT=1 (default) waits for completion. "
                 "WAIT_TIMEOUT=s overrides action_timeout.")
        reg("PIPETTE_ASPIRATE", self.cmd_pipette_aspirate,
            desc="Aspirate liquid. VOLUME=uL is required. "
                 "SPEED=uL/s and CUTOFF=uL/s are optional. "
                 "WAIT=1 (default), WAIT_TIMEOUT=s.")
        reg("PIPETTE_DISPENSE", self.cmd_pipette_dispense,
            desc="Dispense liquid. VOLUME=uL is required. "
                 "SPEED=uL/s, CUTOFF=uL/s, and BACK=uL are optional. "
                 "WAIT=1 (default), WAIT_TIMEOUT=s.")
        reg("PIPETTE_VOLUME_STATUS", self.cmd_pipette_volume_status,
            desc="Read pipette volume status. ADP1000 reads used/available "
                 "volume registers; SP20 reads capacity and position registers "
                 "and reports an estimate.")
        reg("PIPETTE_LIQUID_DETECT", self.cmd_pipette_liquid_detect,
            desc="Trigger in-place liquid detection (no Z movement). "
                 "MODE=0~5 selects ADP1000 pressure mode (SP20 ignores). "
                 "WAIT=1 (default) waits for result. "
                 "WAIT_TIMEOUT=s, TIMEOUT=s.")
        reg("PIPETTE_START_LIQUID_DETECT", self.cmd_pipette_start_liquid_detect,
            desc="Start liquid sensing without Z movement. "
                 "MODE=0~5 selects ADP1000 pressure mode (SP20 ignores). "
                 "TIMEOUT=s sets SP20-side detect timeout.")
        reg("PIPETTE_STOP_LIQUID_DETECT", self.cmd_pipette_stop_liquid_detect,
            desc="Stop liquid sensing without Z movement. "
                 "ADP1000 writes MODE=0; SP20 sends T.")

    # ================================================================
    #  G-code handlers
    # ================================================================

    def cmd_pipette_init(self, gcmd):
        self._check_ready()
        wait         = gcmd.get_int("WAIT",         1,                    minval=0, maxval=1)
        wait_timeout = gcmd.get_float("WAIT_TIMEOUT", self.action_timeout,   minval=0.1, maxval=300.0)
        try:
            if self.device_type == "adp1000":
                self._backend.do_init(wait=bool(wait), timeout_s=wait_timeout)
            else:
                self._backend.do_init(wait=bool(wait), timeout_s=wait_timeout)
        except RuntimeError as e:
            raise self.command_error(str(e))
        gcmd.respond_info("PIPETTE_INIT: done (device_type=%s)" % self.device_type)

    def cmd_pipette_read_tip(self, gcmd):
        self._check_ready()
        try:
            present = self._backend.read_tip()
        except RuntimeError as e:
            raise self.command_error(str(e))
        gcmd.respond_info(
            "PIPETTE_READ_TIP: %s (device_type=%s)"
            % ("TIP PRESENT" if present else "TIP MISSING", self.device_type))

    def cmd_pipette_read_liquid(self, gcmd):
        self._check_ready()
        source = gcmd.get("SOURCE", "auto").lower().strip()
        if source not in ("auto", "pin", "device"):
            raise gcmd.error(
                "PIPETTE_READ_LIQUID: SOURCE must be auto, pin, or device")
        if source in ("auto", "pin"):
            action = self._lookup_pipette_action()
            if action is not None and action.has_liquid_pin():
                try:
                    detected, level, raw_state = action.get_liquid_pin_detected()
                    pin_name = action.get_liquid_pin_name()
                except RuntimeError as e:
                    raise self.command_error(str(e))
                gcmd.respond_info(
                    "PIPETTE_READ_LIQUID: %s "
                    "(source=liquid_pin pin=%s level=%s state=%d(%s) "
                    "raw_endstop=%d(%s) device_type=%s)"
                    % ("LIQUID DETECTED" if detected else "NO LIQUID",
                       pin_name, level, 1 if detected else 0,
                       "TRIGGERED" if detected else "OPEN",
                       1 if raw_state else 0,
                       "TRIGGERED" if raw_state else "OPEN",
                       self.device_type))
                return
            if source == "pin":
                raise gcmd.error(
                    "PIPETTE_READ_LIQUID: liquid_pin is not configured")
        try:
            detected = self._backend.read_liquid_detected()
        except RuntimeError as e:
            raise self.command_error(str(e))
        gcmd.respond_info(
            "PIPETTE_READ_LIQUID: %s (source=device_register device_type=%s)"
            % ("LIQUID DETECTED" if detected else "NO LIQUID",
               self.device_type))

    def cmd_pipette_require_tip(self, gcmd):
        self._check_ready()
        expect = gcmd.get("EXPECT").lower().strip()
        if expect not in ("present", "missing"):
            raise gcmd.error("PIPETTE_REQUIRE_TIP: EXPECT must be 'present' or 'missing'")
        force = gcmd.get_int("FORCE", 0, minval=0, maxval=1)
        try:
            present = self._backend.read_tip()
        except RuntimeError as e:
            raise self.command_error(str(e))
        ok = (present and expect == "present") or (not present and expect == "missing")
        state = "TIP PRESENT" if present else "TIP MISSING"
        if ok:
            gcmd.respond_info("PIPETTE_REQUIRE_TIP: %s (matches EXPECT=%s)" % (state, expect))
            return
        msg = ("PIPETTE_REQUIRE_TIP: state mismatch — %s but EXPECT=%s"
               % (state, expect))
        if force:
            gcmd.respond_info("!! %s (FORCE=1, continuing)" % msg)
            return
        raise gcmd.error(msg + " (pass FORCE=1 to override)")

    def cmd_pipette_unload_tip(self, gcmd):
        self._check_ready()
        wait         = gcmd.get_int("WAIT",         1,                  minval=0, maxval=1)
        wait_timeout = gcmd.get_float(
            "WAIT_TIMEOUT", self.action_timeout, minval=0.1, maxval=300.0)
        try:
            self._backend.do_unload_tip(wait=bool(wait), timeout_s=wait_timeout)
        except RuntimeError as e:
            raise self.command_error(str(e))
        gcmd.respond_info("PIPETTE_UNLOAD_TIP: done (device_type=%s)" % self.device_type)

    def _get_motion_args(self, gcmd):
        volume = gcmd.get_float("VOLUME", minval=0.01, maxval=1000.0)
        speed = gcmd.get_float("SPEED", None, minval=1.0, maxval=64000.0)
        cutoff = gcmd.get_float("CUTOFF", None, minval=0.0, maxval=64000.0)
        wait = gcmd.get_int("WAIT", 1, minval=0, maxval=1)
        wait_timeout = gcmd.get_float(
            "WAIT_TIMEOUT", self.action_timeout, minval=0.1, maxval=300.0)
        return volume, speed, cutoff, bool(wait), wait_timeout

    def cmd_pipette_aspirate(self, gcmd):
        self._check_ready()
        volume, speed, cutoff, wait, wait_timeout = self._get_motion_args(gcmd)
        try:
            if self.device_type == "adp1000":
                self._backend.do_aspirate(
                    volume_ul=volume, speed_ul_s=speed,
                    wait=wait, timeout_s=wait_timeout)
            else:
                self._backend.do_aspirate(
                    volume_ul=volume, speed_ul_s=speed,
                    cutoff_ul_s=cutoff, wait=wait, timeout_s=wait_timeout)
        except RuntimeError as e:
            raise self.command_error(str(e))
        gcmd.respond_info(
            "PIPETTE_ASPIRATE: done volume=%.2f uL (device_type=%s)"
            % (volume, self.device_type))

    def cmd_pipette_dispense(self, gcmd):
        self._check_ready()
        volume, speed, cutoff, wait, wait_timeout = self._get_motion_args(gcmd)
        back = gcmd.get_float("BACK", 0.0, minval=0.0, maxval=100.0)
        try:
            if self.device_type == "adp1000":
                self._backend.do_dispense(
                    volume_ul=volume, speed_ul_s=speed,
                    wait=wait, timeout_s=wait_timeout)
            else:
                self._backend.do_dispense(
                    volume_ul=volume, speed_ul_s=speed, cutoff_ul_s=cutoff,
                    back_ul=back, wait=wait, timeout_s=wait_timeout)
        except RuntimeError as e:
            raise self.command_error(str(e))
        gcmd.respond_info(
            "PIPETTE_DISPENSE: done volume=%.2f uL (device_type=%s)"
            % (volume, self.device_type))

    def cmd_pipette_volume_status(self, gcmd):
        self._check_ready()
        try:
            status = self._backend.read_volume_status()
        except RuntimeError as e:
            raise self.command_error(str(e))

        if self.device_type == "adp1000":
            gcmd.respond_info(
                "PIPETTE_VOLUME_STATUS: used=%.3f uL available=%.3f uL "
                "capacity=%.3f uL raw_used=%d nL raw_available=%d nL "
                "(device_type=adp1000 source=%s)"
                % (
                    status["used_ul"],
                    status["available_ul"],
                    status["capacity_ul"],
                    status["used_nl"],
                    status["available_nl"],
                    status["source"],
                ))
            return

        if status["estimate_valid"]:
            gcmd.respond_info(
                "PIPETTE_VOLUME_STATUS: capacity=%d uL position=%d/%d ustep "
                "estimated_used=%.3f uL estimated_available=%.3f uL "
                "(device_type=sp20 source=%s)"
                % (
                    status["capacity_ul"],
                    status["position_ustep"],
                    status["max_travel_ustep"],
                    status["estimated_used_ul"],
                    status["estimated_available_ul"],
                    status["source"],
                ))
        else:
            gcmd.respond_info(
                "PIPETTE_VOLUME_STATUS: capacity=%d uL position=%d "
                "max_travel=%d ustep estimate_unavailable "
                "(device_type=sp20 source=%s)"
                % (
                    status["capacity_ul"],
                    status["position_ustep"],
                    status["max_travel_ustep"],
                    status["source"],
                ))

    def cmd_pipette_liquid_detect(self, gcmd):
        self._check_ready()
        mode         = gcmd.get_int("MODE",          1,                    minval=0, maxval=5)
        wait         = gcmd.get_int("WAIT",          1,                    minval=0, maxval=1)
        wait_timeout = gcmd.get_float("WAIT_TIMEOUT", self.action_timeout,   minval=0.1, maxval=300.0)
        try:
            if self.device_type == "adp1000":
                self._backend.do_liquid_detect(mode=mode, wait=bool(wait), timeout_s=wait_timeout)
            else:
                self._backend.do_liquid_detect(wait=bool(wait), timeout_s=wait_timeout)
        except RuntimeError as e:
            raise self.command_error(str(e))
        gcmd.respond_info("PIPETTE_LIQUID_DETECT: done (device_type=%s)" % self.device_type)

    def cmd_pipette_start_liquid_detect(self, gcmd):
        self._check_ready()
        mode = gcmd.get_int("MODE", 1, minval=0, maxval=5)
        timeout_s = gcmd.get_float(
            "TIMEOUT", self.action_timeout, minval=0.1, maxval=300.0)
        try:
            self.start_liquid_probe(mode=mode, probe_timeout_s=timeout_s)
        except RuntimeError as e:
            raise self.command_error(str(e))
        gcmd.respond_info(
            "PIPETTE_START_LIQUID_DETECT: started mode=%d timeout=%.3fs "
            "(device_type=%s)" % (mode, timeout_s, self.device_type))

    def cmd_pipette_stop_liquid_detect(self, gcmd):
        self._check_ready()
        try:
            self.stop_liquid_probe()
        except RuntimeError as e:
            raise self.command_error(str(e))
        gcmd.respond_info(
            "PIPETTE_STOP_LIQUID_DETECT: stopped (device_type=%s)"
            % self.device_type)

    # ================================================================
    #  Public interface for pipette_action.py
    # ================================================================

    def get_tip_present(self):
        """Return True if TIP is present. Called from pipette_action timer."""
        return self._backend.read_tip()

    def get_liquid_detected(self):
        """
        Return True if liquid is detected.
        Raises RuntimeError when the device reports a probe abort condition
        (e.g. ADP1000 DEVICE_STATUS=0x0004, max distance reached).
        Called from pipette_action timer.
        """
        return self._backend.read_liquid_detected()

    def do_init(self, timeout_s):
        """Initialize / home the device and wait for completion."""
        if self.device_type == "adp1000":
            self._backend.do_init(wait=True, timeout_s=timeout_s)
        else:
            self._backend.do_init(wait=True, timeout_s=timeout_s)

    def start_liquid_probe(self, mode, probe_timeout_s):
        """
        Called by pipette_action before Z-axis liquid probing movement.
        mode: ADP1000 pressure-detect mode (0-5); SP20 ignores this.
        probe_timeout_s: used by SP20 as the Ld command timeout.
        """
        if self.device_type == "adp1000":
            self._backend.start_liquid_probe(mode=mode)
        else:
            self._backend.start_liquid_probe(probe_timeout_s=probe_timeout_s)

    def stop_liquid_probe(self):
        """Called by pipette_action after Z-axis liquid probing movement."""
        self._backend.stop_liquid_probe()

    # ================================================================
    #  Internal
    # ================================================================

    def _check_ready(self):
        if self._backend is None:
            raise self.command_error(
                "PipetteDriver: device not ready (Klipper has not finished connecting)")

    def _lookup_pipette_action(self):
        try:
            return self.printer.lookup_object("pipette_action", None)
        except TypeError:
            try:
                return self.printer.lookup_object("pipette_action")
            except Exception:
                return None
        except Exception:
            return None


def load_config(config):
    return PipetteDriver(config)
