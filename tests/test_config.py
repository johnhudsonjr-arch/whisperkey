"""Config merging.

The daemon must survive a hand-edited config: unknown keys should not crash it,
partial sections should keep their defaults, and a syntactically broken file
should degrade to defaults rather than leaving the user with no hotkey.
"""

import tempfile
import unittest
from pathlib import Path

from whisperkey import config as config_mod
from whisperkey.config import Config, DEFAULT_TOML, load, write_default


class MergeTests(unittest.TestCase):
    def _write(self, text):
        tmp = Path(tempfile.mkdtemp()) / "config.toml"
        tmp.write_text(text)
        return tmp

    def test_partial_section_keeps_other_defaults(self):
        cfg = load(self._write('[hotkey]\nkey = "alt_r"\n'))
        self.assertEqual(cfg.hotkey.key, "alt_r")
        self.assertEqual(cfg.hotkey.hold_min_ms, Config().hotkey.hold_min_ms)
        self.assertEqual(cfg.model.name, Config().model.name)

    def test_unknown_keys_are_ignored_not_fatal(self):
        cfg = load(self._write('[hotkey]\nkey = "f9"\nnonsense = 1\n'
                               '[nowhere]\nx = 2\n'))
        self.assertEqual(cfg.hotkey.key, "f9")

    def test_broken_toml_falls_back_to_defaults(self):
        cfg = load(self._write("[hotkey\nkey = "))
        self.assertEqual(cfg.hotkey.key, Config().hotkey.key)

    def test_missing_file_gives_defaults(self):
        cfg = load(Path(tempfile.mkdtemp()) / "absent.toml")
        # Left Command: this machine's Apple Magic Keyboard has no Right Ctrl,
        # so the shipped default has to be a key that actually exists on it.
        self.assertEqual(cfg.hotkey.key, "cmd_l")

    def test_nested_tables_load(self):
        cfg = load(self._write(
            '[vocab]\nwords = ["Anthropic"]\n'
            '[vocab.replacements]\n"clod" = "Claude"\n'
        ))
        self.assertEqual(cfg.vocab.words, ["Anthropic"])
        self.assertEqual(cfg.vocab.replacements["clod"], "Claude")


class DefaultTemplateTests(unittest.TestCase):
    def test_shipped_template_parses_and_matches_dataclass_defaults(self):
        tmp = Path(tempfile.mkdtemp()) / "config.toml"
        tmp.write_text(DEFAULT_TOML)
        cfg, defaults = load(tmp), Config()
        # A template that drifts from the dataclasses silently changes behaviour.
        self.assertEqual(cfg.hotkey.key, defaults.hotkey.key)
        self.assertEqual(cfg.hotkey.hold_min_ms, defaults.hotkey.hold_min_ms)
        self.assertEqual(cfg.model.name, defaults.model.name)
        self.assertEqual(cfg.model.beam_size, defaults.model.beam_size)
        self.assertEqual(cfg.audio.sample_rate, defaults.audio.sample_rate)
        self.assertEqual(cfg.inject.method, defaults.inject.method)
        self.assertEqual(cfg.cleanup.enabled, defaults.cleanup.enabled)

    def test_write_default_does_not_clobber_an_existing_file(self):
        tmp = Path(tempfile.mkdtemp()) / "config.toml"
        tmp.write_text("# mine\n")
        write_default(tmp)
        self.assertEqual(tmp.read_text(), "# mine\n")
        write_default(tmp, force=True)
        self.assertIn("WhisperKey configuration", tmp.read_text())


class PathTests(unittest.TestCase):
    def test_paths_are_xdg_based(self):
        self.assertTrue(str(config_mod.CONFIG_PATH).endswith(
            "whisperkey/config.toml"))
        self.assertTrue(str(config_mod.SOCKET_PATH).endswith("whisperkey.sock"))


if __name__ == "__main__":
    unittest.main()
