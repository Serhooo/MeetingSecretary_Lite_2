import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import meeting_secretary_app as app_module
from PySide6.QtWidgets import QApplication


class FakeLlama:
    """Токенизатор, считающий токены по байтам UTF-8 (детерминированно)."""

    def tokenize(self, data, add_bos=False, special=False):
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("tokenize ожидает bytes")
        return list(range(len(data)))


class LlmContextFitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_thread(self, n_ctx=1024):
        thread = app_module.LLMProtocolThread(backend="local")
        thread.n_ctx = n_ctx
        thread.llm = FakeLlama()
        return thread

    def test_n_ctx_can_be_overridden_via_environment(self):
        with patch.dict(os.environ, {"MEETING_LLM_N_CTX": "4096"}):
            thread = app_module.LLMProtocolThread(backend="local")
        self.assertEqual(thread.n_ctx, 4096)

    def test_n_ctx_defaults_to_16384_for_invalid_value(self):
        with patch.dict(os.environ, {"MEETING_LLM_N_CTX": "not-a-number"}):
            thread = app_module.LLMProtocolThread(backend="local")
        self.assertEqual(thread.n_ctx, 16384)

    def test_count_tokens_uses_model_tokenizer(self):
        thread = self.make_thread()
        text = "Speaker 1: we discussed the status of task"
        self.assertEqual(thread._count_tokens(text), len(text.encode("utf-8")))

    def test_count_tokens_falls_back_to_estimate_without_model(self):
        thread = app_module.LLMProtocolThread(backend="local")
        self.assertEqual(thread._count_tokens(""), 0)
        self.assertEqual(thread._count_tokens("abcd"), 2)
        self.assertEqual(thread._count_tokens("abcde"), 3)

    def test_fit_text_returns_original_text_when_it_fits(self):
        thread = self.make_thread(n_ctx=1024)
        text = "Short transcript line"
        self.assertEqual(thread._fit_text(text, 1000), text)

    def test_fit_text_keeps_head_and_tail_within_budget(self):
        thread = self.make_thread(n_ctx=1024)
        lines = [f"[10:00:{index:02d}] Speaker: line {index}" for index in range(400)]
        text = "\n".join(lines)

        fitted = thread._fit_text(text, 500)

        self.assertLessEqual(thread._count_tokens(fitted), 500)
        self.assertTrue(fitted.startswith(lines[0]))
        self.assertTrue(fitted.endswith(lines[-1]))
        self.assertIn("пропущена из-за ограничения", fitted)
        self.assertLess(len(fitted), len(text))

    def test_fit_messages_leaves_short_prompt_unchanged(self):
        thread = self.make_thread(n_ctx=16384)
        messages = [
            {"role": "system", "content": "Protocol instructions"},
            {"role": "user", "content": "Transcript: hello"},
        ]

        fitted = thread._fit_messages(messages, max_tokens=800)

        self.assertIs(fitted, messages)

    def test_fit_messages_trims_long_user_message_to_budget(self):
        thread = self.make_thread(n_ctx=1024)
        system_prompt = "Protocol instructions " * 5
        long_transcript = "\n".join(
            f"[10:00:{index:02d}] Speaker: line {index}" for index in range(500)
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": long_transcript},
        ]
        max_tokens = 200
        reserve = 256 + 32 * (len(messages) + 1)
        budget = thread.n_ctx - max_tokens - reserve

        fitted = thread._fit_messages(messages, max_tokens=max_tokens)

        total = sum(
            thread._count_tokens(str(message["content"]))
            for message in fitted
        )
        self.assertLessEqual(total, budget)
        self.assertEqual(fitted[0]["content"], system_prompt)
        self.assertIn("пропущена из-за ограничения", fitted[1]["content"])
        # Исходные сообщения не изменяются.
        self.assertEqual(messages[1]["content"], long_transcript)

    def test_fit_messages_raises_clear_error_when_prompt_cannot_fit(self):
        thread = self.make_thread(n_ctx=2048)
        messages = [
            {"role": "system", "content": "Huge system prompt " * 300},
            {"role": "user", "content": "tiny transcript"},
        ]

        with self.assertRaises(ValueError):
            thread._fit_messages(messages, max_tokens=100)

    def test_fit_messages_skips_api_backend(self):
        thread = app_module.LLMProtocolThread(backend="api")
        thread.n_ctx = 512
        thread.llm = FakeLlama()
        messages = [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "x" * 10000},
        ]

        fitted = thread._fit_messages(messages, max_tokens=100)

        self.assertIs(fitted, messages)

    def test_chat_completion_fits_prompt_for_local_backend(self):
        thread = self.make_thread(n_ctx=1024)
        captured = {}

        def fake_create_chat_completion(messages=None, max_tokens=None, temperature=None):
            captured["messages"] = messages
            return {"choices": [{"message": {"content": "protocol"}}]}

        thread.llm.create_chat_completion = fake_create_chat_completion
        long_transcript = "\n".join(
            f"[10:00:{index:02d}] Speaker: line {index}" for index in range(500)
        )

        result = thread._chat_completion(
            messages=[
                {"role": "system", "content": "Protocol instructions"},
                {"role": "user", "content": f"Transcript:\n{long_transcript}"},
            ],
            max_tokens=200,
            temperature=0.2,
        )

        self.assertEqual(result, "protocol")
        reserve = 256 + 32 * 3
        budget = thread.n_ctx - 200 - reserve
        total = sum(
            thread._count_tokens(str(message["content"]))
            for message in captured["messages"]
        )
        self.assertLessEqual(total, budget)


if __name__ == "__main__":
    unittest.main()

