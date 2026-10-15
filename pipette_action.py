"""
pipette_action.py - Device-agnostic Z-axis TIP pickup and liquid probe module.

Works with any device loaded by pipette_driver.py (ADP1000 or SP20) through
the PipetteDriver public interface:
  get_tip_present()        -> bool
  get_liquid_detected()    -> bool  (raises RuntimeError on abort condition)
  do_init(timeout_s)
  start_liquid_probe(mode, probe_timeout_s)
  stop_liquid_probe()

Configuration section: [pipette_action]
Required: pipette = <name of [pipette] section>

G-code commands:
  PIPETTE_PICKUP_TIP   - Z-axis move down until TIP detected, then retract
  PIPETTE_LIQUID_PROBE - Z-axis move down until liquid detected, then retract

Target path: ~/klipper/klippy/extras/pipette_action.py
"""

import logging
from . import pipette_pressure_guard, pipette_pressure_trace

# ---- Polling constants ----
_ENDSTOP_POLL_S          = 0.1
_ENDSTOP_FIRST_POLL_S    = 0.25
_TIP_RETRACT_MAX_MM      = 200.0
_LIQUID_RETRACT_MAX_MM   = 50.0


# ================================================================
#  Software virtual endstop
# ================================================================

class _PipetteSoftEndstop:
    """
    Software virtual endstop for Klipper's HomingMove / probing_move.

    check_fn() -> bool: returns True when the stop condition is met.
    If check_fn() raises RuntimeError, it is treated as an abort condition
    (stops immediately, raises the error to the caller).
    """

    def __init__(self, printer, check_fn,
                 prepare_fn=None, finish_fn=None,
                 max_comm_failures=3,
                 poll_interval=_ENDSTOP_POLL_S):
        self._printer            = printer
        self._reactor            = printer.get_reactor()
        self._check_fn           = check_fn
        self._prepare_fn         = prepare_fn
        self._finish_fn          = finish_fn
        self._max_comm_failures  = max_comm_failures
        self._poll_interval_cfg  = poll_interval
        self._steppers           = []
        self._completion         = None
        self._trigger_time       = 0.
        self._abort_reason       = None
        self._timer_handle       = None
        self._poll_interval      = None
        self._consecutive_failures = 0

    def get_steppers(self):
        return list(self._steppers)

    def add_stepper(self, stepper):
        if stepper not in self._steppers:
            self._steppers.append(stepper)

    def get_mcu(self):
        if not self._steppers:
            raise RuntimeError(
                "_PipetteSoftEndstop: no Z stepper registered")
        return self._steppers[0].get_mcu()

    def home_start(self, print_time, sample_time, sample_count,
                   rest_time, triggered=True):
        self._trigger_time       = 0.
        self._abort_reason       = None
        self._consecutive_failures = 0
        self._completion         = self._reactor.completion()
        self._poll_interval      = max(rest_time, self._poll_interval_cfg)
        first_delay = max(self._poll_interval, _ENDSTOP_FIRST_POLL_S)
        waketime = self._reactor.monotonic() + first_delay
        self._timer_handle = self._reactor.register_timer(
            self._do_poll, waketime)
        return self._completion

    def home_wait(self, home_end_time):
        if self._timer_handle is not None:
            self._reactor.unregister_timer(self._timer_handle)
            self._timer_handle = None
        if self._abort_reason is not None:
            raise self._printer.command_error(self._abort_reason)
        return self._trigger_time

    def trigger_time_to_print_time(self, trigger_time):
        return self.get_mcu().estimated_print_time(trigger_time)

    def probe_prepare(self, hmove):
        if self._prepare_fn is not None:
            self._prepare_fn()

    def probe_finish(self, hmove):
        if self._finish_fn is not None:
            self._finish_fn()

    def multi_probe_begin(self):
        pass

    def multi_probe_end(self):
        pass

    def _do_poll(self, eventtime):
        try:
            triggered = self._check_fn()
        except RuntimeError as e:
            # Device-reported abort (e.g. ADP1000 max distance reached).
            self._abort_reason = str(e)
            self._completion.complete(1)
            return self._reactor.NEVER
        except Exception as e:
            self._consecutive_failures += 1
            logging.warning(
                "_PipetteSoftEndstop: comm failure %d/%d: %s",
                self._consecutive_failures, self._max_comm_failures, e)
            if self._consecutive_failures >= self._max_comm_failures:
                self._abort_reason = (
                    "_PipetteSoftEndstop: communication failed %d times in a row; "
                    "stopping to protect the device" % self._consecutive_failures)
                self._completion.complete(1)
                return self._reactor.NEVER
            return self._reactor.monotonic() + self._poll_interval

        self._consecutive_failures = 0
        logging.debug("_PipetteSoftEndstop: poll triggered=%s", triggered)

        if triggered:
            self._trigger_time = self._reactor.monotonic()
            self._completion.complete(1)
            return self._reactor.NEVER

        return self._reactor.monotonic() + self._poll_interval


# ================================================================
#  Physical pin endstop wrapper (unchanged from adp1000_action.py)
# ================================================================

class _PinEndstopWithCallbacks:
    """Wrap an MCU physical-pin endstop and add prepare/finish callbacks."""

    def __init__(self, mcu_endstop, prepare_fn=None, finish_fn=None,
                 triggered_value=None):
        self._mcu_endstop     = mcu_endstop
        self._prepare_fn      = prepare_fn
        self._finish_fn       = finish_fn
        self._triggered_value = triggered_value

    def get_mcu(self):
        return self._mcu_endstop.get_mcu()

    def add_stepper(self, stepper):
        self._mcu_endstop.add_stepper(stepper)

    def get_steppers(self):
        return self._mcu_endstop.get_steppers()

    def home_start(self, print_time, sample_time, sample_count,
                   rest_time, triggered=True):
        if self._triggered_value is not None:
            triggered = self._triggered_value
        return self._mcu_endstop.home_start(
            print_time, sample_time, sample_count, rest_time, triggered)

    def home_wait(self, home_end_time):
        return self._mcu_endstop.home_wait(home_end_time)

    def trigger_time_to_print_time(self, trigger_time):
        return trigger_time

    def probe_prepare(self, hmove):
        if self._prepare_fn is not None:
            self._prepare_fn()

    def probe_finish(self, hmove):
        if self._finish_fn is not None:
            self._finish_fn()

    def multi_probe_begin(self):
        pass

    def multi_probe_end(self):
        pass


# ================================================================
#  Main action module
# ================================================================

class PipetteAction:

    cmd_pipette_pickup_tip_help = (
        "TIP pickup: move Z downward until the selected endstop triggers; "
        "stop when TIP is detected, push down EXTRA_TRAVEL mm, then retract. "
        "Params: INIT=0/1 INIT_TIMEOUT=s MAX_DIST=mm SPEED=mm/s "
        "EXTRA_TRAVEL=mm RETRACT=mm RETRACT_SPEED=mm/s"
    )
    cmd_pipette_liquid_probe_help = (
        "Liquid probe: activate sensing, move Z until the selected endstop "
        "triggers; stop, apply EXTRA_TRAVEL, then retract. "
        "Params: INIT=0/1 INIT_TIMEOUT=s MODE=0~5 MAX_DIST=mm "
        "SPEED=mm/s EXTRA_TRAVEL=mm RETRACT=mm RETRACT_SPEED=mm/s"
    )
    cmd_pipette_read_tip_pin_help = (
        "Read tip_pin endstop state (pin mode only). 1=triggered, 0=open.")
    cmd_pipette_read_liquid_pin_help = (
        "Read liquid_pin current physical level and endstop state.")
    cmd_pipette_pressure_test_help = (
        "Record ADS1220 force during a stationary hold or bounded Z descent. "
        "DIST=0..100 SPEED=mm/s SOURCE=tip|liquid HOLD=s")

    def __init__(self, config):
        self.printer       = config.get_printer()
        self.reactor       = self.printer.get_reactor()
        self.gcode         = self.printer.lookup_object("gcode")
        self.command_error = self.printer.command_error

        self._pipette_name = config.get("pipette", "pipette")
        self._drv          = None   # Filled after klippy:connect.

        # ---- Motion parameters ----
        self.probe_speed         = config.getfloat("probe_speed",         5.0,  minval=0.1, maxval=100.0)
        self.max_distance        = config.getfloat("max_distance",        50.0, minval=1.0, maxval=500.0)
        self.retract_speed       = config.getfloat("retract_speed",       10.0, minval=0.1, maxval=100.0)
        self.tip_retract_dist    = config.getfloat("tip_retract_dist",    20.0, minval=0.0, maxval=_TIP_RETRACT_MAX_MM)
        self.liquid_retract_dist = config.getfloat("liquid_retract_dist", 2.0,  minval=0.0, maxval=_LIQUID_RETRACT_MAX_MM)
        self.tip_extra_travel    = config.getfloat("tip_extra_travel",    0.0,  minval=0.0, maxval=20.0)
        self.liquid_extra_travel = config.getfloat("liquid_extra_travel", 0.0,  minval=0.0, maxval=20.0)

        # ---- Timeout and liquid-probe parameters ----
        self.init_timeout        = config.getfloat("init_timeout",        60.0, minval=1.0,  maxval=300.0)
        self.liquid_detect_mode  = config.getint("liquid_detect_mode",    1,    minval=0,    maxval=5)
        self.probe_timeout       = config.getfloat("probe_timeout",       15.0, minval=5.0,  maxval=300.0)

        # ---- Safety parameters ----
        self.min_safe_z          = config.getfloat("min_safe_z",          None)
        self.max_comm_failures   = config.getint("max_comm_failures",     3,    minval=1,   maxval=10)
        self.endstop_poll_interval = config.getfloat(
            "endstop_poll_interval", _ENDSTOP_POLL_S, minval=0.02, maxval=5.0)
        self.pressure_guard_settings = {}
        for source in ("tip", "liquid"):
            force_name = "pressure_guard_%s_force_g" % source
            clear_name = "pressure_guard_%s_clear_time" % source
            force_g = config.getfloat(force_name, None, minval=0.05)
            clear_time = config.getfloat(clear_name, None, minval=0.15, maxval=5.0)
            if force_g is not None and clear_time is None:
                raise config.error("%s is required when %s is set"
                                   % (clear_name, force_name))
            self.pressure_guard_settings[source] = (force_g, clear_time)
        self.pressure_guard_drift_g_s = config.getfloat(
            "pressure_guard_drift_g_s", 0.0, minval=0.0)

        # ---- Optional physical pins ----
        self._tip_pin_name       = config.get("tip_pin",    None)
        self._liquid_pin_name    = config.get("liquid_pin", None)
        self._tip_mcu_endstop    = None
        self._liquid_mcu_endstop = None
        ppins = self.printer.lookup_object("pins")
        if self._tip_pin_name is not None:
            try:
                self._tip_mcu_endstop = ppins.setup_pin("endstop", self._tip_pin_name)
            except Exception as e:
                raise config.error(
                    "PipetteAction: invalid tip_pin '%s': %s"
                    % (self._tip_pin_name, e))
        if self._liquid_pin_name is not None:
            try:
                self._liquid_mcu_endstop = ppins.setup_pin("endstop", self._liquid_pin_name)
            except Exception as e:
                raise config.error(
                    "PipetteAction: invalid liquid_pin '%s': %s"
                    % (self._liquid_pin_name, e))

        self._tip_endstop        = None
        self._liquid_endstop     = None
        self._z_steppers         = []
        self._current_liq_mode   = self.liquid_detect_mode
        self._liq_probe_active   = False

        self.printer.register_event_handler("klippy:mcu_identify", self._handle_mcu_identify)
        self.printer.register_event_handler("klippy:connect",      self._handle_connect)
        self._register_commands()

        logging.info(
            "PipetteAction: init  pipette=%s  probe_speed=%.1f  "
            "max_dist=%.1f  tip_retract=%.1f  liquid_retract=%.1f  liq_mode=%d",
            self._pipette_name, self.probe_speed, self.max_distance,
            self.tip_retract_dist, self.liquid_retract_dist, self.liquid_detect_mode)

    def _handle_mcu_identify(self):
        try:
            kin = self.printer.lookup_object("toolhead").get_kinematics()
            for stepper in kin.get_steppers():
                if stepper.is_active_axis("z"):
                    if stepper not in self._z_steppers:
                        self._z_steppers.append(stepper)
            if self._tip_mcu_endstop is not None:
                for s in self._z_steppers:
                    self._tip_mcu_endstop.add_stepper(s)
            if self._liquid_mcu_endstop is not None:
                for s in self._z_steppers:
                    self._liquid_mcu_endstop.add_stepper(s)
            logging.info("PipetteAction: mcu_identify collected %d Z steppers",
                         len(self._z_steppers))
        except Exception as e:
            logging.warning("PipetteAction: mcu_identify failed: %s", e)

    def _handle_connect(self):
        self._drv = self.printer.lookup_object(self._pipette_name)

        # ---- TIP endstop ----
        if self._tip_mcu_endstop is not None:
            self._tip_endstop = _PinEndstopWithCallbacks(self._tip_mcu_endstop)
            logging.info("PipetteAction: TIP endstop -> pin mode (%s)",
                         self._tip_pin_name)
        else:
            self._tip_endstop = _PipetteSoftEndstop(
                self.printer,
                check_fn=self._drv.get_tip_present,
                max_comm_failures=self.max_comm_failures,
                poll_interval=self.endstop_poll_interval,
            )
            logging.info("PipetteAction: TIP endstop -> register-polling mode")

        # ---- Liquid endstop ----
        if self._liquid_mcu_endstop is not None:
            self._liquid_endstop = _PinEndstopWithCallbacks(
                self._liquid_mcu_endstop,
                prepare_fn=self._liquid_probe_start,
                finish_fn=self._liquid_probe_stop,
                triggered_value=self._low_level_raw_endstop_state(),
            )
            logging.info("PipetteAction: liquid endstop -> pin mode (%s)",
                         self._liquid_pin_name)
        else:
            self._liquid_endstop = _PipetteSoftEndstop(
                self.printer,
                check_fn=self._drv.get_liquid_detected,
                prepare_fn=self._liquid_probe_start,
                finish_fn=self._liquid_probe_stop,
                max_comm_failures=self.max_comm_failures,
                poll_interval=self.endstop_poll_interval,
            )
            logging.info("PipetteAction: liquid endstop -> register-polling mode")

        if not self._z_steppers:
            raise self.printer.config_error(
                "PipetteAction: no Z stepper found; check kinematics config")
        for s in self._z_steppers:
            self._tip_endstop.add_stepper(s)
            self._liquid_endstop.add_stepper(s)

        logging.info(
            "PipetteAction: connected to '%s'  "
            "tip_steppers=%d  liquid_steppers=%d",
            self._pipette_name,
            len(self._tip_endstop.get_steppers()),
            len(self._liquid_endstop.get_steppers()))

    def _register_commands(self):
        reg = self.gcode.register_command
        reg("PIPETTE_PICKUP_TIP",       self.cmd_pipette_pickup_tip,
            desc=self.cmd_pipette_pickup_tip_help)
        reg("PIPETTE_LIQUID_PROBE",     self.cmd_pipette_liquid_probe,
            desc=self.cmd_pipette_liquid_probe_help)
        reg("PIPETTE_READ_TIP_PIN",     self.cmd_pipette_read_tip_pin,
            desc=self.cmd_pipette_read_tip_pin_help)
        reg("PIPETTE_READ_LIQUID_PIN",  self.cmd_pipette_read_liquid_pin,
            desc=self.cmd_pipette_read_liquid_pin_help)
        self._pressure_test = pipette_pressure_trace.PipettePressureTest(self)
        reg("PIPETTE_PRESSURE_TEST", self._pressure_test.cmd,
            desc=self.cmd_pipette_pressure_test_help)

    # ================================================================
    #  Internal helpers
    # ================================================================

    def _check_driver(self):
        if self._drv is None:
            raise self.command_error(
                "PipetteAction: '%s' not ready yet" % self._pipette_name)

    def _get_pressure_load_cell(self):
        probe = self.printer.lookup_object("probe", None)
        if probe is None or not hasattr(probe, "get_load_cell"):
            raise self.command_error(
                "PipetteAction: [load_cell_probe] is required for pressure sensing")
        load_cell = probe.get_load_cell()
        if not load_cell.is_calibrated() or not load_cell.is_tared():
            raise self.command_error(
                "PipetteAction: calibrate and tare the ADS1220 load cell first")
        return load_cell

    def _query_pin_state(self, mcu_endstop, pin_name, wait=True):
        if mcu_endstop is None:
            raise self.command_error(
                "PipetteAction: %s is not configured" % pin_name)
        toolhead = self.printer.lookup_object("toolhead")
        if wait:
            toolhead.wait_moves()
        return bool(mcu_endstop.query_endstop(toolhead.get_last_move_time()))

    @staticmethod
    def _pin_level_name(pin_name, triggered):
        """
        Infer current physical level from Klipper endstop state.

        query_endstop() returns Klipper's configured endstop state, not a raw
        voltage. For normal syntax (for example ^th:PC13), state=1 maps to
        physical HIGH; with ! inversion, state=1 maps to physical LOW.
        """
        inverted = "!" in (pin_name or "")
        high = (not triggered) if inverted else triggered
        return "HIGH" if high else "LOW"

    def _low_level_raw_endstop_state(self):
        """Return the query_endstop() state that corresponds to physical LOW."""
        return "!" in (self._liquid_pin_name or "")

    def has_liquid_pin(self):
        return self._liquid_mcu_endstop is not None

    def get_liquid_pin_state(self):
        return self._query_pin_state(self._liquid_mcu_endstop, "liquid_pin")

    def get_liquid_pin_level(self, wait=True):
        state = self._query_pin_state(
            self._liquid_mcu_endstop, "liquid_pin", wait=wait)
        return state, self._pin_level_name(self._liquid_pin_name, state)

    def get_liquid_pin_detected(self, wait=True):
        raw_state, level = self.get_liquid_pin_level(wait=wait)
        return level == "LOW", level, raw_state

    def get_liquid_pin_name(self):
        return self._liquid_pin_name

    def _format_driver_error(self, label, err):
        msg = str(err) or err.__class__.__name__
        lowered = msg.lower()
        if "not homed" in lowered:
            msg += "; run PIPETTE_INIT WAIT=1 first or use INIT=1"
        return "%s: %s" % (label, msg)

    def _liquid_probe_start(self):
        """Start liquid sensing before Z movement. Idempotent."""
        if self._liq_probe_active:
            return
        try:
            self._drv.start_liquid_probe(
                mode=self._current_liq_mode,
                probe_timeout_s=self.probe_timeout)
        except self.command_error:
            raise
        except Exception as e:
            # A command can reach the device even if its reply is lost.
            try:
                self._drv.stop_liquid_probe()
            except Exception as stop_error:
                logging.warning("PipetteAction: failed to clean up liquid start: %s",
                                stop_error)
            raise self.command_error(
                self._format_driver_error("PIPETTE_LIQUID_PROBE", e))
        self._liq_probe_active = True
        logging.info("PipetteAction: liquid probe started mode=%d", self._current_liq_mode)

    def _liquid_probe_stop(self):
        """Stop liquid sensing after Z movement; report cleanup failures."""
        if not self._liq_probe_active:
            return
        self._liq_probe_active = False
        try:
            self._drv.stop_liquid_probe()
            logging.info("PipetteAction: liquid probe stopped")
        except Exception as e:
            raise self.command_error(
                self._format_driver_error("PIPETTE_LIQUID_PROBE stop", e))

    def _check_tip_before_move(self):
        try:
            if self._tip_mcu_endstop is not None:
                present = self._query_pin_state(
                    self._tip_mcu_endstop, "tip_pin", wait=False)
            else:
                present = self._drv.get_tip_present()
        except Exception as e:
            raise self.command_error(
                self._format_driver_error("PIPETTE_PICKUP_TIP pre-check", e))
        if present:
            raise self.command_error(
                "PIPETTE_PICKUP_TIP: selected TIP sensor is already triggered")

    def _confirm_tip_trigger(self):
        if self._tip_mcu_endstop is None:
            return
        try:
            pin_present = self._query_pin_state(
                self._tip_mcu_endstop, "tip_pin", wait=False)
        except Exception as e:
            raise self.command_error(
                self._format_driver_error("PIPETTE_PICKUP_TIP confirmation", e))
        if not pin_present:
            raise self.command_error(
                "PIPETTE_PICKUP_TIP: tip_pin is no longer triggered; "
                "extra travel cancelled")

    def _validate_liquid_move(self, distance, speed):
        if self._drv.device_type != "sp20":
            return
        minimum_timeout = distance / speed + 2.0
        if self.probe_timeout > 20.0 or self.probe_timeout <= minimum_timeout:
            raise self.command_error(
                "PIPETTE_LIQUID_PROBE: SP20 probe_timeout must be <=20 s and "
                "> %.1f s for this descent" % minimum_timeout)

    def _check_liquid_before_move(self):
        try:
            detected = (self.get_liquid_pin_detected(wait=False)[0]
                        if self._liquid_mcu_endstop is not None
                        else self._drv.get_liquid_detected())
        except Exception as e:
            raise self.command_error(
                self._format_driver_error("PIPETTE_LIQUID_PROBE pre-check", e))
        if detected:
            raise self.command_error(
                "PIPETTE_LIQUID_PROBE: liquid already detected after sensing start")

    def _confirm_liquid_trigger(self):
        if self._liquid_mcu_endstop is None:
            return
        try:
            detected = self.get_liquid_pin_detected(wait=False)[0]
        except Exception as e:
            raise self.command_error(
                self._format_driver_error("PIPETTE_LIQUID_PROBE confirmation", e))
        if not detected:
            raise self.command_error(
                "PIPETTE_LIQUID_PROBE: liquid_pin is no longer triggered; "
                "check PC13 wiring or signal hold time")

    def _do_init(self, timeout_s):
        try:
            self._drv.do_init(timeout_s)
        except self.command_error:
            raise
        except Exception as e:
            raise self.command_error(
                self._format_driver_error("PIPETTE_INIT", e))

    def _get_move_params(self, gcmd, default_retract, max_retract):
        return {
            "max_dist":      gcmd.get_float("MAX_DIST",      self.max_distance,
                                            minval=1.0,  maxval=500.0),
            "probe_speed":   gcmd.get_float("SPEED",         self.probe_speed,
                                            minval=0.1,  maxval=100.0),
            "retract_dist":  gcmd.get_float("RETRACT",       default_retract,
                                            minval=0.0,  maxval=max_retract),
            "retract_speed": gcmd.get_float("RETRACT_SPEED", self.retract_speed,
                                            minval=0.1,  maxval=100.0),
        }

    def _get_z_axis_maximum(self, toolhead):
        try:
            kin = toolhead.get_kinematics()
            status = kin.get_status(self.reactor.monotonic())
            axis_max = status.get("axis_maximum")
            if axis_max is None:
                return None
            return float(axis_max[2])
        except Exception as e:
            logging.debug("PipetteAction: could not read Z axis maximum: %s", e)
            return None

    def _check_z_axis_maximum(self, toolhead, label, target_z):
        max_z = self._get_z_axis_maximum(toolhead)
        if max_z is not None and target_z > max_z:
            raise self.command_error(
                "%s: target Z=%.3f mm exceeds Z axis maximum %.3f mm"
                % (label, target_z, max_z))

    def _z_probe_via_homing(self, gcmd, toolhead, params, endstop, label,
                            extra_travel=0.0, confirm_fn=None,
                            pressure_source=None):
        max_dist      = params["max_dist"]
        probe_speed   = params["probe_speed"]
        retract_dist  = params["retract_dist"]
        retract_speed = params["retract_speed"]

        toolhead.wait_moves()
        pos = list(toolhead.get_position())
        start_z = pos[2]

        target_z = start_z - max_dist
        if self.min_safe_z is None:
            raise self.command_error(
                "%s: set min_safe_z before an automatic Z descent" % label)
        if target_z < self.min_safe_z:
            raise self.command_error(
                "%s: target Z=%.3f mm exceeds safety limit %.3f mm"
                % (label, target_z, self.min_safe_z))

        gcmd.respond_info(
            "%s: start Z=%.3f mm  max_dist=%.1f mm  speed=%.1f mm/s"
            % (label, start_z, max_dist, probe_speed))

        pos[2] = target_z
        phoming = self.printer.lookup_object("homing")
        trace = None
        force_g, clear_time = self.pressure_guard_settings.get(
            pressure_source, (None, None))
        if force_g is not None:
            if max_dist / probe_speed <= clear_time + 0.1:
                raise self.command_error(
                    "%s: descent is too short to establish the pressure baseline"
                    % label)
            load_cell = self._get_pressure_load_cell()
            directory = pipette_pressure_trace.PressureTrace.report_directory(
                self.printer)
            trace = pipette_pressure_trace.PressureTrace(load_cell)
            guard = pipette_pressure_guard.MovingPressureGuard(
                load_cell, force_g, clear_time,
                self.pressure_guard_drift_g_s, self.reactor.monotonic)
            endstop = pipette_pressure_guard.GuardedEndstop(
                self.printer, endstop, guard)
            trace.start()
        try:
            epos = phoming.probing_move(endstop, pos, probe_speed)
        except Exception as move_error:
            if trace is not None:
                trace.stop()
                pipette_pressure_trace.report_guard_failure(
                    trace, directory, gcmd, toolhead, start_z,
                    probe_speed, guard, self, pressure_source,
                    params, extra_travel, move_error)
            raise
        else:
            if trace is not None:
                trace.stop()
        trigger_z = epos[2]

        if confirm_fn is not None:
            confirm_fn()

        gcmd.respond_info(
            "%s: triggered at Z=%.3f mm (moved %.3f mm)"
            % (label, trigger_z, start_z - trigger_z))

        if extra_travel > 0.0:
            cur = list(toolhead.get_position())
            cur[2] = trigger_z - extra_travel
            if self.min_safe_z is not None and cur[2] < self.min_safe_z:
                raise self.command_error(
                    "%s: extra travel target Z=%.3f mm exceeds safety limit %.3f mm"
                    % (label, cur[2], self.min_safe_z))
            toolhead.move(cur, probe_speed)
            toolhead.wait_moves()
            gcmd.respond_info(
                "%s: extra travel %.3f mm -> Z=%.3f mm"
                % (label, extra_travel, cur[2]))

        if retract_dist > 0.0:
            cur = list(toolhead.get_position())
            cur[2] += retract_dist
            self._check_z_axis_maximum(toolhead, label, cur[2])
            toolhead.move(cur, retract_speed)
            toolhead.wait_moves()
            gcmd.respond_info(
                "%s: retracted %.3f mm -> Z=%.3f mm"
                % (label, retract_dist, cur[2]))

        return trigger_z

    # ================================================================
    #  G-code command handlers
    # ================================================================

    def cmd_pipette_pickup_tip(self, gcmd):
        """
        PIPETTE_PICKUP_TIP [INIT=0] [INIT_TIMEOUT=s]
                           [MAX_DIST=mm] [SPEED=mm/s]
                           [EXTRA_TRAVEL=mm] [RETRACT=mm] [RETRACT_SPEED=mm/s]
        """
        self._check_driver()
        do_init      = gcmd.get_int("INIT",          0,                  minval=0, maxval=1)
        init_timeout = gcmd.get_float("INIT_TIMEOUT", self.init_timeout,  minval=1.0, maxval=120.0)
        extra_travel = gcmd.get_float("EXTRA_TRAVEL", self.tip_extra_travel,
                                      minval=0.0, maxval=20.0)
        params   = self._get_move_params(gcmd, self.tip_retract_dist, _TIP_RETRACT_MAX_MM)
        toolhead = self.printer.lookup_object("toolhead")

        if do_init:
            gcmd.respond_info("PIPETTE_PICKUP_TIP: [1/2] running init...")
            self._do_init(init_timeout)
            gcmd.respond_info("PIPETTE_PICKUP_TIP: init done")
        else:
            gcmd.respond_info("PIPETTE_PICKUP_TIP: [1/2] skipping init (INIT=0)")

        self._check_tip_before_move()
        gcmd.respond_info("PIPETTE_PICKUP_TIP: pre-check TIP absent OK")

        gcmd.respond_info("PIPETTE_PICKUP_TIP: [2/2] starting Z move for TIP detection...")
        trigger_z = self._z_probe_via_homing(
            gcmd, toolhead, params, self._tip_endstop,
            "PIPETTE_PICKUP_TIP", extra_travel=extra_travel,
            confirm_fn=self._confirm_tip_trigger, pressure_source="tip")
        gcmd.respond_info(
            "PIPETTE_PICKUP_TIP: success  trigger Z=%.3f mm" % trigger_z)

    def cmd_pipette_liquid_probe(self, gcmd):
        """
        PIPETTE_LIQUID_PROBE [INIT=0] [INIT_TIMEOUT=s] [MODE=0~5]
                             [MAX_DIST=mm] [SPEED=mm/s]
                             [EXTRA_TRAVEL=mm] [RETRACT=mm] [RETRACT_SPEED=mm/s]
        """
        self._check_driver()
        do_init      = gcmd.get_int("INIT",          0,                       minval=0, maxval=1)
        init_timeout = gcmd.get_float("INIT_TIMEOUT", self.init_timeout,       minval=1.0, maxval=120.0)
        liq_mode     = gcmd.get_int("MODE",           self.liquid_detect_mode, minval=0, maxval=5)
        extra_travel = gcmd.get_float("EXTRA_TRAVEL", self.liquid_extra_travel,
                                      minval=0.0, maxval=20.0)
        params   = self._get_move_params(gcmd, self.liquid_retract_dist, _LIQUID_RETRACT_MAX_MM)
        toolhead = self.printer.lookup_object("toolhead")

        if do_init:
            gcmd.respond_info("PIPETTE_LIQUID_PROBE: [1/2] running init...")
            self._do_init(init_timeout)
            gcmd.respond_info("PIPETTE_LIQUID_PROBE: init done")
        else:
            gcmd.respond_info("PIPETTE_LIQUID_PROBE: [1/2] skipping init (INIT=0)")

        self._validate_liquid_move(params["max_dist"], params["probe_speed"])
        toolhead.wait_moves()
        if (self.min_safe_z is None or
                toolhead.get_position()[2] - params["max_dist"] < self.min_safe_z):
            raise self.command_error(
                "PIPETTE_LIQUID_PROBE: set a min_safe_z below the target Z")
        self._current_liq_mode = liq_mode
        gcmd.respond_info(
            "PIPETTE_LIQUID_PROBE: [2/2] starting Z move for liquid detection MODE=%d..." % liq_mode)
        try:
            self._liquid_probe_start()
            self._check_liquid_before_move()
            gcmd.respond_info("PIPETTE_LIQUID_PROBE: pre-check no liquid OK")
            trigger_z = self._z_probe_via_homing(
                gcmd, toolhead, params, self._liquid_endstop,
                "PIPETTE_LIQUID_PROBE", extra_travel=extra_travel,
                confirm_fn=self._confirm_liquid_trigger,
                pressure_source="liquid")
        except Exception:
            try:
                self._liquid_probe_stop()
            except Exception as stop_error:
                logging.error("PipetteAction: liquid cleanup also failed: %s",
                              stop_error)
            raise
        else:
            self._liquid_probe_stop()
        gcmd.respond_info(
            "PIPETTE_LIQUID_PROBE: success  trigger Z=%.3f mm" % trigger_z)

    def cmd_pipette_read_tip_pin(self, gcmd):
        state = self._query_pin_state(self._tip_mcu_endstop, "tip_pin")
        level = self._pin_level_name(self._tip_pin_name, state)
        gcmd.respond_info(
            "PIPETTE_READ_TIP_PIN: pin=%s level=%s state=%d(%s)"
            % (self._tip_pin_name, level, 1 if state else 0,
               "TRIGGERED" if state else "OPEN"))

    def cmd_pipette_read_liquid_pin(self, gcmd):
        detected, level, raw_state = self.get_liquid_pin_detected()
        gcmd.respond_info(
            "PIPETTE_READ_LIQUID_PIN: pin=%s level=%s state=%d(%s) "
            "raw_endstop=%d(%s)"
            % (self._liquid_pin_name, level, 1 if detected else 0,
               "TRIGGERED" if detected else "OPEN",
               1 if raw_state else 0, "TRIGGERED" if raw_state else "OPEN"))

def load_config(config):
    return PipetteAction(config)
