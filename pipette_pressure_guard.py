"""Optional ADS1220 force backup for pipette Z probing moves."""

from collections import deque
import math
from statistics import median
import time


class PressureGuardSettings:
    """Validated per-source limits, with session-only G-code overrides."""

    def __init__(self, config):
        self._settings = {}
        for source in ('tip', 'liquid'):
            prefix = 'pressure_guard_%s_' % source
            force = config.getfloat(prefix + 'force_g', None,
                                    minval=0.05, maxval=5000.)
            clear = config.getfloat(prefix + 'clear_time', None,
                                    minval=0.15, maxval=5.)
            mcu_force = config.getfloat(prefix + 'mcu_force_g', None,
                                        minval=0.05, maxval=5000.)
            if force is not None and clear is None:
                raise config.error('%sclear_time is required with %sforce_g'
                                   % (prefix, prefix))
            if mcu_force is not None and force is None:
                raise config.error('%sforce_g is required with %smcu_force_g'
                                   % (prefix, prefix))
            self._settings[source] = (force, clear, mcu_force)
        self.drift_g_s = config.getfloat('pressure_guard_drift_g_s', 0.,
                                         minval=0.)

    def get(self, source):
        return self._settings[source]

    def cmd_set(self, gcmd):
        """Update the next probing move; a failed update leaves all values intact."""
        updated = {}
        for source in ('tip', 'liquid'):
            force, clear, mcu_force = self._settings[source]
            prefix = source.upper() + '_'
            force = gcmd.get_float(prefix + 'FORCE_G', force or 0.,
                                   minval=0., maxval=5000.) or None
            clear = gcmd.get_float(prefix + 'CLEAR_TIME', clear,
                                   minval=0.15, maxval=5.)
            mcu_force = gcmd.get_float(prefix + 'MCU_FORCE_G', mcu_force or 0.,
                                       minval=0., maxval=5000.) or None
            if force is not None and clear is None:
                raise gcmd.error('%sCLEAR_TIME is required' % prefix)
            if mcu_force is not None and force is None:
                raise gcmd.error('%sFORCE_G is required with %sMCU_FORCE_G'
                                 % (prefix, prefix))
            updated[source] = (force, clear, mcu_force)
        drift = gcmd.get_float('DRIFT_G_S', self.drift_g_s, minval=0.)
        self._settings = updated
        self.drift_g_s = drift
        gcmd.respond_info(
            'PIPETTE_PRESSURE_GUARD_SET: TIP host=%s MCU=%s clear=%s; '
            'LIQUID host=%s MCU=%s clear=%s; drift=%.3f g/s; '
            'effective from the next probe until Klipper restarts'
            % (*updated['tip'], *updated['liquid'], drift))


def validate_mcu_pressure_support(printer, endstop, mcu_endstop, label):
    """Reject incomplete or cross-MCU wiring before starting Z motion."""
    try:
        dispatch = endstop.get_dispatch()
        supported = (hasattr(mcu_endstop, 'home_start_shared')
                     and dispatch.get_mcu() == mcu_endstop.get_mcu())
    except AttributeError:
        supported = False
    if not supported:
        raise printer.command_error(
            '%s: updated Klipper MCU pressure support and a shared MCU '
            'with the trigger pin are required' % label)


class MovingPressureGuard:
    """Learn motion preload in a known clear interval, then watch extra force."""

    def __init__(self, load_cell, limit_g, settle_time, drift_g_per_s,
                 clock=None):
        self._load_cell = load_cell
        self._limit_g = limit_g
        self._settle_time = settle_time
        self._drift_g_per_s = drift_g_per_s
        self._on_stop = None
        self._clock = clock or time.monotonic
        self._active = False
        self._start_time = None
        self._baseline = None
        self._baseline_samples = deque()
        self._window = deque()
        self._above_since = None
        self._last_time = None
        self._last_batch_time = None
        self.reason = None
        self.trip_time = None

    def start(self, print_time):
        self._start_time = print_time
        self._active = True
        self._last_batch_time = self._clock()
        self._load_cell.add_client(self._on_batch)

    def set_on_stop(self, callback):
        self._on_stop = callback

    def stop(self):
        self._active = False

    def check_stale(self, eventtime, current_print_time=None):
        if (current_print_time is not None
                and current_print_time < self._start_time + 0.35):
            return
        if self._active and eventtime - self._last_batch_time > 0.35:
            self._abort("ADS1220 samples stopped during descent")

    def _abort(self, reason, print_time=None):
        if self.reason is None:
            self.reason = reason
            self.trip_time = print_time
            self._on_stop()

    def _on_batch(self, msg):
        if not self._active:
            return False
        if msg.get('errors') or msg.get('overflows'):
            self._abort("ADS1220 reported sampling errors during descent")
            return True
        if any(row[0] >= self._start_time for row in msg.get('data', ())):
            self._last_batch_time = self._clock()
        range_min, range_max = self._load_cell.get_sensor().get_range()
        for print_time, force_g, _counts, _tare in msg.get('data', ()):
            if self.reason is not None:
                break
            if print_time < self._start_time:
                continue
            if force_g is None or not math.isfinite(force_g):
                self._abort("ADS1220 load cell force is unavailable or invalid",
                            print_time)
                break
            if _counts <= range_min or _counts >= range_max:
                self._abort("ADS1220 reading saturated during descent",
                            print_time)
                break
            if self._baseline is None:
                self._baseline_samples.append((print_time, force_g))
                # Use the end of the clear interval, after startup acceleration.
                cutoff = print_time - min(0.1, self._settle_time / 2.)
                while (self._baseline_samples
                       and self._baseline_samples[0][0] < cutoff):
                    self._baseline_samples.popleft()
                if print_time < self._start_time + self._settle_time:
                    continue
                if len(self._baseline_samples) < 5:
                    self._abort("insufficient ADS1220 samples for motion baseline",
                                print_time)
                    break
                self._baseline = median(v for _, v in self._baseline_samples)
                self._last_time = print_time
                self._baseline_samples.clear()
                continue
            self._window.append((print_time, force_g))
            cutoff = print_time - 0.025
            while self._window and self._window[0][0] < cutoff:
                self._window.popleft()
            if len(self._window) < 5:
                continue
            filtered = median(v for _, v in self._window)
            delta = abs(filtered - self._baseline)
            if delta >= self._limit_g:
                if self._above_since is None:
                    self._above_since = print_time
                elif print_time - self._above_since >= 0.025:
                    self._abort("ADS1220 pressure protection: extra force "
                                "%.3f g >= %.3f g" % (delta, self._limit_g),
                                print_time)
                    break
            else:
                self._above_since = None
                # Follow slow preload drift only while far from contact.
                if delta < self._limit_g / 2.:
                    elapsed = max(0., print_time - self._last_time)
                    step = self._drift_g_per_s * elapsed
                    self._baseline += max(-step, min(step,
                                                       filtered - self._baseline))
            self._last_time = print_time
        return True


class GuardedEndstop:
    """Stop on the selected sensor, MCU force, or host force."""

    def __init__(self, printer, endstop, guard, mcu_endstop=None,
                 mcu_force_g=None):
        self._printer = printer
        self._reactor = printer.get_reactor()
        self._endstop = endstop
        self._guard = guard
        self._mcu_endstop = mcu_endstop
        self._mcu_force_g = mcu_force_g
        self._completion = None
        self._watchdog_timer = None
        self.pressure_reason = None
        self.pressure_trip_time = None
        guard.set_on_stop(self._pressure_stop)

    def _pressure_stop(self):
        self._completion.complete(1)

    def _check_sensor(self, eventtime):
        print_time = self.get_mcu().estimated_print_time(eventtime)
        self._guard.check_stale(eventtime, print_time)
        if self._guard.reason is not None:
            return self._reactor.NEVER
        return eventtime + 0.1

    def _relay_original(self, completion):
        # Klipper completes a successful MCU trsync with False. Completion,
        # not its boolean result, is what must stop the host's drip move.
        completion.wait()
        self._completion.complete(1)

    def get_mcu(self):
        return self._endstop.get_mcu()

    def get_steppers(self):
        return self._endstop.get_steppers()

    def home_start(self, print_time, sample_time, sample_count,
                   rest_time, triggered=True):
        original = self._endstop.home_start(
            print_time, sample_time, sample_count, rest_time, triggered)
        try:
            if self._mcu_endstop is not None:
                self._mcu_endstop.home_start_shared(
                    print_time, self._endstop.get_dispatch())
        except Exception:
            try:
                self._endstop.home_wait(print_time)
            except Exception:
                pass
            raise
        self._completion = self._reactor.completion()
        self.pressure_reason = None
        self.pressure_trip_time = None
        self._guard.start(print_time)
        self._watchdog_timer = self._reactor.register_timer(
            self._check_sensor, self._reactor.monotonic() + 0.1)
        self._reactor.register_callback(
            lambda eventtime: self._relay_original(original))
        return self._completion

    def home_wait(self, home_end_time):
        pin_trigger = mcu_trigger = 0.
        shared_reason = None
        try:
            if self._mcu_endstop is None:
                pin_trigger = self._endstop.home_wait(home_end_time)
            else:
                pin_error = mcu_error = None
                try:
                    pin_trigger, shared_reason = (
                        self._endstop.home_wait_with_reason(home_end_time))
                except Exception as error:
                    pin_error = error
                try:
                    mcu_trigger = self._mcu_endstop.home_wait_shared(
                        shared_reason, home_end_time)
                except Exception as error:
                    mcu_error = error
                if mcu_error is not None:
                    self.pressure_reason = (
                        'MCU ADS1220 pressure sensor error: %s' % mcu_error)
                    raise mcu_error
                if pin_error is not None:
                    raise pin_error
        finally:
            if self._watchdog_timer is not None:
                self._reactor.unregister_timer(self._watchdog_timer)
                self._watchdog_timer = None
            self._guard.stop()
        pin_time = (self._endstop.trigger_time_to_print_time(pin_trigger)
                    if pin_trigger > 0. else None)
        mcu_time = mcu_trigger if mcu_trigger > 0. else None
        host_time = self._guard.trip_time if self._guard.reason is not None else None
        # Compare measurement times, not callback arrival order. A pin wins ties.
        if (mcu_time is not None
                and (pin_time is None or mcu_time < pin_time)
                and (host_time is None or mcu_time <= host_time)):
            self.pressure_trip_time = mcu_time
            self.pressure_reason = (
                'MCU ADS1220 pressure protection: %.3f g limit'
                % self._mcu_force_g)
            raise self._printer.command_error(self.pressure_reason)
        if (pin_time is not None
                and (mcu_time is None or pin_time <= mcu_time)
                and (self._guard.reason is None or
                     (host_time is not None and pin_time <= host_time))):
            return pin_trigger
        if self._guard.reason is not None:
            raise self._printer.command_error(self._guard.reason)
        if self._guard._baseline is None:
            raise self._printer.command_error(
                "ADS1220 motion baseline was not established")
        return pin_trigger

    def probe_prepare(self, hmove):
        return self._endstop.probe_prepare(hmove)

    def probe_finish(self, hmove):
        return self._endstop.probe_finish(hmove)

    def multi_probe_begin(self):
        return self._endstop.multi_probe_begin()

    def multi_probe_end(self):
        return self._endstop.multi_probe_end()
