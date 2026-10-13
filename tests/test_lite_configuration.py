import os
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import meeting_secretary_app as app_module
from PySide6.QtWidgets import QApplication


class FakeSoundDevice:
    default = types.SimpleNamespace(device=(0, 1))

    @staticmethod
    def query_devices():
        return [
            {
                "name": "Default microphone",
                "max_input_channels": 1,
                "default_samplerate": 48000,
            },
            {
                "name": "Unused second microphone",
                "max_input_channels": 2,
                "default_samplerate": 44100,
            },
        ]


class FakePyAudioInstance:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get_loopback_device_info_generator(self):
        return iter(
            [
                {
                    "name": "HDMI Output [Loopback]",
                    "index": 12,
                    "maxInputChannels": 2,
                    "defaultSampleRate": 48000,
                },
                {
                    "name": "Speakers Realtek [Loopback]",
                    "index": 18,
                    "maxInputChannels": 2,
                    "defaultSampleRate": 44100,
                },
            ]
        )

    def get_default_output_device_info(self):
        return {"name": "Speakers Realtek"}


class FakePyAudio:
    PyAudio = FakePyAudioInstance


class LiteConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_only_default_windows_mic_and_default_system_audio_are_listed(self):
        with (
            patch.object(app_module, "sd", FakeSoundDevice),
            patch.object(app_module, "pyaudio", FakePyAudio),
        ):
            sources = app_module.enumerate_audio_sources()

        self.assertEqual([source["kind"] for source in sources], ["input", "loopback"])
        self.assertEqual(sources[0]["id"], "default")
        self.assertEqual(sources[1]["device"], 18)
        self.assertEqual(
            sources[1]["label"],
            "Системный звук Windows (выход по умолчанию)",
        )

    def test_lite_window_offers_whisper_and_gigaam_with_readable_tooltips(self):
        with (
            patch.object(app_module, "load_api_credential", return_value=""),
            patch.object(
                app_module,
                "enumerate_audio_sources",
                return_value=[
                    {
                        "id": "default",
                        "label": "Микрофон Windows по умолчанию",
                        "kind": "input",
                        "device": None,
                        "channels": 1,
                        "sample_rate": 16000,
                    }
                ],
            ),
        ):
            window = app_module.MeetingSecretaryMainWindow()
        try:
            self.assertIn("ИИ-Секретарь Lite", window.windowTitle())
            self.assertIn("RAM", window.windowTitle())
            available_asr = {
                window.combo_whisper.itemData(index)
                for index in range(window.combo_whisper.count())
            }
            self.assertEqual(
                available_asr,
                {
                    "tiny",
                    "small",
                    "medium",
                    "large-v3-turbo",
                    "gigaam-v3-e2e-rnnt",
                },
            )
            self.assertTrue(window.combo_whisper.isEnabled())
            self.assertEqual(window.settings.applicationName(), "MeetingSecretaryLite2")
            window.combo_diarization.blockSignals(True)
            window.combo_diarization.setCurrentIndex(
                window.combo_diarization.findData(app_module.DIARIZATION_PROFILES)
            )
            window.combo_diarization.blockSignals(False)
            window.update_diarization_controls()
            self.assertIn(
                app_module.DIARIZATION_DISABLED,
                [
                    window.combo_diarization.itemData(index)
                    for index in range(window.combo_diarization.count())
                ],
            )
            self.assertTrue(window.btn_pause.isHidden())
            disabled_index = window.combo_diarization.findData(
                app_module.DIARIZATION_DISABLED
            )
            window.combo_diarization.blockSignals(True)
            window.combo_diarization.setCurrentIndex(disabled_index)
            window.combo_diarization.blockSignals(False)
            window.update_diarization_controls()
            self.assertFalse(window.btn_pause.isHidden())
            self.assertFalse(window.btn_rename_speaker.isEnabled())
            self.assertFalse(window.btn_manage_speakers.isEnabled())
            self.assertTrue(hasattr(window, "btn_agenda"))
            self.assertEqual(
                window.combo_gguf.currentData(),
                os.path.join(
                    app_module.APP_DIR,
                    "models",
                    "Gemma-4-E2B-Uncensored-HauhauCS-Aggressive-Q6_K_P.gguf",
                ),
            )
            self.assertFalse(hasattr(window, "btn_add_term"))
            self.assertFalse(hasattr(app_module, "TermsDictionaryDialog"))
            self.assertIn("QToolTip", self.app.styleSheet())
            self.assertIn("#263238", self.app.styleSheet())
        finally:
            window.close()
            self.app.processEvents()

    def test_whisper_backend_transcribes_timestamped_segments(self):
        class FakeSegment:
            text = "Решение принято"
            start = 0.25
            end = 1.75

        transcribe = Mock(return_value=([FakeSegment()], None))
        worker = app_module.ASRWorkerThread(
            model_size="small",
            diarization_mode=app_module.DIARIZATION_DISABLED,
        )
        worker.model = types.SimpleNamespace(transcribe=transcribe)

        utterances = worker._transcribe_phrase(
            app_module.np.ones(
                app_module.SAMPLE_RATE * 2,
                dtype=app_module.np.float32,
            )
        )

        self.assertEqual(worker.model_backend, "whisper")
        self.assertEqual(utterances, [["Спикер не определён", "Решение принято", 1.5]])
        transcribe.assert_called_once()
        self.assertEqual(transcribe.call_args.kwargs["language"], "ru")
        self.assertEqual(transcribe.call_args.kwargs["beam_size"], 3)

    def test_http_api_is_allowed_only_for_loopback_and_private_network_addresses(self):
        for host in (
            "localhost",
            "127.0.0.1",
            "::1",
            "10.97.120.204",
            "172.16.0.1",
            "172.31.255.254",
            "192.168.1.20",
            "fd12:3456::1",
        ):
            with self.subTest(host=host):
                self.assertTrue(app_module.is_local_network_host(host))

        for host in (
            "172.15.255.255",
            "172.32.0.1",
            "192.169.1.1",
            "8.8.8.8",
            "public-api.example.com",
        ):
            with self.subTest(host=host):
                self.assertFalse(app_module.is_local_network_host(host))

    def test_lan_http_api_completion_is_allowed_but_public_http_is_rejected(self):
        worker = app_module.LLMProtocolThread(
            backend="api",
            api_endpoint="http://10.97.120.204:1234",
            api_model="qwen/qwen3.5-9b",
            api_key="local-key",
        )
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": "Протокол готов"}}]
        }
        with patch.object(app_module.httpx, "post", return_value=response) as post:
            result = worker._chat_completion([], max_tokens=100, temperature=0.2)

        self.assertEqual(result, "Протокол готов")
        self.assertEqual(
            post.call_args.args[0],
            "http://10.97.120.204:1234/v1/chat/completions",
        )
        self.assertEqual(post.call_args.kwargs["json"]["model"], "qwen/qwen3.5-9b")

        self.assertEqual(
            app_module.openai_chat_completions_url(
                "http://10.97.120.204:1234/v1/"
            ),
            "http://10.97.120.204:1234/v1/chat/completions",
        )
        self.assertEqual(
            app_module.openai_chat_completions_url(
                "http://10.97.120.204:1234/v1/chat/completions"
            ),
            "http://10.97.120.204:1234/v1/chat/completions",
        )

        worker.api_endpoint = "http://8.8.8.8:1234/v1"
        with self.assertRaisesRegex(ValueError, "Для публичного API требуется HTTPS"):
            worker._chat_completion([], max_tokens=100, temperature=0.2)

    def test_fast_asr_skips_speaker_embeddings_and_labels_text_unattributed(self):
        worker = app_module.ASRWorkerThread(
            diarization_mode=app_module.DIARIZATION_DISABLED
        )
        worker.model = types.SimpleNamespace(
            recognize=lambda *_args, **_kwargs: types.SimpleNamespace(
                text="Текст без определения голоса"
            )
        )
        worker.embedder.get_speaker_timeline = Mock(
            side_effect=AssertionError("Diarization must be skipped")
        )

        utterances = worker._transcribe_phrase(
            app_module.np.ones(app_module.SAMPLE_RATE * 2, dtype=app_module.np.float32)
        )

        self.assertEqual(utterances[0][0], "Спикер не определён")
        self.assertEqual(utterances[0][1], "Текст без определения голоса")
        worker.embedder.get_speaker_timeline.assert_not_called()

    def test_fast_asr_emits_after_short_chunk_without_loading_diarization_model(self):
        recognized_audio_lengths = []

        def recognize(audio, **_kwargs):
            recognized_audio_lengths.append(len(audio))
            return types.SimpleNamespace(text="Быстро распознанный блок")

        fake_model = types.SimpleNamespace(
            recognize=recognize
        )
        fake_onnx_asr = types.SimpleNamespace(load_model=lambda _name: fake_model)
        worker = app_module.ASRWorkerThread(
            chunk_seconds=1,
            diarization_mode=app_module.DIARIZATION_DISABLED,
        )
        worker.processing = True
        worker.embedder.initialize = Mock(
            side_effect=AssertionError("Diarization model must not be initialized")
        )
        worker.embedder.get_speaker_timeline = Mock(
            side_effect=AssertionError("Speaker embeddings must not be computed")
        )
        emitted = []
        direct_connection = app_module.Qt.ConnectionType.DirectConnection
        worker.model_ready.connect(
            lambda: [
                worker.audio_queue.put(
                    app_module.np.ones(
                        app_module.SAMPLE_RATE,
                        dtype=app_module.np.float32,
                    )
                )
                for _ in range(3)
            ] + [worker.audio_queue.put(None)],
            direct_connection,
        )
        worker.text_recognized.connect(
            lambda speaker, text, duration: emitted.append((speaker, text, duration)),
            direct_connection,
        )
        worker.processing_finished.connect(
            lambda: setattr(worker, "running", False),
            direct_connection,
        )

        with patch.dict("sys.modules", {"onnx_asr": fake_onnx_asr}):
            worker.start()
            self.assertTrue(worker.wait(10000), "ASR worker did not finish the queued test audio")

        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0][0], "Спикер не определён")
        self.assertEqual(emitted[0][1], "Быстро распознанный блок")
        self.assertEqual(
            recognized_audio_lengths,
            [3 * app_module.SAMPLE_RATE],
            "Fast mode should transcribe one combined three-second block, not three one-second pieces",
        )
        worker.embedder.initialize.assert_not_called()
        worker.embedder.get_speaker_timeline.assert_not_called()

    def test_fast_mode_pauses_flushes_and_marks_resume_time(self):
        with (
            patch.object(app_module, "load_api_credential", return_value=""),
            patch.object(
                app_module,
                "enumerate_audio_sources",
                return_value=[
                    {
                        "id": "default",
                        "label": "Микрофон Windows по умолчанию",
                        "kind": "input",
                        "device": None,
                        "channels": 1,
                        "sample_rate": 16000,
                    }
                ],
            ),
        ):
            window = app_module.MeetingSecretaryMainWindow()
        try:
            window.combo_diarization.setCurrentIndex(
                window.combo_diarization.findData(app_module.DIARIZATION_DISABLED)
            )
            window.audio_thread = Mock()
            window.asr_thread = Mock()
            window.recording_active = True
            window.save_recovery_snapshot = Mock()

            window.toggle_recording_pause()
            self.assertTrue(window._pause_pending)
            window.audio_thread.set_paused.assert_called_once_with(True)

            window.on_audio_pause_ready()
            window.asr_thread.finish_input.assert_called_once_with()
            window.on_asr_processing_finished()
            self.assertTrue(window.recording_paused)
            self.assertEqual(window.btn_pause.text(), "▶ Продолжить")

            window.toggle_recording_pause()
            self.assertFalse(window.recording_paused)
            window.asr_thread.start_processing.assert_called_once_with()
            window.audio_thread.set_paused.assert_called_with(False)
            self.assertIn(
                "Продолжение записи после паузы",
                window.txt_transcript.toPlainText(),
            )
            self.assertTrue(window.full_transcript[-1]["marker"])
            self.assertRegex(
                window.full_transcript[-1]["timestamp"],
                r"^\d{2}:\d{2}:\d{2}$",
            )

            window.asr_thread.diarization_enabled = False
            window.on_text_recognized("Спикер не определён", "Продолженная речь", 1.0)
            window.on_text_recognized("Спикер не определён", "и ещё слова", 1.0)
            self.assertIn(
                "--- [",
                window.txt_transcript.toPlainText(),
            )
            self.assertEqual(
                window.txt_transcript.toPlainText().splitlines()[-1],
                "Продолженная речь и ещё слова",
            )
            self.assertNotIn("Спикер не определён", window.txt_transcript.toPlainText())
            self.assertEqual(
                sum(bool(entry.get("fast_text")) for entry in window.full_transcript),
                1,
            )
        finally:
            window.recording_active = False
            window.audio_thread = None
            window.asr_thread = None
            window.close()
            self.app.processEvents()

    def test_fast_mode_keeps_speech_in_one_plain_untimed_text_block(self):
        with (
            patch.object(app_module, "load_api_credential", return_value=""),
            patch.object(
                app_module,
                "enumerate_audio_sources",
                return_value=[
                    {
                        "id": "default",
                        "label": "Микрофон Windows по умолчанию",
                        "kind": "input",
                        "device": None,
                        "channels": 1,
                        "sample_rate": 16000,
                    }
                ],
            ),
        ):
            window = app_module.MeetingSecretaryMainWindow()
        try:
            window.asr_thread = types.SimpleNamespace(diarization_enabled=False)
            window.on_text_recognized("Спикер не определён", "Ну", 1.0)
            window.on_text_recognized("Спикер не определён", "А нафиге?", 1.0)
            window.on_text_recognized(
                "Спикер не определён", "Сериал моноблог.", 1.0
            )
            window.on_text_recognized(
                "Спикер не определён", "У нас ведь нету такого.", 1.0
            )

            self.assertEqual(
                window.txt_transcript.toPlainText(),
                "Ну А нафиге? Сериал моноблог. У нас ведь нету такого.",
            )
            self.assertEqual(len(window.full_transcript), 1)
            self.assertTrue(window.full_transcript[0]["fast_text"])
            self.assertEqual(window.full_transcript[0]["timestamp"], "")
        finally:
            window.asr_thread = None
            window.close()
            self.app.processEvents()

    def test_audio_capture_suppresses_paused_audio_and_flushes_before_pause_signal(self):
        stream_ready = app_module.threading.Event()
        pause_ready = app_module.threading.Event()
        emitted_chunks = []

        class FakeInputStream:
            current = None

            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]
                type(self).current = self
                stream_ready.set()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def feed(self, value):
                samples = app_module.np.full(
                    (1600, 1), value, dtype=app_module.np.float32
                )
                self.callback(samples, len(samples), None, None)

        class FakeAudioInput:
            InputStream = FakeInputStream

        recorder = app_module.AudioRecorderThread()
        recorder.audio_chunk_ready.connect(
            lambda chunk: emitted_chunks.append(chunk.copy()),
            app_module.Qt.ConnectionType.DirectConnection,
        )
        recorder.pause_ready.connect(
            pause_ready.set,
            app_module.Qt.ConnectionType.DirectConnection,
        )

        with patch.object(app_module, "sd", FakeAudioInput):
            recorder.start()
            try:
                self.assertTrue(stream_ready.wait(5), "Audio input stream did not start")
                stream = FakeInputStream.current
                self.assertIsNotNone(stream)

                first_chunk_ready = app_module.threading.Event()
                recorder.audio_chunk_ready.connect(
                    lambda _chunk: first_chunk_ready.set(),
                    app_module.Qt.ConnectionType.DirectConnection,
                )
                stream.feed(0.1)
                self.assertTrue(first_chunk_ready.wait(5))

                recorder.set_paused(True)
                self.assertTrue(pause_ready.wait(5), "Audio buffer was not flushed on pause")
                stream.feed(0.9)
                app_module.time.sleep(0.2)
                self.assertEqual(len(emitted_chunks), 1)

                recorder.set_paused(False)
                second_chunk_ready = app_module.threading.Event()
                recorder.audio_chunk_ready.connect(
                    lambda _chunk: second_chunk_ready.set(),
                    app_module.Qt.ConnectionType.DirectConnection,
                )
                stream.feed(0.3)
                self.assertTrue(second_chunk_ready.wait(5))
                self.assertEqual(len(emitted_chunks), 2)
                self.assertAlmostEqual(float(emitted_chunks[0].mean()), 0.1, places=5)
                self.assertAlmostEqual(float(emitted_chunks[1].mean()), 0.3, places=5)
            finally:
                recorder.stop()

    def test_agenda_template_is_available_and_protocol_receives_context(self):
        dialog = app_module.MeetingAgendaDialog(
            "1. Планирование",
            {"Еженедельная встреча": "1. Статус задач\n2. Риски"},
        )
        dialog.template_combo.setCurrentIndex(1)
        dialog.insert_template()
        self.assertIn("Статус задач", dialog.editor.toPlainText())
        dialog.save_agenda()
        self.assertEqual(dialog.result_agenda, dialog.editor.toPlainText().strip())
        self.assertIn("Еженедельная встреча", dialog.result_library)

        worker = app_module.LLMProtocolThread()
        worker._is_ready = True
        worker.transcript_text = "[10:00:00] Спикер 1: Обсудили статус задач."
        worker.agenda_text = "1. Статус задач — докладчик: Иван"
        worker._chat_completion = Mock(return_value="Принято решение.")
        worker.analyze_mind_map = Mock()
        worker.generate_now()

        messages = worker._chat_completion.call_args.kwargs["messages"]
        self.assertIn("Статус задач", messages[1]["content"])
        self.assertIn("Иван", messages[1]["content"])
        self.assertIn("не назначай имя", messages[0]["content"])

    def test_agenda_library_is_saved_and_reloaded_as_a_portable_json_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "meeting_agenda.json")
            current = "1. Бюджет — докладчик: Иван"
            library = {"Еженедельная встреча": "1. Задачи\n2. Риски"}

            app_module.save_meeting_agenda(current, library, path)

            self.assertEqual(
                app_module.load_meeting_agenda(path),
                (current, library),
            )


if __name__ == "__main__":
    unittest.main()
