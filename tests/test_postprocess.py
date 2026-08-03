"""Vocabulary fixups, prompt building, and the LLM cleanup guard rails."""

import json
import unittest
from unittest import mock

from whisperkey.config import CleanupConfig, ModelConfig, VocabConfig
from whisperkey.postprocess import (MAX_PROMPT_CHARS, apply_replacements,
                                    build_initial_prompt, cleanup_with_ollama,
                                    process)


class ReplacementTests(unittest.TestCase):
    def test_replacement_is_case_insensitive(self):
        table = {"clod code": "Claude Code"}
        self.assertEqual(apply_replacements("open Clod Code now", table),
                         "open Claude Code now")

    def test_only_whole_words_are_replaced(self):
        table = {"cat": "dog"}
        self.assertEqual(apply_replacements("concatenate the cat", table),
                         "concatenate the dog")

    def test_longer_phrases_win_over_shorter_ones(self):
        table = {"claude": "Claude", "claude code": "Claude Code"}
        self.assertEqual(apply_replacements("use claude code", table),
                         "use Claude Code")

    def test_regex_metacharacters_are_literal(self):
        self.assertEqual(apply_replacements("c++ rocks", {"c++": "C++"}),
                         "C++ rocks")

    def test_empty_inputs_are_safe(self):
        self.assertEqual(apply_replacements("", {"a": "b"}), "")
        self.assertEqual(apply_replacements("hello", {}), "hello")


class PromptTests(unittest.TestCase):
    def test_vocabulary_is_folded_into_the_prompt(self):
        prompt = build_initial_prompt(
            ModelConfig(initial_prompt="Technical dictation."),
            VocabConfig(words=["Kubernetes", "Anthropic"]),
        )
        self.assertIn("Technical dictation.", prompt)
        self.assertIn("Kubernetes", prompt)

    def test_prompt_is_truncated_to_whispers_window(self):
        prompt = build_initial_prompt(
            ModelConfig(), VocabConfig(words=[f"word{i}" for i in range(500)])
        )
        self.assertLessEqual(len(prompt), MAX_PROMPT_CHARS + 2)

    def test_no_vocabulary_gives_an_empty_prompt(self):
        self.assertEqual(build_initial_prompt(ModelConfig(), VocabConfig()), "")


def _response(text):
    """Fake urlopen context manager returning an Ollama-shaped payload."""
    handle = mock.MagicMock()
    handle.read.return_value = json.dumps({"response": text}).encode()
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = handle
    return ctx


class CleanupGuardTests(unittest.TestCase):
    def setUp(self):
        self.cfg = CleanupConfig(enabled=True)
        self.text = "um so i think we should ship it tomorrow"

    def test_reasonable_rewrite_is_accepted(self):
        cleaned = "So I think we should ship it tomorrow."
        with mock.patch("urllib.request.urlopen", return_value=_response(cleaned)):
            self.assertEqual(cleanup_with_ollama(self.text, self.cfg), cleaned)

    def test_model_answering_the_text_is_rejected(self):
        # The classic failure: the model replies instead of tidying.
        essay = "Shipping tomorrow is a great idea! " * 12
        with mock.patch("urllib.request.urlopen", return_value=_response(essay)):
            self.assertEqual(cleanup_with_ollama(self.text, self.cfg), self.text)

    def test_suspiciously_short_rewrite_is_rejected(self):
        with mock.patch("urllib.request.urlopen", return_value=_response("Ship it.")):
            self.assertEqual(cleanup_with_ollama(self.text, self.cfg), self.text)

    def test_surrounding_quotes_are_stripped(self):
        quoted = '"So I think we should ship it tomorrow."'
        with mock.patch("urllib.request.urlopen", return_value=_response(quoted)):
            self.assertEqual(cleanup_with_ollama(self.text, self.cfg),
                             "So I think we should ship it tomorrow.")

    def test_unreachable_ollama_falls_back_to_the_raw_transcript(self):
        import urllib.error

        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("refused")):
            self.assertEqual(cleanup_with_ollama(self.text, self.cfg), self.text)

    def test_malformed_json_falls_back(self):
        handle = mock.MagicMock()
        handle.read.return_value = b"not json"
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = handle
        with mock.patch("urllib.request.urlopen", return_value=ctx):
            self.assertEqual(cleanup_with_ollama(self.text, self.cfg), self.text)

    def test_empty_response_falls_back(self):
        with mock.patch("urllib.request.urlopen", return_value=_response("")):
            self.assertEqual(cleanup_with_ollama(self.text, self.cfg), self.text)


class PipelineTests(unittest.TestCase):
    def test_cleanup_is_skipped_when_disabled(self):
        vocab = VocabConfig(replacements={"clod": "Claude"})
        with mock.patch("urllib.request.urlopen") as fake:
            out = process("clod is here", vocab, CleanupConfig(enabled=False))
        fake.assert_not_called()
        self.assertEqual(out, "Claude is here")

    def test_process_never_raises_on_a_bad_table(self):
        self.assertTrue(process("hello", VocabConfig(replacements={"[": "x"}),
                                CleanupConfig()))


if __name__ == "__main__":
    unittest.main()
