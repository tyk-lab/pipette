"""
adp1000_backend.py - ADP1000 Modbus RTU protocol backend for pipette_driver.py

Handles all ADP1000-specific communication: Modbus RTU framing, CRC,
FC03/FC06 register operations, and device-specific wait logic.

Do NOT import sp20_backend or pipette_driver from here.
"""

import logging


class Adp1000Backend:
    # ---- ADP1000 Register Map ----
    REG_SW_VERSION      = 0x0F00
    REG_EXEC_STATUS     = 0x0203
    REG_DEVICE_STATUS   = 0x0204
    REG_VOLUME_STATUS   = 0x0205
    REG_RESET_STATUS    = 0x020D
    REG_PRESSURE_DETECT = 0x0221
    REG_ASPIRATE        = 0x0222
    REG_DISPENSE        = 0x022B
    REG_TIP_PRESENCE    = 0x0231
    REG_DISPENSE_SPEED  = 0x0233
    REG_ASPIRATE_SPEED  = 0x0234
    REG_INIT_CMD        = 0x020C
    REG_UNLOAD_TIP      = 0x0230

    EXEC_STATUS_TEXT = {
        0x0000: "Busy",
        0x0001: "Completed",
        0x0002: "Alarm or insufficient travel",
    }
    DEVICE_STATUS_TEXT = {
        0x0000: "Running",
        0x0001: "Target position reached",
        0x0002: "Abnormal collision",
        0x0003: "Liquid detected",
        0x0004: "Max liquid-detect distance reached (failed)",
        0x0005: "Travel limit exceeded",
        0x0006: "Aspiration blocked",
        0x0007: "Dispense blocked",
        0x0009: "Dry aspiration alarm (early phase)",
        0x000A: "Dry aspiration alarm (late phase)",
    }
    RESET_STATUS_TEXT = {
        0x0001: "Reset OK",
        0x0002: "Homing failed",
        0x0003: "Not homed after power-on",
    }

    def __init__(self, ser, addr, response_timeout, poll_interval,
                 retry_count, monotonic_fn, sleep_fn):
        """
        Parameters:
          ser              - open serial.Serial object (USB CDC, timeout=0)
          addr             - Modbus slave address (1-247)
          response_timeout - seconds to wait for a single RTU response
          poll_interval    - seconds between polling attempts in wait loops
          retry_count      - number of USB-level retransmissions on timeout
          monotonic_fn     - callable() -> float, returns current time (reactor.monotonic)
          sleep_fn         - callable(delay_s), yields to the reactor for delay_s seconds
        """
        self._ser              = ser
        self._addr             = addr
        self._response_timeout = response_timeout
        self._poll_interval    = poll_interval
        self._retry_count      = retry_count
        self._monotonic        = monotonic_fn
        self._sleep            = sleep_fn
        self._liq_probe_active = False

    # ================================================================
    #  Modbus RTU helpers
    # ================================================================

    @staticmethod
    def _crc16_modbus(data):
        crc = 0xFFFF
        for b in data:
            crc ^= b
            for _ in range(8):
                crc = (crc >> 1) ^ 0xA001 if crc & 0x0001 else crc >> 1
        return crc & 0xFFFF

    @staticmethod
    def _rtu_total_len_by_header(buf):
        if len(buf) < 2:
            return 0
        fc = buf[1]
        if fc & 0x80:
            return 5
        if fc in (1, 2, 3, 4):
            return (5 + buf[2]) if len(buf) >= 3 else 0
        if fc in (5, 6, 15, 16):
            return 8
        return 0

    @classmethod
    def _rtu_has_valid_crc(cls, frame):
        if len(frame) < 5:
            return False
        calc = cls._crc16_modbus(frame[:-2])
        recv = frame[-2] | (frame[-1] << 8)
        return calc == recv

    @staticmethod
    def _hex_bytes(data):
        return " ".join("%02X" % b for b in data)

    def _build_fc03_req(self, reg, count):
        req = bytearray(8)
        req[0] = self._addr
        req[1] = 0x03
        req[2] = (reg >> 8) & 0xFF
        req[3] = reg & 0xFF
        req[4] = (count >> 8) & 0xFF
        req[5] = count & 0xFF
        crc = self._crc16_modbus(req[:6])
        req[6] = crc & 0xFF
        req[7] = (crc >> 8) & 0xFF
        return bytes(req)

    def _build_fc06_req(self, reg, value):
        req = bytearray(8)
        req[0] = self._addr
        req[1] = 0x06
        req[2] = (reg >> 8) & 0xFF
        req[3] = reg & 0xFF
        req[4] = (value >> 8) & 0xFF
        req[5] = value & 0xFF
        crc = self._crc16_modbus(req[:6])
        req[6] = crc & 0xFF
        req[7] = (crc >> 8) & 0xFF
        return bytes(req)

    def _transceive(self, req_bytes, timeout_s=None):
        """Send a Modbus RTU frame and return the response frame."""
        if timeout_s is None:
            timeout_s = self._response_timeout

        for attempt in range(self._retry_count + 1):
            if attempt > 0:
                logging.warning(
                    "Adp1000Backend: USB retry %d/%d  req=%s",
                    attempt, self._retry_count, self._hex_bytes(req_bytes))

            self._ser.reset_input_buffer()
            self._ser.write(req_bytes)

            deadline = self._monotonic() + timeout_s
            rx_buf = bytearray()

            while self._monotonic() < deadline:
                avail = self._ser.in_waiting
                if avail > 0:
                    rx_buf.extend(self._ser.read(avail))
                    total = self._rtu_total_len_by_header(rx_buf)
                    if total > 0 and len(rx_buf) >= total:
                        frame = bytes(rx_buf[:total])
                        if not self._rtu_has_valid_crc(frame):
                            logging.warning(
                                "Adp1000Backend: CRC error  frame=%s", self._hex_bytes(frame))
                            break
                        if frame[0] != self._addr:
                            logging.debug(
                                "Adp1000Backend: address mismatch  frame=%s", self._hex_bytes(frame))
                            break
                        return frame
                self._sleep(self._poll_interval)

        raise RuntimeError(
            "Adp1000Backend: USB timed out after %d attempt(s): no valid RTU response"
            % (self._retry_count + 1,))

    def _check_exception_rsp(self, rsp):
        if len(rsp) < 2:
            raise RuntimeError("Adp1000Backend: response too short: %s" % self._hex_bytes(rsp))
        fc = rsp[1]
        if fc & 0x80:
            ex = rsp[2] if len(rsp) > 2 else 0xFF
            raise RuntimeError(
                "Adp1000Backend: Modbus exception FC=0x%02X EX=0x%02X" % (fc, ex))
        return fc

    def _fc03_read_regs(self, reg, count, timeout_s=None):
        req = self._build_fc03_req(reg, count)
        rsp = self._transceive(req, timeout_s=timeout_s)
        fc = self._check_exception_rsp(rsp)
        if fc != 0x03:
            raise RuntimeError("Adp1000Backend: FC mismatch 0x%02X" % fc)
        byte_cnt = rsp[2]
        if byte_cnt != count * 2:
            raise RuntimeError("Adp1000Backend: byte count mismatch got=%d expect=%d"
                               % (byte_cnt, count * 2))
        regs = []
        for i in range(count):
            regs.append((rsp[3 + i * 2] << 8) | rsp[4 + i * 2])
        return regs

    def _fc06_write_reg(self, reg, value, timeout_s=None):
        req = self._build_fc06_req(reg, value)
        rsp = self._transceive(req, timeout_s=timeout_s)
        fc = self._check_exception_rsp(rsp)
        if fc != 0x06:
            raise RuntimeError("Adp1000Backend: FC mismatch 0x%02X" % fc)
        return req, rsp

    @staticmethod
    def _to_u16(value, name):
        val = int(round(value))
        if val < 1 or val > 0xFFFF:
            raise RuntimeError(
                "Adp1000Backend: %s out of range after rounding: %s" % (name, value))
        return val

    # ================================================================
    #  Wait helpers
    # ================================================================

    def _wait_init_done(self, timeout_s):
        """Poll REG_RESET_STATUS (0x020D) until homing completes."""
        deadline = self._monotonic() + timeout_s
        last = None
        while self._monotonic() < deadline:
            try:
                regs = self._fc03_read_regs(
                    self.REG_RESET_STATUS, 1,
                    timeout_s=min(self._response_timeout, 1.0))
                last = regs[0]
            except Exception:
                self._sleep(self._poll_interval)
                continue
            if last == 0x0001:
                return
            if last == 0x0002:
                raise RuntimeError(
                    "Adp1000Backend: initialization failed: homing failed (RESET_STATUS=0x0002)")
            self._sleep(self._poll_interval)
        raise RuntimeError(
            "Adp1000Backend: initialization timed out, last RESET_STATUS=0x%04X[%s]"
            % (last or 0, self.RESET_STATUS_TEXT.get(last, "Unknown")))

    def _wait_exec_done(self, timeout_s):
        """Poll REG_EXEC_STATUS (0x0203) until action completes."""
        deadline = self._monotonic() + timeout_s
        last = None
        while self._monotonic() < deadline:
            try:
                regs = self._fc03_read_regs(
                    self.REG_EXEC_STATUS, 1,
                    timeout_s=min(self._response_timeout, 1.0))
                last = regs[0]
            except Exception:
                self._sleep(self._poll_interval)
                continue
            if last == 0x0000:
                self._sleep(self._poll_interval)
                continue
            if last == 0x0001:
                return
            dev_text = ""
            try:
                dregs = self._fc03_read_regs(self.REG_DEVICE_STATUS, 1,
                                             timeout_s=min(self._response_timeout, 1.0))
                dv = dregs[0]
                dev_text = ", DEVICE_STATUS=0x%04X[%s]" % (
                    dv, self.DEVICE_STATUS_TEXT.get(dv, "Unknown"))
            except Exception:
                pass
            raise RuntimeError(
                "Adp1000Backend: action error EXEC_STATUS=0x%04X[%s]%s"
                % (last, self.EXEC_STATUS_TEXT.get(last, "Unknown"), dev_text))
        raise RuntimeError(
            "Adp1000Backend: action timed out, last EXEC_STATUS=0x%04X[%s]"
            % (last or 0, self.EXEC_STATUS_TEXT.get(last, "Unknown")))

    def _wait_liquid_detect_done(self, timeout_s):
        """Poll REG_DEVICE_STATUS (0x0204) until liquid detected or error."""
        deadline = self._monotonic() + timeout_s
        last = None
        while self._monotonic() < deadline:
            try:
                regs = self._fc03_read_regs(
                    self.REG_DEVICE_STATUS, 1,
                    timeout_s=min(self._response_timeout, 1.0))
                last = regs[0]
            except Exception:
                self._sleep(self._poll_interval)
                continue
            if last == 0x0003:
                return
            if last == 0x0004:
                raise RuntimeError(
                    "Adp1000Backend: liquid probe failed: max distance reached (DEVICE_STATUS=0x0004)")
            self._sleep(self._poll_interval)
        raise RuntimeError(
            "Adp1000Backend: liquid detect timed out, last DEVICE_STATUS=0x%04X[%s]"
            % (last or 0, self.DEVICE_STATUS_TEXT.get(last, "Unknown")))

    # ================================================================
    #  Public interface (called by PipetteDriver)
    # ================================================================

    def verify_connection(self):
        """Read version register to confirm communication."""
        regs = self._fc03_read_regs(self.REG_SW_VERSION, 2)
        version = (regs[0] << 16) | regs[1]
        logging.info(
            "Adp1000Backend: connection OK  VERSION=0x%08X(%d)", version, version)

    def read_tip(self):
        """Return True if TIP is present (REG_TIP_PRESENCE == 0x0001)."""
        regs = self._fc03_read_regs(self.REG_TIP_PRESENCE, 1)
        return regs[0] == 0x0001

    def read_liquid_detected(self):
        """
        Return True if liquid is detected (DEVICE_STATUS == 0x0003).
        Raises RuntimeError if device reports probe failure (0x0004).
        """
        regs = self._fc03_read_regs(self.REG_DEVICE_STATUS, 1)
        val = regs[0]
        if val == 0x0004:
            raise RuntimeError(
                "Pipette: ADP1000 liquid probe failed: max distance reached "
                "(DEVICE_STATUS=0x0004)")
        return val == 0x0003

    def read_volume_status(self):
        """Read used/available volume from 0x0205..0x0208 (nL units)."""
        regs = self._fc03_read_regs(self.REG_VOLUME_STATUS, 4)
        used_nl = (regs[0] << 16) | regs[1]
        available_nl = (regs[2] << 16) | regs[3]
        capacity_nl = used_nl + available_nl
        return {
            "device_type": "adp1000",
            "source": "0x0205",
            "used_nl": used_nl,
            "available_nl": available_nl,
            "capacity_nl": capacity_nl,
            "used_ul": used_nl / 1000.0,
            "available_ul": available_nl / 1000.0,
            "capacity_ul": capacity_nl / 1000.0,
        }

    def do_init(self, wait, timeout_s):
        """Write init command (0x020C=1) and optionally wait for completion."""
        self._fc06_write_reg(self.REG_INIT_CMD, 0x0001)
        if wait:
            self._wait_init_done(timeout_s)
        logging.info("Adp1000Backend: init sent, wait=%s", wait)

    def do_unload_tip(self, wait, timeout_s):
        """Trigger TIP unload/eject command (0x0230=1) and optionally wait."""
        self._fc06_write_reg(self.REG_UNLOAD_TIP, 0x0001)
        if wait:
            self._wait_exec_done(timeout_s)
        logging.info("Adp1000Backend: unload TIP sent, wait=%s", wait)

    def do_aspirate(self, volume_ul, speed_ul_s, wait, timeout_s):
        """Trigger aspirate. Optional speed is written before the volume command."""
        volume = self._to_u16(volume_ul, "aspirate volume")
        if speed_ul_s is not None:
            self._fc06_write_reg(
                self.REG_ASPIRATE_SPEED, self._to_u16(speed_ul_s, "aspirate speed"))
        self._fc06_write_reg(self.REG_ASPIRATE, volume)
        if wait:
            self._wait_exec_done(timeout_s)
        logging.info(
            "Adp1000Backend: aspirate sent volume=%d speed=%s wait=%s",
            volume, speed_ul_s, wait)

    def do_dispense(self, volume_ul, speed_ul_s, wait, timeout_s):
        """Trigger dispense. Optional speed is written before the volume command."""
        volume = self._to_u16(volume_ul, "dispense volume")
        if speed_ul_s is not None:
            self._fc06_write_reg(
                self.REG_DISPENSE_SPEED, self._to_u16(speed_ul_s, "dispense speed"))
        self._fc06_write_reg(self.REG_DISPENSE, volume)
        if wait:
            self._wait_exec_done(timeout_s)
        logging.info(
            "Adp1000Backend: dispense sent volume=%d speed=%s wait=%s",
            volume, speed_ul_s, wait)

    def do_liquid_detect(self, mode, wait, timeout_s):
        """PIPETTE_LIQUID_DETECT handler: write PRESSURE_DETECT mode, optionally wait."""
        self._fc06_write_reg(self.REG_PRESSURE_DETECT, mode)
        if wait:
            self._wait_liquid_detect_done(timeout_s)
        logging.info("Adp1000Backend: liquid detect started mode=%d wait=%s", mode, wait)

    def start_liquid_probe(self, mode):
        """
        Called by pipette_action before Z-axis liquid probing movement.
        Writes REG_PRESSURE_DETECT to activate pressure sensing.
        Idempotent: skips if already active.
        """
        if self._liq_probe_active:
            return
        self._fc06_write_reg(self.REG_PRESSURE_DETECT, mode)
        self._liq_probe_active = True
        logging.info("Adp1000Backend: pressure detect started mode=%d", mode)

    def stop_liquid_probe(self):
        """
        Called by pipette_action after Z-axis liquid probing movement.
        Writes REG_PRESSURE_DETECT=0 to stop sensing.
        Idempotent and failure-safe.
        """
        if not self._liq_probe_active:
            return
        self._liq_probe_active = False
        try:
            self._fc06_write_reg(self.REG_PRESSURE_DETECT, 0x0000)
            logging.info("Adp1000Backend: pressure detect stopped")
        except Exception as e:
            logging.warning("Adp1000Backend: failed to stop pressure detect: %s", e)
