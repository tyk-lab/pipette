import csv
import tempfile
import unittest

from pipette.pipette_pressure_guard import GuardedEndstop, MovingPressureGuard
from pipette.pipette_pressure_trace import PipettePressureTest, PressureTrace


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

    def completion(self):
        return FakeCompletion()

    def register_timer(self, callback, waketime):
        return callback

    def unregister_timer(self, handle):
        pass

    def register_callback(self, callback):
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


if __name__ == '__main__':
    unittest.main()
