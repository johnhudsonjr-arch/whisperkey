"""Key-name translation for the two Wayland backends.

Both tables are easy to extend wrongly and impossible to notice: a bad entry
does not raise, it just silently watches the wrong key. These pin the aliases
that matter and the shape of the fallbacks.
"""

import pytest

from whisperkey.evdev_input import code_for
from whisperkey.hotkey import accelerator_for


class TestAccelerator:
    @pytest.mark.parametrize("name,expected", [
        ("cmd_l", "Super_L"), ("cmd_r", "Super_R"), ("cmd", "Super_L"),
        ("ctrl_r", "Control_R"), ("alt_r", "Alt_R"), ("esc", "Escape"),
        ("caps_lock", "Caps_Lock"), ("space", "space"),
    ])
    def test_known_aliases(self, name, expected):
        assert accelerator_for(name) == expected

    def test_function_keys_uppercase(self):
        assert accelerator_for("f9") == "F9"
        assert accelerator_for("f12") == "F12"

    def test_single_characters_pass_through(self):
        assert accelerator_for("v") == "v"

    def test_empty_is_an_error(self):
        with pytest.raises(ValueError):
            accelerator_for("")

    def test_case_is_irrelevant(self):
        assert accelerator_for("CMD_L") == accelerator_for("cmd_l")


class TestEvdevCodes:
    @pytest.mark.parametrize("name,expected", [
        ("cmd_l", 125), ("cmd_r", 126), ("ctrl", 29), ("ctrl_r", 97),
        ("shift", 42), ("alt", 56), ("v", 47), ("f9", 67), ("esc", 1),
    ])
    def test_known_codes(self, name, expected):
        assert code_for(name) == expected

    def test_unknown_key_raises(self):
        with pytest.raises(ValueError):
            code_for("there_is_no_such_key")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            code_for("")

    def test_case_is_irrelevant(self):
        assert code_for("F9") == code_for("f9")

    def test_paste_combo_parts_all_resolve(self):
        """Every part of the default chords must map, or paste fails at runtime."""
        for combo in ("ctrl+v", "ctrl+shift+v"):
            for part in combo.split("+"):
                assert isinstance(code_for(part), int)
