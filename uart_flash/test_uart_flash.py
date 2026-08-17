"""Regression tests for the non-destructive UART probe reader."""

import importlib.util
import pathlib
import sys
import types
import unittest
from unittest import mock


try:
    import serial  # noqa: F401
except ModuleNotFoundError:
    # Reader tests do not need pyserial; provide only enough for module import.
    sys.modules["serial"] = types.SimpleNamespace(Serial=None)


MODULE_PATH = pathlib.Path(__file__).with_name("uart_flash.py")
SPEC = importlib.util.spec_from_file_location("uart_flash_under_test", MODULE_PATH)
uart_flash = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(uart_flash)


class SequencedReader:
    """A serial fake that returns initial timeout reads before a frame."""

    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.read_calls = 0

    def read(self, _size):
        self.read_calls += 1
        return next(self.chunks, b"")


class ReaderPumpTest(unittest.TestCase):
    def test_pump_waits_through_empty_reads_for_later_frame(self):
        expected = (uart_flash.MSG_CMD_OTA_BLOCK_REQUEST, b"\x00\x00\x00\x00\x28")
        serial_fake = SequencedReader([
            b"",
            b"",
            uart_flash.frame(*expected),
        ])

        # The clock sequence is explicit: two timeout reads, one frame, deadline.
        with mock.patch.object(
                uart_flash.time, "monotonic",
                side_effect=[0.0, 0.0, 0.0, 0.0, 1.0]):
            messages = list(uart_flash.Reader(serial_fake).pump(1.0))

        self.assertEqual(messages, [expected])
        self.assertGreaterEqual(serial_fake.read_calls, 3)


if __name__ == "__main__":
    unittest.main()
