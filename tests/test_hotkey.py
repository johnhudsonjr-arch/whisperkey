"""Hotkey semantics.

These are the trickiest logic in the project -- hold, tap, and double-tap-to-latch
all overlap on a single key -- so the state machine takes an injectable clock and
is tested with no X server and no keyboard.
"""

import unittest

from whisperkey.hotkey import Action, HotkeyMachine, State


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, ms):
        self.now += ms / 1000.0


class HoldToTalkTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.machine = HotkeyMachine(hold_min_ms=250, double_tap_ms=400,
                                     clock=self.clock)

    def test_hold_records_then_transcribes(self):
        self.assertIs(self.machine.on_press(), Action.START)
        self.assertTrue(self.machine.recording)
        self.clock.advance(1200)
        self.assertIs(self.machine.on_release(), Action.STOP)
        self.assertFalse(self.machine.recording)
        self.assertIs(self.machine.state, State.IDLE)

    def test_recording_starts_immediately_not_after_a_debounce(self):
        # Audio must begin on the press itself, otherwise the first syllable
        # is lost. A too-short press is discarded later instead.
        self.assertIs(self.machine.on_press(), Action.START)

    def test_hold_exactly_at_threshold_counts_as_a_hold(self):
        self.machine.on_press()
        self.clock.advance(250)
        self.assertIs(self.machine.on_release(), Action.STOP)

    def test_short_tap_is_discarded(self):
        self.machine.on_press()
        self.clock.advance(80)
        self.assertIs(self.machine.on_release(), Action.DISCARD)
        self.assertIs(self.machine.state, State.TAP_PENDING)


class DoubleTapLatchTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.machine = HotkeyMachine(hold_min_ms=250, double_tap_ms=400,
                                     clock=self.clock)

    def _tap(self, ms=80):
        self.machine.on_press()
        self.clock.advance(ms)
        return self.machine.on_release()

    def test_double_tap_latches_and_next_press_stops(self):
        self.assertIs(self._tap(), Action.DISCARD)
        self.clock.advance(150)
        self.assertIs(self.machine.on_press(), Action.START)
        self.assertIs(self.machine.state, State.LATCHED)

        # Releasing the second tap must NOT stop a latched recording.
        self.clock.advance(60)
        self.assertIs(self.machine.on_release(), Action.NONE)
        self.assertTrue(self.machine.recording)

        # Long pause while dictating hands-free.
        self.clock.advance(9000)
        self.assertTrue(self.machine.recording)

        self.assertIs(self.machine.on_press(), Action.STOP)
        self.assertIs(self.machine.state, State.IDLE)

    def test_second_tap_after_the_window_starts_a_fresh_hold(self):
        self._tap()
        self.clock.advance(900)  # window expired
        self.assertIs(self.machine.on_press(), Action.START)
        self.assertIs(self.machine.state, State.HOLD)
        self.clock.advance(600)
        self.assertIs(self.machine.on_release(), Action.STOP)

    def test_lone_tap_leaves_nothing_recording(self):
        self._tap()
        self.assertFalse(self.machine.recording)

    def test_release_while_latched_is_ignored_repeatedly(self):
        self._tap()
        self.clock.advance(100)
        self.machine.on_press()
        for _ in range(3):
            self.assertIs(self.machine.on_release(), Action.NONE)
        self.assertTrue(self.machine.recording)


class AbortTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.machine = HotkeyMachine(clock=self.clock)

    def test_abort_discards_a_held_recording(self):
        self.machine.on_press()
        self.clock.advance(2000)
        self.assertIs(self.machine.on_abort(), Action.DISCARD)
        self.assertFalse(self.machine.recording)

    def test_abort_discards_a_latched_recording(self):
        self.machine.on_press()
        self.clock.advance(50)
        self.machine.on_release()
        self.clock.advance(100)
        self.machine.on_press()
        self.assertIs(self.machine.state, State.LATCHED)
        self.assertIs(self.machine.on_abort(), Action.DISCARD)
        self.assertIs(self.machine.state, State.IDLE)

    def test_abort_when_idle_does_nothing(self):
        self.assertIs(self.machine.on_abort(), Action.NONE)

    def test_release_without_press_is_harmless(self):
        self.assertIs(self.machine.on_release(), Action.NONE)


if __name__ == "__main__":
    unittest.main()
