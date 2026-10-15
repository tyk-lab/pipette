"""Bounded ADS1220 recordings for pipette descent experiments."""

import csv
import datetime
import logging
import os
from statistics import median


MAX_TRACE_SAMPLES = 30000
REPORT_DIR_NAME = 'pipette_pressure_reports'
REPORT_FIELDS = (
    'report_file', 'recorded_at', 'record_kind', 'command', 'source',
    'trigger_source', 'trigger_pin',
    'device_type', 'stop_reason', 'stop_detail', 'start_z_mm', 'end_z_mm',
    'target_z_mm', 'min_safe_z_mm', 'probe_timeout_s',
    'max_dist_mm', 'speed_mm_s', 'extra_travel_mm', 'retract_mm',
    'retract_speed_mm_s', 'hold_s', 'liquid_mode', 'force_limit_g',
    'clear_time_s', 'drift_g_s', 'motion_baseline_g',
    'motion_start_s', 'motion_end_s', 'trip_print_time_s',
    'sample_count', 'sensor_errors', 'sensor_overflows', 'truncated',
)
SAMPLE_FIELDS = ('print_time_s', 'phase', 'z_est_mm',
                 'force_g', 'raw_counts', 'tare_counts')


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
    def report_directory(printer):
        log_file = printer.get_start_args().get('log_file')
        if not log_file or not os.path.isdir(os.path.dirname(log_file)):
            raise printer.command_error(
                "Pipette pressure report requires a Klipper log directory")
        directory = os.path.join(os.path.dirname(log_file), REPORT_DIR_NAME)
        os.makedirs(directory, exist_ok=True)
        return directory

    def write_csv(self, directory, start_z, end_z, motion_start,
                  motion_end, speed, metadata=None):
        metadata = dict(metadata or {})
        kind = metadata.get('record_kind', 'trace')
        source = metadata.get('source', 'unknown')
        if kind not in ('test', 'guard', 'trace') or source not in ('tip', 'liquid', 'unknown'):
            raise ValueError('Invalid pressure report kind or source')
        now = datetime.datetime.now().astimezone()
        name = "pipette-pressure-%s-%s-%s.csv" % (
            kind, source, now.strftime('%Y%m%d-%H%M%S-%f'))
        path = os.path.join(directory, name)
        metadata.update({
            'report_file': name,
            'recorded_at': now.isoformat(timespec='milliseconds'),
            'start_z_mm': '%.4f' % start_z,
            'end_z_mm': '%.4f' % end_z,
            'speed_mm_s': '%.4f' % speed,
            'motion_start_s': ('%.6f' % motion_start
                               if motion_start is not None else ''),
            'motion_end_s': ('%.6f' % motion_end
                             if motion_end is not None else ''),
            'sample_count': len(self.samples),
            'sensor_errors': self.errors,
            'sensor_overflows': self.overflows,
            'truncated': int(self.truncated),
        })
        with open(path, 'x', newline='', encoding='utf-8') as output:
            writer = csv.DictWriter(output, SAMPLE_FIELDS + REPORT_FIELDS)
            writer.writeheader()
            for print_time, force_g, counts, tare in self.samples:
                if motion_start is None or print_time < motion_start:
                    phase, z_est = 'before', start_z
                elif motion_end is None or print_time <= motion_end:
                    phase = 'descent'
                    # Estimated from commanded speed; acceleration and stop
                    # latency make this unsuitable as an exact contact Z.
                    z_est = max(end_z, start_z - speed *
                                max(0., print_time - motion_start))
                else:
                    phase, z_est = 'after', end_z
                writer.writerow(dict(metadata, **{
                    'print_time_s': '%.6f' % print_time,
                    'phase': phase,
                    'z_est_mm': '%.4f' % z_est,
                    'force_g': '%.5f' % force_g if force_g is not None else '',
                    'raw_counts': counts,
                    'tare_counts': tare,
                }))
            if not self.samples:
                writer.writerow(metadata)
        return path

    def summary(self, motion_start, motion_end):
        pre = [s[1] for s in self.samples
               if s[1] is not None
               and (motion_start is None or s[0] < motion_start)]
        moving = [s[1] for s in self.samples
                  if s[1] is not None and motion_start is not None
                  and motion_start <= s[0]
                  and (motion_end is None or s[0] <= motion_end)]
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


def _trigger_source(action, source):
    pin = getattr(action, '_%s_mcu_endstop' % source, None)
    return 'pin' if pin is not None else 'device_poll'


def _guard_stop_reason(reason):
    if reason is None:
        return 'move_error'
    if reason.startswith('ADS1220 pressure protection:'):
        return 'force_limit'
    if reason.startswith('ADS1220 samples stopped'):
        return 'samples_stale'
    if reason.startswith('insufficient ADS1220 samples'):
        return 'baseline_error'
    return 'sensor_error'


def report_guard_failure(trace, directory, gcmd, toolhead, start_z, speed,
                         guard, action, source, params, extra_travel,
                         move_error=None):
    """Save the descent samples after a pressure stop without masking its error."""
    try:
        end_z = toolhead.get_position()[2]
        motion_end = toolhead.get_last_move_time()
        detail = guard.reason or str(move_error)
        stop_reason = _guard_stop_reason(guard.reason)
        metadata = {
            'record_kind': 'guard',
            'command': gcmd.get_commandline(),
            'source': source,
            'trigger_source': _trigger_source(action, source),
            'trigger_pin': getattr(action, '_%s_pin_name' % source, '') or '',
            'device_type': getattr(getattr(action, '_drv', None), 'device_type', ''),
            'stop_reason': stop_reason,
            'stop_detail': detail,
            'max_dist_mm': params['max_dist'],
            'target_z_mm': start_z - params['max_dist'],
            'min_safe_z_mm': action.min_safe_z,
            'probe_timeout_s': (action.probe_timeout if source == 'liquid' else ''),
            'extra_travel_mm': extra_travel,
            'retract_mm': params['retract_dist'],
            'retract_speed_mm_s': params['retract_speed'],
            'liquid_mode': (getattr(action, '_current_liq_mode', '')
                            if source == 'liquid' else ''),
            'force_limit_g': guard._limit_g,
            'clear_time_s': guard._settle_time,
            'drift_g_s': guard._drift_g_per_s,
            'motion_baseline_g': guard._baseline,
            'trip_print_time_s': guard.trip_time,
        }
        path = trace.write_csv(directory, start_z, end_z,
                               guard._start_time, motion_end, speed, metadata)
        gcmd.respond_info(
            'PIPETTE_PRESSURE_GUARD: stop_reason=%s; start_z=%.3f '
            'end_z=%.3f; %s; samples=%d errors=%d overflows=%d '
            'truncated=%s CSV=%s' %
            (detail, start_z, end_z,
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
        directory = PressureTrace.report_directory(self._printer)
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

        trace_error = bool(trace.errors or trace.overflows or trace.truncated)
        if move_error is None and trace_error:
            stop_reason = 'trace_error'
        stop_detail = (str(move_error) if move_error is not None else
                       'Pressure trace is incomplete or contains sensor errors'
                       if trace_error else '')
        metadata = {
            'record_kind': 'test',
            'command': gcmd.get_commandline(),
            'source': source,
            'trigger_source': (_trigger_source(action, source)
                               if distance else 'none'),
            'trigger_pin': ((getattr(action, '_%s_pin_name' % source, '') or '')
                            if distance else ''),
            'device_type': getattr(getattr(action, '_drv', None), 'device_type', ''),
            'stop_reason': stop_reason,
            'stop_detail': stop_detail,
            'max_dist_mm': distance,
            'target_z_mm': target_z,
            'min_safe_z_mm': getattr(action, 'min_safe_z', ''),
            'probe_timeout_s': (getattr(action, 'probe_timeout', '')
                                if distance and source == 'liquid' else ''),
            'hold_s': hold if not distance else '',
            'liquid_mode': (getattr(action, '_current_liq_mode', '')
                            if distance and source == 'liquid' else ''),
        }
        path = trace.write_csv(directory, start_z, end_z, motion_start,
                               motion_end, speed, metadata)
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
        if trace_error:
            raise gcmd.error('Pressure trace is incomplete or contains sensor errors')
