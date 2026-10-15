import csv
import os
import tempfile
import unittest

from pipette.pipette_pressure_guard import (
    GuardedEndstop, MovingPressureGuard, PressureGuardSettings)
from pipette.pipette_pressure_trace import (
    PipettePressureTest, PressureTrace, report_guard_failure)


class FakeLoadCell:
    def add_client(self, callback):
        self.callback = callback

    def get_sensor(self):
        return self

    def get_range(self):
        return -100000, 100000


def samples(start, stop, force, step=0.005):
    count = round((stop - start) / step)
    return [(round(start + i * step, 6), force, 100, 0)
            for i in range(count)]


class PressureGuardSettingsTests(unittest.TestCase):
    def setUp(self):
        class Config:
            error = RuntimeError

            def getfloat(self, name, default, **kwargs):
                values = {
                    'pressure_guard_tip_force_g': 250.,
                    'pressure_guard_tip_clear_time': 0.15,
                    'pressure_guard_liquid_force_g': 5.,
                    'pressure_guard_liquid_clear_time': 0.15,
                }
                return values.get(name, default)

            def get(self, name, default=None):
                return default

        self.settings = PressureGuardSettings(Config())

    def test_updates_next_probe_settings_without_changing_other_source(self):
        class Gcmd:
            error = RuntimeError

            def __init__(self):
                self.messages = []

            def get_float(self, name, default, **kwargs):
                return {'TIP_FORCE_G': 240.}.get(name, default)

            def get(self, name, default=None):
                return default

            def respond_info(self, message):
                self.messages.append(message)

        gcmd = Gcmd()
        self.settings.cmd_set(gcmd)
        self.assertEqual(self.settings.get('tip'), (240., 0.15))
        self.assertEqual(self.settings.get('liquid'), (5., 0.15))
        self.assertIn('effective from the next probe', gcmd.messages[0])

    def test_removed_mcu_parameter_does_not_change_any_setting(self):
        class Gcmd:
            error = RuntimeError

            def get_float(self, name, default, **kwargs):
                return 240. if name == 'TIP_FORCE_G' else default

            def get(self, name, default=None):
                return 300. if name == 'TIP_MCU_FORCE_G' else default

        with self.assertRaisesRegex(RuntimeError, 'TIP_MCU_FORCE_G was removed'):
            self.settings.cmd_set(Gcmd())
        self.assertEqual(self.settings.get('tip'), (250., 0.15))

    def test_host_limit_can_be_disabled_for_next_probe(self):
        class Gcmd:
            error = RuntimeError

            def get(self, name, default=None):
                return default

            def get_float(self, name, default, **kwargs):
                return 0. if name == 'TIP_FORCE_G' else default

            def respond_info(self, message):
                pass

        self.settings.cmd_set(Gcmd())
        self.assertEqual(self.settings.get('tip'), (None, 0.15))
        self.assertEqual(self.settings.get('liquid'), (5., 0.15))

    def test_removed_mcu_config_option_has_actionable_error(self):
        class Config:
            error = RuntimeError

            def get(self, name, default=None):
                return 300. if name == 'pressure_guard_tip_mcu_force_g' else default

        with self.assertRaisesRegex(RuntimeError, 'pressure_guard_tip_mcu_force_g was removed'):
            PressureGuardSettings(Config())


class PressureGuardTests(unittest.TestCase):
    def setUp(self):
        self.stops = []
        self.cell = FakeLoadCell()
        self.guard = MovingPressureGuard(self.cell, 0.5, 0.2, 0.)
        self.guard.set_on_stop(lambda: self.stops.append(True))
        self.guard.start(0.)

    def feed(self, data, errors=0):
        self.cell.callback({'data': data, 'errors': errors, 'overflows': 0})

    def test_motion_preload_above_limit_does_not_trigger(self):
        self.feed(samples(0., 0.35, 2.0))
        self.assertIsNone(self.guard.reason)
        self.assertAlmostEqual(self.guard._baseline, 2.0)
        self.assertEqual(self.stops, [])

    def test_spike_rejected_but_sustained_extra_force_stops(self):
        self.feed(samples(0., 0.3, 2.0))
        self.feed([(0.3, 8.0, 100, 0)] + samples(0.305, 0.35, 2.0))
        self.assertIsNone(self.guard.reason)
        self.feed(samples(0.35, 0.4, 2.8))
        self.assertIn('pressure protection', self.guard.reason)
        self.assertGreaterEqual(self.guard.trip_time, 0.35)
        self.assertEqual(len(self.stops), 1)

    def test_sampling_error_stops(self):
        self.feed([], errors=1)
        self.assertIn('sampling errors', self.guard.reason)
        self.assertEqual(len(self.stops), 1)

    def test_missing_batches_stop(self):
        self.guard.check_stale(self.guard._last_batch_time + 0.36)
        self.assertIn('samples stopped', self.guard.reason)
        self.assertEqual(len(self.stops), 1)

    def test_watchdog_waits_for_scheduled_motion(self):
        self.guard.check_stale(self.guard._last_batch_time + 0.36, 0.2)
        self.assertIsNone(self.guard.reason)

    def test_saturated_reading_stops(self):
        self.feed([(0.1, 2.0, 100000, 0)])
        self.assertIn('saturated', self.guard.reason)

    def test_invalid_force_stops(self):
        self.feed([(0.1, float('nan'), 100, 0)])
        self.assertIn('invalid', self.guard.reason)

    def test_slow_preload_drift_can_be_followed(self):
        guard = MovingPressureGuard(self.cell, 0.5, 0.2, 0.4)
        guard.set_on_stop(lambda: self.stops.append(True))
        guard.start(0.)
        self.feed(samples(0., 0.3, 2.0))
        self.feed([(round(0.3 + i * 0.005, 6), 2.0 + i * 0.001,
                    100, 0) for i in range(80)])
        self.assertIsNone(guard.reason)
        self.assertGreater(guard._baseline, 2.0)


class FakeReactor:
    NEVER = float('inf')

    def __init__(self):
        self.callbacks = []

    def completion(self):
        return FakeCompletion()

    def register_timer(self, callback, waketime):
        return callback

    def unregister_timer(self, handle):
        pass

    def register_callback(self, callback):
        self.callbacks.append(callback)
        self.callback = callback

    def monotonic(self):
        return 0.


class FakeCompletion:
    def __init__(self):
        self.value = None

    def complete(self, value):
        self.value = value

    def wait(self):
        return self.value


class FakePrinter:
    command_error = RuntimeError

    def __init__(self):
        self.reactor = FakeReactor()

    def get_reactor(self):
        return self.reactor


class FakeEndstop:
    def __init__(self, trigger_time):
        self.trigger_time = trigger_time

    def home_wait(self, home_end_time):
        return self.trigger_time

    def home_start(self, *args):
        completion = FakeCompletion()
        completion.complete(False)
        return completion

    def trigger_time_to_print_time(self, trigger_time):
        return trigger_time

class GuardedEndstopTests(unittest.TestCase):
    def test_force_trip_completes_homing_wait(self):
        cell = FakeLoadCell()
        guard = MovingPressureGuard(cell, 0.5, 0.2, 0., lambda: 0.)
        guarded = GuardedEndstop(FakePrinter(), FakeEndstop(0.), guard)
        completion = guarded.home_start(0., 0., 0, 0.)
        cell.callback({'data': samples(0., 0.3, 2.0),
                       'errors': 0, 'overflows': 0})
        cell.callback({'data': samples(0.3, 0.36, 2.8),
                       'errors': 0, 'overflows': 0})
        self.assertEqual(completion.value, 1)
        with self.assertRaisesRegex(RuntimeError, 'pressure protection'):
            guarded.home_wait(0.4)
        self.assertFalse(guard._active)

    def test_successful_pin_completion_stops_outer_move(self):
        cell = FakeLoadCell()
        guard = MovingPressureGuard(cell, 0.5, 0.2, 0., lambda: 0.)
        printer = FakePrinter()
        guarded = GuardedEndstop(printer, FakeEndstop(1.), guard)
        outer = guarded.home_start(0., 0., 0, 0.)
        # Klipper's MCU trsync uses False for a normal endstop hit.
        printer.get_reactor().callback(0.)
        self.assertEqual(outer.value, 1)

    def test_original_pin_wins_simultaneous_pressure_trigger(self):
        guard = MovingPressureGuard(FakeLoadCell(), 0.5, 0.2, 0.)
        guard.reason = 'pressure protection'
        guard.trip_time = 1.0
        guarded = GuardedEndstop(FakePrinter(), FakeEndstop(1.0), guard)
        self.assertEqual(guarded.home_wait(2.0), 1.0)

    def test_earlier_pressure_trip_wins_over_later_pin(self):
        guard = MovingPressureGuard(FakeLoadCell(), 0.5, 0.2, 0.)
        guard.reason = 'pressure protection'
        guard.trip_time = 0.9
        guarded = GuardedEndstop(FakePrinter(), FakeEndstop(1.0), guard)
        with self.assertRaisesRegex(RuntimeError, 'pressure protection'):
            guarded.home_wait(2.0)

    def test_pressure_trigger_is_an_error_without_pin_hit(self):
        guard = MovingPressureGuard(FakeLoadCell(), 0.5, 0.2, 0.)
        guard.reason = 'pressure protection'
        guarded = GuardedEndstop(FakePrinter(), FakeEndstop(0.0), guard)
        with self.assertRaisesRegex(RuntimeError, 'pressure protection'):
            guarded.home_wait(2.0)

class PressureTraceTests(unittest.TestCase):
    def test_pressure_stop_saves_force_curve_and_stop_reason(self):
        cell = FakeLoadCell()
        trace = PressureTrace(cell)
        trace.samples = [(0.9, 0.0, 100, 0), (1.1, 2.0, 120, 0),
                         (1.2, 8.0, 180, 0)]

        class Toolhead:
            def get_position(self):
                return [0., 0., 19., 0.]

            def get_last_move_time(self):
                return 1.2

        class Guard:
            reason = 'ADS1220 pressure protection: extra force 8 g >= 5 g'
            _start_time = 1.0
            _limit_g = 5.
            _settle_time = 0.15
            _drift_g_per_s = 0.
            _baseline = 0.
            trip_time = 1.2

        class Action:
            _tip_mcu_endstop = object()
            _tip_pin_name = '^th:PB9'
            min_safe_z = 0.

            class _drv:
                device_type = 'sp20'

        class Gcmd:
            def __init__(self):
                self.messages = []

            def respond_info(self, message):
                self.messages.append(message)

            def get_commandline(self):
                return 'PIPETTE_PICKUP_TIP MAX_DIST=5 SPEED=1 EXTRA_TRAVEL=0 RETRACT=0'

        with tempfile.TemporaryDirectory() as directory:
            gcmd = Gcmd()
            report_guard_failure(trace, directory, gcmd, Toolhead(),
                                 20., 1., Guard(), Action(), 'tip',
                                 {'max_dist': 5., 'retract_dist': 0.,
                                  'retract_speed': 10.}, 0.)
            self.assertIn('stop_reason=ADS1220 pressure protection',
                          gcmd.messages[0])
            path = gcmd.messages[0].split('CSV=')[1]
            with open(path, newline='', encoding='utf-8') as source:
                rows = list(csv.DictReader(source))
            self.assertIn('guard-tip', path)
            self.assertEqual([row['phase'] for row in rows],
                             ['before', 'descent', 'descent'])
            self.assertEqual(rows[-1]['force_g'], '8.00000')
            self.assertEqual(rows[0]['command'], gcmd.get_commandline())
            self.assertEqual(rows[0]['report_file'], os.path.basename(path))
            self.assertEqual(rows[0]['trigger_source'], 'pin')
            self.assertEqual(rows[0]['trigger_pin'], '^th:PB9')
            self.assertEqual(rows[0]['stop_reason'], 'force_limit')
            self.assertIn('extra force 8 g', rows[0]['stop_detail'])
            self.assertEqual(rows[0]['force_limit_g'], '5.0')
            self.assertEqual(rows[0]['end_z_mm'], '19.0000')
            self.assertEqual(rows[0]['target_z_mm'], '15.0')

    def test_stationary_and_no_moving_samples_are_explicit(self):
        trace = PressureTrace(FakeLoadCell())
        trace.samples = [(0.1, 1.2, 120, 0), (0.2, 1.4, 140, 0)]
        self.assertIn('move_samples=0', trace.summary(0.3, 0.31))
        with tempfile.TemporaryDirectory() as directory:
            path = trace.write_csv(directory, 20., 20., None, None, 5.)
            with open(path, newline='', encoding='utf-8') as source:
                rows = list(csv.DictReader(source))
            self.assertEqual([row['phase'] for row in rows],
                             ['before', 'before'])

    def test_report_keeps_command_and_error_when_no_samples_arrive(self):
        trace = PressureTrace(FakeLoadCell())
        metadata = {'record_kind': 'test', 'source': 'liquid',
                    'command': 'PIPETTE_PRESSURE_TEST SOURCE=LIQUID DIST=5',
                    'stop_reason': 'error', 'stop_detail': 'ADS1220 unavailable'}
        with tempfile.TemporaryDirectory() as directory:
            path = trace.write_csv(directory, 20., 20., None, None, 1., metadata)
            with open(path, newline='', encoding='utf-8') as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['command'], metadata['command'])
            self.assertEqual(rows[0]['stop_detail'], 'ADS1220 unavailable')
            self.assertEqual(rows[0]['sample_count'], '0')
            self.assertEqual(rows[0]['print_time_s'], '')

    def test_stationary_command_writes_sampled_csv(self):
        cell = FakeLoadCell()

        class Reactor:
            def monotonic(self):
                return 0.

            def pause(self, waketime):
                cell.callback({'data': [(0.1, 1.2, 120, 0)],
                               'errors': 0, 'overflows': 0})

        class Toolhead:
            def wait_moves(self):
                pass

            def get_position(self):
                return [0., 0., 20., 0.]

        class Gcmd:
            def __init__(self):
                self.messages = []

            def get_float(self, name, default, **kwargs):
                return default

            def get(self, name, default):
                return default

            def respond_info(self, message):
                self.messages.append(message)

            def get_commandline(self):
                return 'PIPETTE_PRESSURE_TEST DIST=0 HOLD=1'

        with tempfile.TemporaryDirectory() as directory:
            class Printer:
                def get_start_args(self):
                    return {'log_file': directory + '/klippy.log'}

                def lookup_object(self, name):
                    return Toolhead()

            class Action:
                printer = Printer()
                reactor = Reactor()
                probe_speed = 5.

                def _check_driver(self):
                    pass

                def _get_pressure_load_cell(self):
                    return cell

            gcmd = Gcmd()
            PipettePressureTest(Action()).cmd(gcmd)
            self.assertIn('stationary', gcmd.messages[0])
            self.assertIn('samples=1', gcmd.messages[1])
            path = gcmd.messages[1].split('CSV=')[1]
            self.assertIn('pipette_pressure_reports', path)
            self.assertIn('test-tip', path)
            with open(path, newline='', encoding='utf-8') as source:
                row = next(csv.DictReader(source))
            self.assertEqual(row['command'], gcmd.get_commandline())
            self.assertEqual(row['stop_reason'], 'stationary')
            self.assertEqual(row['source'], 'tip')
            self.assertEqual(row['hold_s'], '1.0')

    def test_moving_command_records_effective_settings_and_sensor_stop(self):
        cell = FakeLoadCell()

        class Reactor:
            def monotonic(self):
                return 0.

            def pause(self, waketime):
                pass

        class Toolhead:
            def wait_moves(self):
                pass

            def get_position(self):
                return [0., 0., 20., 0.]

            def get_status(self, eventtime):
                return {'homed_axes': 'z'}

            def get_last_move_time(self):
                return 1.5

        class Gcmd:
            error = RuntimeError

            def __init__(self):
                self.messages = []

            def get_float(self, name, default, **kwargs):
                return {'DIST': 5., 'SPEED': 1.}.get(name, default)

            def get(self, name, default):
                return 'LIQUID' if name == 'SOURCE' else default

            def get_commandline(self):
                return 'PIPETTE_PRESSURE_TEST DIST=5 SPEED=1 SOURCE=LIQUID'

            def respond_info(self, message):
                self.messages.append(message)

        with tempfile.TemporaryDirectory() as directory:
            class Printer:
                command_error = RuntimeError

                def get_start_args(self):
                    return {'log_file': directory + '/klippy.log'}

                def lookup_object(self, name):
                    return Toolhead()

            class Action:
                printer = Printer()
                reactor = Reactor()
                probe_speed = 5.
                min_safe_z = 0.
                probe_timeout = 15.
                _liquid_mcu_endstop = object()
                _liquid_pin_name = '^th:PC13'
                _current_liq_mode = 1

                class _drv:
                    device_type = 'sp20'

                def _check_driver(self):
                    pass

                def _get_pressure_load_cell(self):
                    return cell

                def _validate_liquid_move(self, distance, speed):
                    pass

            test = PipettePressureTest(Action())
            test._select_endstop = lambda source, gcmd: object()

            def run_descent(toolhead, endstop, source, target_z, speed):
                cell.callback({'data': [(1.1, -120., 100, 0)],
                               'errors': 0, 'overflows': 0})
                return 18., 1.

            test._run_descent = run_descent
            gcmd = Gcmd()
            test.cmd(gcmd)
            path = gcmd.messages[1].split('CSV=')[1]
            with open(path, newline='', encoding='utf-8') as source:
                row = next(csv.DictReader(source))
            self.assertIn('test-liquid', path)
            self.assertEqual(row['command'], gcmd.get_commandline())
            self.assertEqual(row['stop_reason'], 'pipette_sensor')
            self.assertEqual(row['trigger_pin'], '^th:PC13')
            self.assertEqual(row['max_dist_mm'], '5.0')
            self.assertEqual(row['speed_mm_s'], '1.0000')
            self.assertEqual(row['target_z_mm'], '15.0')
            self.assertEqual(row['end_z_mm'], '18.0000')


if __name__ == '__main__':
    unittest.main()
