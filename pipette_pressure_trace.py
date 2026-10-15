"""Bounded ADS1220 recordings for pipette descent experiments."""

import csv
import datetime
import logging
import os
from statistics import median


MAX_TRACE_SAMPLES = 30000


class PressureTrace:
    def __init__(self, load_cell):
        self._load_cell = load_cell
        self._active = False
        self.samples = []
        self.errors = 0
        self.overflows = 0
        self.truncated = False

    def start(self):
        self._active = True
        self._load_cell.add_client(self._on_batch)

    def stop(self):
        self._active = False

    def _on_batch(self, msg):
        if not self._active:
            return False
        self.errors += msg.get('errors', 0)
        self.overflows += msg.get('overflows', 0)
        for sample in msg.get('data', ()):
            if len(self.samples) >= MAX_TRACE_SAMPLES:
                self.truncated = True
                break
            self.samples.append(sample)
        return True

    @staticmethod
    def log_directory(printer):
        log_file = printer.get_start_args().get('log_file')
        if not log_file or not os.path.isdir(os.path.dirname(log_file)):
            raise printer.command_error(
                "PIPETTE_PRESSURE_TEST requires a Klipper log directory")
        return os.path.dirname(log_file)

    def write_csv(self, directory, start_z, end_z, motion_start,
                  motion_end, speed):
        name = "pipette-pressure-%s.csv" % (
            datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        path = os.path.join(directory, name)
        with open(path, 'x', newline='', encoding='utf-8') as output:
            writer = csv.writer(output)
            writer.writerow(('print_time_s', 'phase', 'z_est_mm',
                             'force_g', 'raw_counts', 'tare_counts'))
            for print_time, force_g, counts, tare in self.samples:
                if motion_start is None or print_time < motion_start:
                    phase, z_est = 'before', start_z
                elif print_time <= motion_end:
                    phase = 'descent'
                    # Estimated from commanded speed; acceleration and stop
                    # latency make this unsuitable as an exact contact Z.
                    z_est = max(end_z, start_z - speed *
                                max(0., print_time - motion_start))
                else:
                    phase, z_est = 'after', end_z
                writer.writerow(("%.6f" % print_time, phase,
                                 "%.4f" % z_est,
                                 "%.5f" % force_g if force_g is not None else '',
                                 counts, tare))
        return path

    def summary(self, motion_start, motion_end):
        pre = [s[1] for s in self.samples
               if s[1] is not None
               and (motion_start is None or s[0] < motion_start)]
        moving = [s[1] for s in self.samples
                  if s[1] is not None and motion_start is not None
                  and motion_start <= s[0] <= motion_end]
        baseline = median(pre) if pre else None
        if baseline is None:
            return "no stationary baseline samples; move_samples=%d" % len(moving)
        if not moving:
            return "baseline=%.3f g; move_samples=0 (too short or no move)" % baseline
        extra = [force - baseline for force in moving]
        return ("baseline=%.3f g; move_samples=%d; moving_range=%.3f..%.3f g; "
                "extra_range=%.3f..%.3f g" %
                (baseline, len(moving), min(moving), max(moving),
                 min(extra), max(extra)))


def report_guard_failure(trace, directory, gcmd, toolhead, start_z, speed,
                         guard):
    """Save the descent samples after a pressure stop without masking its error."""
    try:
        end_z = toolhead.get_position()[2]
        motion_end = toolhead.get_last_move_time()
        path = trace.write_csv(directory, start_z, end_z,
                               guard._start_time, motion_end, speed)
        gcmd.respond_info(
            'PIPETTE_PRESSURE_GUARD: stop_reason=%s; start_z=%.3f '
            'end_z=%.3f; %s; samples=%d errors=%d overflows=%d '
            'truncated=%s CSV=%s' %
            (guard.reason, start_z, end_z,
             trace.summary(guard._start_time, motion_end),
             len(trace.samples), trace.errors, trace.overflows,
             trace.truncated, path))
    except Exception:
        logging.exception('Pipette pressure guard: failed to save stop trace')


class PipettePressureTest:
    """Diagnostic G-code; motion stays bounded by a pipette endstop and Z limit."""

    def __init__(self, action):
        self._action = action
        self._printer = action.printer
        self._reactor = action.reactor

    def _select_endstop(self, source, gcmd):
        action = self._action
        if source == 'tip':
            endstop = action._tip_endstop
            already_hit = (action._query_pin_state(
                action._tip_mcu_endstop, 'tip_pin', wait=False)
                if action._tip_mcu_endstop is not None
                else action._drv.get_tip_present())
        else:
            endstop = action._liquid_endstop
            # SP20 may retain the prior Rr2 result until the next Ld command.
            # Liquid state is checked after sensing starts in _run_descent().
            already_hit = False
        if endstop is None:
            raise gcmd.error("Pipette endstop is not ready")
        if already_hit:
            raise gcmd.error("Pipette %s sensor is already triggered" % source)
        return endstop

    def _run_descent(self, toolhead, endstop, source, target_z, speed):
        from . import homing

        action = self._action
        try:
            if source == 'liquid':
                action._liquid_probe_start()
                action._check_liquid_before_move()
            motion_start = toolhead.get_last_move_time()
            position = list(toolhead.get_position())
            position[2] = target_z
            hmove = homing.HomingMove(
                self._printer, [(endstop, 'pipette_pressure_test')])
            result = hmove.homing_move(
                position, speed, probe_pos=True, check_triggered=False)
            if result[2] > target_z + 0.001:
                if source == 'tip':
                    action._confirm_tip_trigger()
                else:
                    action._confirm_liquid_trigger()
            return result[2], motion_start
        finally:
            if source == 'liquid':
                action._liquid_probe_stop()

    def cmd(self, gcmd):
        """Record force with no move or a Z move stopped by a pipette sensor."""
        action = self._action
        action._check_driver()
        load_cell = action._get_pressure_load_cell()
        distance = gcmd.get_float('DIST', 0.0, minval=0.0, maxval=100.0)
        speed = gcmd.get_float('SPEED', action.probe_speed,
                               minval=0.1, maxval=100.0)
        hold = gcmd.get_float('HOLD', 1.0, minval=0.2, maxval=10.0)
        source = gcmd.get('SOURCE', 'tip').lower()
        if source not in ('tip', 'liquid'):
            raise gcmd.error('SOURCE must be TIP or LIQUID')
        if distance and distance / speed > 30.0:
            raise gcmd.error('Pressure test descent may not exceed 30 seconds')
        if distance and source == 'liquid':
            action._validate_liquid_move(distance, speed)
        directory = PressureTrace.log_directory(self._printer)
        toolhead = self._printer.lookup_object('toolhead')
        toolhead.wait_moves()
        start_z = toolhead.get_position()[2]
        target_z = start_z - distance
        if distance:
            if action.min_safe_z is None:
                raise gcmd.error('Set min_safe_z before a moving pressure test')
            homed_axes = toolhead.get_status(self._reactor.monotonic()).get(
                'homed_axes', '')
            if 'z' not in homed_axes:
                raise gcmd.error('Home Z before a pressure descent test')
            if target_z < action.min_safe_z:
                raise gcmd.error('Pressure test target is below min_safe_z')
            endstop = self._select_endstop(source, gcmd)

        trace = PressureTrace(load_cell)
        motion_start = motion_end = None
        end_z = start_z
        stop_reason = 'stationary'
        move_error = None
        trace.start()
        try:
            if distance:
                self._reactor.pause(self._reactor.monotonic() + 0.3)
                motion_start = toolhead.get_last_move_time()
                try:
                    end_z, motion_start = self._run_descent(
                        toolhead, endstop, source, target_z, speed)
                finally:
                    motion_end = toolhead.get_last_move_time()
                stop_reason = ('pipette_sensor' if end_z > target_z + 0.001
                               else 'distance_reached')
                self._reactor.pause(self._reactor.monotonic() + 0.2)
            else:
                self._reactor.pause(self._reactor.monotonic() + hold)
        except Exception as e:
            move_error = e
            end_z = toolhead.get_position()[2]
            stop_reason = 'error'
        finally:
            trace.stop()

        path = trace.write_csv(directory, start_z, end_z, motion_start,
                               motion_end, speed)
        gcmd.respond_info(
            'PIPETTE_PRESSURE_TEST: %s; start_z=%.3f end_z=%.3f; %s'
            % (stop_reason, start_z, end_z,
               trace.summary(motion_start, motion_end)))
        gcmd.respond_info(
            'PIPETTE_PRESSURE_TEST: samples=%d errors=%d overflows=%d '
            'truncated=%s CSV=%s' %
            (len(trace.samples), trace.errors, trace.overflows,
             trace.truncated, path))
        if move_error is not None:
            raise move_error
        if trace.errors or trace.overflows or trace.truncated:
            raise gcmd.error('Pressure trace is incomplete or contains sensor errors')
