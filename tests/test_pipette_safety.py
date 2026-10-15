import pathlib
import sys
import unittest
from unittest.mock import Mock, patch

from pipette.pipette_driver import PipetteDriver
from pipette.sp20_backend import Sp20Backend


class Sp20StatusTests(unittest.TestCase):
    def test_invalid_register_response_is_not_treated_as_absent(self):
        backend = object.__new__(Sp20Backend)
        for state, data in ((10, '0'), (2, ''), (None, '1')):
            backend._transceive = lambda command, s=state, d=data: (s, d)
            with self.subTest(state=state, data=data):
                with self.assertRaises(RuntimeError):
                    backend.read_tip()
                with self.assertRaises(RuntimeError):
                    backend.read_liquid_detected()

    def test_valid_boolean_register_values(self):
        backend = object.__new__(Sp20Backend)
        backend._transceive = lambda command: (2, '1')
        self.assertTrue(backend.read_tip())
        self.assertTrue(backend.read_liquid_detected())


class LiquidTipRequirementTests(unittest.TestCase):
    def setUp(self):
        self.driver = object.__new__(PipetteDriver)
        self.driver.command_error = RuntimeError
        self.driver._backend = Mock()
        self.driver.device_type = 'sp20'
        self.driver.action_timeout = 20.
        self.gcmd = Mock()
        self.gcmd.get_int.side_effect = lambda name, default, **kwargs: default
        self.gcmd.get_float.side_effect = lambda name, default, **kwargs: default

    def test_missing_tip_rejects_both_liquid_start_paths(self):
        self.driver._backend.read_tip.return_value = False
        for device_type in ('sp20', 'adp1000'):
            with self.subTest(device_type=device_type):
                self.driver.device_type = device_type
                with self.assertRaisesRegex(RuntimeError, 'TIP missing'):
                    self.driver.start_liquid_probe(mode=1, probe_timeout_s=15.)
                with self.assertRaisesRegex(RuntimeError, 'TIP missing'):
                    self.driver.cmd_pipette_start_liquid_detect(self.gcmd)
                with self.assertRaisesRegex(RuntimeError, 'TIP missing'):
                    self.driver.cmd_pipette_liquid_detect(self.gcmd)
                self.driver._backend.start_liquid_probe.assert_not_called()
                self.driver._backend.do_liquid_detect.assert_not_called()

    def test_tip_read_failure_rejects_liquid_start(self):
        self.driver._backend.read_tip.side_effect = RuntimeError('RS485 timeout')
        with self.assertRaisesRegex(RuntimeError, 'cannot verify TIP.*RS485 timeout'):
            self.driver.start_liquid_probe(mode=1, probe_timeout_s=15.)
        self.driver._backend.start_liquid_probe.assert_not_called()

    def test_present_tip_allows_liquid_detection_for_both_devices(self):
        self.driver._backend.read_tip.return_value = True
        for device_type in ('sp20', 'adp1000'):
            with self.subTest(device_type=device_type):
                self.driver.device_type = device_type
                self.driver.start_liquid_probe(mode=1, probe_timeout_s=15.)
                self.driver.cmd_pipette_liquid_detect(self.gcmd)
        self.assertEqual(self.driver._backend.read_tip.call_count, 4)
        self.assertEqual(self.driver._backend.start_liquid_probe.call_count, 2)
        self.assertEqual(self.driver._backend.do_liquid_detect.call_count, 2)

    def test_adp1000_mode_zero_can_turn_detection_off_without_tip(self):
        self.driver.device_type = 'adp1000'
        self.driver._backend.read_tip.return_value = False
        self.gcmd.get_int.side_effect = (
            lambda name, default, **kwargs: 0 if name == 'MODE' else default)
        self.driver.cmd_pipette_liquid_detect(self.gcmd)
        self.driver._backend.read_tip.assert_not_called()
        self.driver._backend.do_liquid_detect.assert_called_once()


def load_action_module():
    sys.path.insert(0, str(pathlib.Path('klipper/klippy').resolve()))
    import extras
    pipette_path = str(pathlib.Path('pipette').resolve())
    if pipette_path not in extras.__path__:
        extras.__path__.append(pipette_path)
    from extras import pipette_action
    return pipette_action


class EndstopSelectionTests(unittest.TestCase):
    def setUp(self):
        self.action_module = load_action_module()
        self.action = object.__new__(self.action_module.PipetteAction)

        class Driver:
            def get_tip_present(self):
                return False

            def get_liquid_detected(self):
                return False

        class Printer:
            config_error = RuntimeError

            def lookup_object(self, name):
                return driver

            def get_reactor(self):
                return self

        class Pin:
            def __init__(self):
                self.steppers = []
                self.last_triggered = None

            def add_stepper(self, stepper):
                self.steppers.append(stepper)

            def get_steppers(self):
                return self.steppers

            def home_start(self, print_time, sample_time, sample_count,
                           rest_time, triggered=True):
                self.last_triggered = triggered

        driver = Driver()
        self.action.printer = Printer()
        self.action._pipette_name = 'pipette'
        self.action._z_steppers = [object()]
        self.action._tip_pin_name = 'th:PB9'
        self.action._liquid_pin_name = 'th:PC13'
        self.action._tip_mcu_endstop = Pin()
        self.action._liquid_mcu_endstop = Pin()
        self.action.max_comm_failures = 3
        self.action.endstop_poll_interval = 0.1

    def test_configured_pins_select_only_physical_endstops(self):
        self.action._handle_connect()
        pin_type = self.action_module._PinEndstopWithCallbacks
        self.assertIsInstance(self.action._tip_endstop, pin_type)
        self.assertIsInstance(self.action._liquid_endstop, pin_type)
        self.assertEqual(len(self.action._tip_endstop.get_steppers()), 1)
        self.assertEqual(len(self.action._liquid_endstop.get_steppers()), 1)
        self.action._tip_endstop.home_start(0., 0., 0, 0.)
        self.action._liquid_endstop.home_start(0., 0., 0, 0.)
        self.assertTrue(self.action._tip_mcu_endstop.last_triggered)
        self.assertFalse(self.action._liquid_mcu_endstop.last_triggered)

    def test_missing_pins_select_only_device_polling(self):
        self.action._tip_mcu_endstop = None
        self.action._liquid_mcu_endstop = None
        self.action._handle_connect()
        poll_type = self.action_module._PipetteSoftEndstop
        self.assertIsInstance(self.action._tip_endstop, poll_type)
        self.assertIsInstance(self.action._liquid_endstop, poll_type)

    def test_each_sensor_selects_its_own_mode(self):
        self.action._liquid_mcu_endstop = None
        self.action._handle_connect()
        self.assertIsInstance(
            self.action._tip_endstop,
            self.action_module._PinEndstopWithCallbacks)
        self.assertIsInstance(
            self.action._liquid_endstop,
            self.action_module._PipetteSoftEndstop)


class ActionPreflightTests(unittest.TestCase):
    def setUp(self):
        action_module = load_action_module()
        self.action = object.__new__(action_module.PipetteAction)
        self.action.command_error = RuntimeError

        class Driver:
            device_type = 'sp20'

            def get_tip_present(self):
                return False

        self.action._drv = Driver()
        self.action._tip_mcu_endstop = object()
        self.action._tip_endstop = None
        self.action._liquid_endstop = None

    def test_tip_pin_trigger_rejects_move_despite_device_absent(self):
        self.action._query_pin_state = lambda *args, **kwargs: True
        with self.assertRaisesRegex(RuntimeError, 'already triggered'):
            self.action._check_tip_before_move()

    def test_register_mode_communication_failure_rejects_move(self):
        self.action._tip_mcu_endstop = None

        def failed_read():
            raise RuntimeError('RS485 timeout')

        self.action._drv.get_tip_present = failed_read
        with self.assertRaisesRegex(RuntimeError, 'RS485 timeout'):
            self.action._check_tip_before_move()

    def test_sp20_timeout_requires_motion_margin(self):
        self.action.probe_timeout = 10.
        with self.assertRaisesRegex(RuntimeError, 'probe_timeout'):
            self.action._validate_liquid_move(50., 5.)
        self.action.probe_timeout = 15.
        self.action._validate_liquid_move(50., 5.)

    def test_tip_pin_mode_does_not_read_device_status(self):
        self.action._drv.get_tip_present = lambda: (_ for _ in ()).throw(
            AssertionError('pin mode must not read device TIP status'))
        states = iter((False, True))
        self.action._query_pin_state = lambda *args, **kwargs: next(states)
        self.action._check_tip_before_move()
        self.action._confirm_tip_trigger()

    def test_tip_pin_drops_before_confirmation(self):
        self.action._query_pin_state = lambda *args, **kwargs: False
        with self.assertRaisesRegex(RuntimeError, 'extra travel cancelled'):
            self.action._confirm_tip_trigger()

    def test_liquid_precheck_uses_pin_after_start(self):
        self.action._liquid_mcu_endstop = object()
        self.action.get_liquid_pin_detected = lambda wait=False: (
            False, 'HIGH', True)
        self.action._drv.get_liquid_detected = lambda: (_ for _ in ()).throw(
            AssertionError('stale device register must not be used'))
        self.action._check_liquid_before_move()

    def test_liquid_pin_drops_before_confirmation(self):
        self.action._liquid_mcu_endstop = object()
        self.action.get_liquid_pin_detected = lambda wait=False: (
            False, 'HIGH', True)
        with self.assertRaisesRegex(RuntimeError, 'no longer triggered'):
            self.action._confirm_liquid_trigger()

    def test_missing_safe_z_rejects_automatic_descent(self):
        class Toolhead:
            def wait_moves(self):
                pass

            def get_position(self):
                return [0., 0., 20., 0.]

        self.action.min_safe_z = None
        params = {'max_dist': 10., 'probe_speed': 5.,
                  'retract_dist': 0., 'retract_speed': 5.}
        with self.assertRaisesRegex(RuntimeError, 'set min_safe_z'):
            self.action._z_probe_via_homing(
                None, Toolhead(), params, None, 'PIPETTE_PICKUP_TIP')

    def test_liquid_stop_failure_is_reported(self):
        self.action._liq_probe_active = True
        self.action._drv.stop_liquid_probe = lambda: (_ for _ in ()).throw(
            RuntimeError('stop failed'))
        with self.assertRaisesRegex(RuntimeError, 'stop failed'):
            self.action._liquid_probe_stop()


class PressureSourceTests(unittest.TestCase):
    def test_runtime_pressure_command_is_registered(self):
        module = load_action_module()
        action = object.__new__(module.PipetteAction)
        action.gcode = Mock()
        action.printer = Mock()
        action.reactor = Mock()
        action.pressure_guard_settings = Mock()
        action._register_commands()
        registrations = action.gcode.register_command.call_args_list
        commands = {call.args[0]: call.args[1] for call in registrations}
        self.assertIs(commands['PIPETTE_PRESSURE_GUARD_SET'],
                      action.pressure_guard_settings.cmd_set)

    def test_each_descent_uses_only_its_own_force_limit(self):
        module = load_action_module()
        action = object.__new__(module.PipetteAction)
        action.command_error = RuntimeError
        action.min_safe_z = 0.
        class Settings:
            drift_g_s = 0.
            values = {'tip': (250., 0.2),
                      'liquid': (5., 0.3)}

            def get(self, source):
                return self.values[source]

        action.pressure_guard_settings = Settings()
        action.reactor = Mock()
        action._get_pressure_load_cell = Mock(return_value=object())

        class Toolhead:
            def __init__(self):
                self.position = [0., 0., 100., 0.]
                self.moves = []

            def wait_moves(self):
                pass

            def get_position(self):
                return self.position

            def move(self, position, speed):
                self.moves.append((position[:], speed))
                self.position = position[:]

        homing = Mock()
        homing.probing_move.side_effect = lambda endstop, pos, speed: [0., 0., 90., 0.]
        action.printer = Mock()
        action.printer.lookup_object.return_value = homing
        gcmd = Mock()
        params = {'max_dist': 20., 'probe_speed': 5.,
                  'retract_dist': 0., 'retract_speed': 5.}
        endstop = Mock()

        with (patch.object(module.pipette_pressure_guard, 'MovingPressureGuard') as guard,
              patch.object(module.pipette_pressure_guard, 'GuardedEndstop') as wrapped,
              patch.object(module.pipette_pressure_trace, 'PressureTrace') as trace):
            trace.report_directory.return_value = 'unused-test-directory'
            for source, force, clear, extra in (
                    ('tip', 250., 0.2, 3.), ('liquid', 5., 0.3, 0.)):
                with self.subTest(source=source):
                    toolhead = Toolhead()
                    action._z_probe_via_homing(
                        gcmd, toolhead, params, endstop, source,
                        extra_travel=extra, pressure_source=source)
                    self.assertEqual(guard.call_args.args[1:4],
                                     (force, clear, 0.))
                    self.assertIs(homing.probing_move.call_args.args[0],
                                  wrapped.return_value)
                    if source == 'tip':
                        self.assertEqual(toolhead.moves[0][0][2], 87.)
            self.assertEqual(guard.call_count, 2)

            action.pressure_guard_settings.values['liquid'] = (None, None)
            action._z_probe_via_homing(
                gcmd, Toolhead(), params, endstop, 'liquid',
                pressure_source='liquid')
            self.assertIs(homing.probing_move.call_args.args[0], endstop)
            self.assertEqual(guard.call_count, 2)

            homing.probing_move.side_effect = RuntimeError('pressure stop')
            guard.return_value.reason = 'ADS1220 pressure protection'
            with patch.object(module.pipette_pressure_trace,
                              'report_guard_failure') as report:
                with self.assertRaisesRegex(RuntimeError, 'pressure stop'):
                    action._z_probe_via_homing(
                        gcmd, Toolhead(), params, endstop, 'tip',
                        pressure_source='tip')
                report.assert_called_once()
                trace.return_value.stop.assert_called()


if __name__ == '__main__':
    unittest.main()
