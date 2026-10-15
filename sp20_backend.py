"""
sp20_backend.py - SP20 KT_OEM binary-frame protocol backend for pipette_driver.py

KT_OEM request frame:  [0xAA][seq 0x80-0xFE][addr][payload_len][payload...][sum & 0xFF]
KT_OEM response frame: [0x55][seq][??][state][data_len][data...][sum & 0xFF]

State codes: 0=Idle, 1=Busy, 2=Accepted, 3=Complete, 4=Liquid detected, >=10=Error

Do NOT import adp1000_backend or pipette_driver from here.
"""

import logging


_STATE_TEXT = {
    0:  "Idle",
    1:  "Busy",
    2:  "Executed successfully",
    3:  "Execution complete",
    4:  "Liquid detected",
    10: "Parameter out of range",
    11: "Parameter error",
    12: "Syntax error",
    13: "Invalid command",
    14: "Address error",
    15: "Write forbidden",
    16: "Read forbidden",
    17: "Not homed",
    18: "Z-axis not homed",
    19: "Z-axis not connected",
    20: "TIP missing alarm",
    21: "TIP eject failed alarm",
    22: "Timeout alarm",
    23: "Aspirate clog alarm",
    25: "Aspirate empty alarm",
    27: "Dispense clog alarm",
    50: "Motor stall error",
    51: "Driver fault",
    52: "Home opto error",
    53: "TIP opto error",
    54: "Pressure sensor error",
    55: "Storage error",
}


class Sp20Backend:

    REG_AUXOUT_CONFIG = 10
    AUXOUT_LOW_ON_LIQUID = 2
    LIQUID_TIMEOUT_MIN_MS = 1000
    LIQUID_TIMEOUT_MAX_MS = 20000

    def __init__(self, ser, addr, response_timeout, poll_interval,
                 retry_count, monotonic_fn, sleep_fn):
        """
        Parameters:
          ser              - open serial.Serial object (USB CDC, timeout=0)
          addr             - KT_OEM device address (0-255)
          response_timeout - seconds to wait for a single OEM response
          poll_interval    - seconds between polling attempts in wait loops
          retry_count      - number of USB-level retransmissions on timeout
          monotonic_fn     - callable() -> float, returns current time
          sleep_fn         - callable(delay_s), yields for delay_s seconds
        """
        self._ser              = ser
        self._addr             = addr
        self._response_timeout = response_timeout
        self._poll_interval    = poll_interval
        self._retry_count      = retry_count
        self._monotonic        = monotonic_fn
        self._sleep            = sleep_fn
        self._seq              = 0x80

    # ================================================================
    #  KT_OEM frame helpers
    # ================================================================

    def _next_seq(self):
        val = self._seq
        self._seq = 0x80 if self._seq >= 0xFE else self._seq + 1
        return val

    def _build_frame(self, cmd_str):
        """Pack cmd_str into a KT_OEM request binary frame."""
        payload = cmd_str.encode("ascii")
        frame = bytearray([0xAA, self._next_seq(), self._addr, len(payload)])
        frame.extend(payload)
        frame.append(sum(frame) & 0xFF)
        return bytes(frame)

    def _parse_response(self, raw):
        """
        Parse a KT_OEM response frame.
        Returns (state: int, data_str: str) or (None, "") on format error.

        Response layout:
          [0x55][seq][??][state][data_len][data...][checksum]
        """
        if not raw or len(raw) < 6 or raw[0] != 0x55:
            return None, ""
        state    = raw[3]
        data_len = raw[4]
        if len(raw) < 5 + data_len + 1:
            return None, ""
        data = raw[5: 5 + data_len].decode("ascii", errors="replace").strip()
        return state, data

    def _transceive(self, cmd_str, timeout_s=None):
        """
        Build a KT_OEM frame, send it, and return (state, data_str).
        Retries on timeout up to self._retry_count times.
        Raises RuntimeError on all-attempt failure.
        """
        if timeout_s is None:
            timeout_s = self._response_timeout

        frame = self._build_frame(cmd_str)

        for attempt in range(self._retry_count + 1):
            if attempt > 0:
                logging.warning(
                    "Sp20Backend: retry %d/%d  cmd=%s",
                    attempt, self._retry_count, cmd_str)
                frame = self._build_frame(cmd_str)  # new seq each retry

            self._ser.reset_input_buffer()
            self._ser.write(frame)

            deadline = self._monotonic() + timeout_s
            rx_buf   = bytearray()

            while self._monotonic() < deadline:
                avail = self._ser.in_waiting
                if avail > 0:
                    rx_buf.extend(self._ser.read(avail))
                    # Determine if we have a complete frame.
                    if len(rx_buf) >= 5 and rx_buf[0] == 0x55:
                        data_len = rx_buf[4]
                        expected = 6 + data_len
                        if len(rx_buf) >= expected:
                            state, data = self._parse_response(bytes(rx_buf[:expected]))
                            if state is not None:
                                logging.debug(
                                    "Sp20Backend: cmd=%s  state=%d[%s]  data=%r",
                                    cmd_str, state, _STATE_TEXT.get(state, "?"), data)
                                return state, data
                            # Bad frame — retry
                            break
                self._sleep(self._poll_interval)

        raise RuntimeError(
            "Sp20Backend: timeout after %d attempt(s) for cmd=%r"
            % (self._retry_count + 1, cmd_str))

    @staticmethod
    def _volume_to_counts(volume_ul):
        counts = int(round(volume_ul * 100.0))
        if counts < 4 or counts > 100000:
            raise RuntimeError(
                "Sp20Backend: volume out of range after conversion: %s uL" % volume_ul)
        return counts

    @staticmethod
    def _require_command_accepted(op, state):
        if state == 2:
            return
        raise RuntimeError(
            "Sp20Backend: %s command rejected state=%d[%s]"
            % (op, state, _STATE_TEXT.get(state, "Error")))

    def _read_reg_int(self, reg):
        state, data = self._transceive("Rr%d" % reg)
        if state >= 10:
            raise RuntimeError(
                "Sp20Backend: read Rr%d failed state=%d[%s]"
                % (reg, state, _STATE_TEXT.get(state, "Error")))
        try:
            return int(data)
        except (TypeError, ValueError):
            raise RuntimeError(
                "Sp20Backend: read Rr%d returned non-integer data=%r"
                % (reg, data))

    def _write_reg_int(self, reg, value):
        state, _ = self._transceive("Wr%d,%d" % (reg, value))
        self._require_command_accepted("write Wr%d" % reg, state)

    def _configure_liquid_output(self):
        """
        Configure SP20 AUXOUT to match ADP1000 liquid_pin polarity:
        HIGH while detecting / no liquid, LOW when liquid is detected.
        """
        self._write_reg_int(self.REG_AUXOUT_CONFIG, self.AUXOUT_LOW_ON_LIQUID)

    def _liquid_timeout_ms(self, timeout_s, min_ms=None):
        """
        Convert seconds to the SP20 Ld timeout range.

        SP20 accepts Ld timeout values from 0 to 20000 ms; clamp user-facing
        Klipper timeouts so defaults like action_timeout=60s do not produce
        state=10(Parameter out of range).
        """
        if min_ms is None:
            min_ms = self.LIQUID_TIMEOUT_MIN_MS
        tmo_ms = int(timeout_s * 1000)
        if tmo_ms < min_ms:
            return min_ms
        if tmo_ms > self.LIQUID_TIMEOUT_MAX_MS:
            return self.LIQUID_TIMEOUT_MAX_MS
        return tmo_ms

    def _stop_liquid_detection(self):
        """
        Stop any previous SP20 liquid-detect action so Rr2 is refreshed by
        the next Ld command instead of reporting the last latched result.
        """
        state, _ = self._transceive("T")
        if state is None or state >= 10:
            raise RuntimeError(
                "Sp20Backend: stop liquid detect failed state=%s[%s]"
                % (state, _STATE_TEXT.get(state, "Error")))
        logging.debug("Sp20Backend: liquid detect stopped state=%s", state)

    # ================================================================
    #  Wait helpers
    # ================================================================

    def _wait_action_done(self, timeout_s):
        """
        Poll device status via '?' until action completes.

        Per SP20 protocol doc (Q12): action is complete when status transitions
        from Busy(1) back to Idle(0). In KT_OEM frames the '?' response usually
        carries the current status in the state byte. Some firmware variants may
        return it as ASCII data, so accept both forms.
        """
        deadline = self._monotonic() + timeout_s
        last_status = None

        while self._monotonic() < deadline:
            try:
                state, data = self._transceive(
                    "?", timeout_s=min(self._response_timeout, 1.0))
            except Exception:
                self._sleep(self._poll_interval)
                continue

            try:
                status_val = int(data) if data else state
            except ValueError:
                self._sleep(self._poll_interval)
                continue

            last_status = status_val

            if status_val == 0:
                return
            if status_val >= 10:
                raise RuntimeError(
                    "Sp20Backend: action failed status=%d[%s]"
                    % (status_val, _STATE_TEXT.get(status_val, "Error")))
            # 1=Busy, 2=Accepted — keep polling
            self._sleep(self._poll_interval)

        raise RuntimeError(
            "Sp20Backend: action timed out, last status=%s"
            % (last_status if last_status is not None else "none"))

    # ================================================================
    #  Public interface (called by PipetteDriver)
    # ================================================================

    def verify_connection(self):
        """Query device model and version to confirm communication."""
        state, model = self._transceive("Rr91")
        state2, ver  = self._transceive("Rr90")
        if state is None or state2 is None:
            raise RuntimeError("Sp20Backend: no valid response from device")
        logging.info(
            "Sp20Backend: connection OK  model=%r  version=%r", model, ver)

    def read_tip(self):
        """Return True if TIP is present (register 3 == '1')."""
        state, data = self._transceive("Rr3")
        if state is None or state >= 10 or data not in ("0", "1"):
            raise RuntimeError(
                "Sp20Backend: invalid TIP status state=%s data=%r"
                % (state, data))
        return data == "1"

    def read_liquid_detected(self):
        """Return True if liquid is detected (register 2 == '1')."""
        state, data = self._transceive("Rr2")
        if state is None or state >= 10 or data not in ("0", "1"):
            raise RuntimeError(
                "Sp20Backend: invalid liquid status state=%s data=%r"
                % (state, data))
        return data == "1"

    def read_volume_status(self):
        """
        Read SP20 capacity and position.

        SP20 does not expose a direct current-liquid-volume register. Estimate
        used/available volume from current motor position and max travel.
        """
        position_ustep = self._read_reg_int(20)
        max_travel_ustep = self._read_reg_int(28)
        capacity_ul = self._read_reg_int(29)
        estimate_valid = max_travel_ustep > 0 and capacity_ul >= 0

        estimated_used_ul = None
        estimated_available_ul = None
        if estimate_valid:
            ratio = float(position_ustep) / float(max_travel_ustep)
            if ratio < 0.0:
                ratio = 0.0
            elif ratio > 1.0:
                ratio = 1.0
            estimated_used_ul = capacity_ul * ratio
            estimated_available_ul = capacity_ul - estimated_used_ul

        return {
            "device_type": "sp20",
            "source": "Rr20/Rr28/Rr29",
            "position_ustep": position_ustep,
            "max_travel_ustep": max_travel_ustep,
            "capacity_ul": capacity_ul,
            "estimate_valid": estimate_valid,
            "estimated_used_ul": estimated_used_ul,
            "estimated_available_ul": estimated_available_ul,
        }

    def do_init(self, wait, timeout_s):
        """Send init/home command and optionally wait for completion."""
        state, _ = self._transceive("It16000,100,0")
        self._require_command_accepted("init", state)
        if wait:
            self._wait_action_done(timeout_s)
        logging.info("Sp20Backend: init sent, wait=%s", wait)

    def do_unload_tip(self, wait, timeout_s):
        """
        Trigger SP20 TIP eject via homing command.

        It speed=16000, power=100, mode=0 means home and always eject TIP.
        """
        state, _ = self._transceive("It16000,100,0")
        self._require_command_accepted("unload TIP", state)
        if wait:
            self._wait_action_done(timeout_s)
        logging.info("Sp20Backend: unload TIP sent, wait=%s", wait)

    def do_aspirate(self, volume_ul, speed_ul_s, cutoff_ul_s, wait, timeout_s):
        """Send Ia aspirate command and optionally wait for completion."""
        volume = self._volume_to_counts(volume_ul)
        speed = int(round(speed_ul_s)) if speed_ul_s is not None else 200
        cutoff = int(round(cutoff_ul_s)) if cutoff_ul_s is not None else 10
        if cutoff > speed:
            raise RuntimeError("Sp20Backend: cutoff speed must be <= aspirate speed")
        state, _ = self._transceive("Ia%d,%d,%d" % (volume, speed, cutoff))
        self._require_command_accepted("aspirate", state)
        if wait:
            self._wait_action_done(timeout_s)
        logging.info(
            "Sp20Backend: aspirate sent volume=%d speed=%d cutoff=%d wait=%s",
            volume, speed, cutoff, wait)

    def do_dispense(self, volume_ul, speed_ul_s, cutoff_ul_s,
                    back_ul, wait, timeout_s):
        """Send Da dispense command and optionally wait for completion."""
        volume = self._volume_to_counts(volume_ul)
        back = int(round(back_ul * 100.0))
        if back < 0 or back > 10000:
            raise RuntimeError(
                "Sp20Backend: back volume out of range after conversion: %s uL"
                % back_ul)
        speed = int(round(speed_ul_s)) if speed_ul_s is not None else 200
        cutoff = int(round(cutoff_ul_s)) if cutoff_ul_s is not None else 10
        if cutoff > speed:
            raise RuntimeError("Sp20Backend: cutoff speed must be <= dispense speed")
        state, _ = self._transceive(
            "Da%d,%d,%d,%d" % (volume, back, speed, cutoff))
        self._require_command_accepted("dispense", state)
        if wait:
            self._wait_action_done(timeout_s)
        logging.info(
            "Sp20Backend: dispense sent volume=%d back=%d speed=%d cutoff=%d wait=%s",
            volume, back, speed, cutoff, wait)

    def do_liquid_detect(self, wait, timeout_s):
        """PIPETTE_LIQUID_DETECT handler: send Ld command, optionally wait."""
        tmo_ms = self._liquid_timeout_ms(timeout_s)
        self._stop_liquid_detection()
        self._configure_liquid_output()
        state, _ = self._transceive("Ld1,%d" % tmo_ms)
        self._require_command_accepted("liquid detect", state)
        if wait:
            self._wait_action_done(timeout_s)
        logging.info("Sp20Backend: liquid detect sent tmo_ms=%d wait=%s", tmo_ms, wait)

    def start_liquid_probe(self, probe_timeout_s):
        """
        Called by pipette_action before Z-axis liquid probing movement.
        Sends Ld1,<tmo_ms> to activate SP20 liquid detection.
        Uses probe_timeout_s as the SP20-side timeout (converted to ms).
        """
        tmo_ms = self._liquid_timeout_ms(probe_timeout_s, min_ms=5000)
        self._stop_liquid_detection()
        self._configure_liquid_output()
        state, _ = self._transceive("Ld1,%d" % tmo_ms)
        self._require_command_accepted("liquid probe start", state)
        logging.info("Sp20Backend: liquid probe started tmo_ms=%d", tmo_ms)

    def stop_liquid_probe(self):
        """
        Called by pipette_action after Z-axis liquid probing movement.
        Sends T so SP20 stops the active Ld action and refreshes the latched
        Rr2 liquid-detect state before the next probe.
        """
        self._stop_liquid_detection()
