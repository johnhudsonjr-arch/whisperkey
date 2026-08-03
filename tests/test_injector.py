"""Choosing the right paste chord for the focused window.

Regression coverage for a bug that produced no error of any kind: GNOME
Terminal reports WM_CLASS "gnome-terminal" while the shipped list only held
"gnome-terminal-server". The one-directional substring test could not match the
shorter string, so pastes were sent as Ctrl+V, which a terminal ignores. The
daemon logged a successful injection and no text ever appeared.

None of this touches X, so it runs anywhere.
"""

import unittest

from whisperkey.config import InjectConfig
from whisperkey.injector import Injector


class TerminalDetectionTests(unittest.TestCase):
    def setUp(self):
        self.injector = Injector(InjectConfig())

    def test_gnome_terminal_shorter_form_is_detected(self):
        # The exact failure seen in the wild.
        self.assertTrue(self.injector.is_terminal("gnome-terminal"))
        self.assertEqual(self.injector._combo_for("gnome-terminal"),
                         "ctrl+shift+v")

    def test_gnome_terminal_process_form_is_detected(self):
        self.assertTrue(self.injector.is_terminal("gnome-terminal-server"))

    def test_matching_works_in_both_directions(self):
        cfg = InjectConfig(terminal_classes=["someterm"])
        injector = Injector(cfg)
        self.assertTrue(injector.is_terminal("someterm-server"))  # longer window
        self.assertTrue(injector.is_terminal("someterm"))         # exact
        cfg2 = InjectConfig(terminal_classes=["someterm-server"])
        self.assertTrue(Injector(cfg2).is_terminal("someterm"))   # shorter window

    def test_other_known_terminals(self):
        for wm in ("kitty", "alacritty", "konsole", "xterm", "tilix", "foot",
                   "ptyxis", "org.wezfurlong.wezterm"):
            with self.subTest(wm=wm):
                self.assertTrue(self.injector.is_terminal(wm))
                self.assertEqual(self.injector._combo_for(wm), "ctrl+shift+v")

    def test_normal_applications_get_plain_paste(self):
        for wm in ("firefox", "code", "slack", "org.gnome.texteditor",
                   "chromium-browser", "thunderbird"):
            with self.subTest(wm=wm):
                self.assertFalse(self.injector.is_terminal(wm))
                self.assertEqual(self.injector._combo_for(wm), "ctrl+v")

    def test_detection_is_case_insensitive(self):
        self.assertTrue(self.injector.is_terminal("GNOME-Terminal"))

    def test_unknown_window_falls_back_to_plain_paste(self):
        self.assertEqual(self.injector._combo_for(""), "ctrl+v")
        self.assertFalse(self.injector.is_terminal(""))

    def test_empty_entries_do_not_match_everything(self):
        # A stray "" in the user's config must not turn every app into a
        # terminal, which would break paste everywhere.
        injector = Injector(InjectConfig(terminal_classes=["", "kitty"]))
        self.assertFalse(injector.is_terminal("firefox"))
        self.assertTrue(injector.is_terminal("kitty"))


class MethodSelectionTests(unittest.TestCase):
    def test_per_app_override_wins(self):
        cfg = InjectConfig(per_app_method={"jetbrains-idea": "type"})
        injector = Injector(cfg)
        self.assertEqual(injector._method_for("jetbrains-idea"), "type")
        self.assertEqual(injector._method_for("firefox"), "paste")


if __name__ == "__main__":
    unittest.main()
