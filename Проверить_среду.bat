@echo off
setlocal
cd /d "%~dp0"
set "PATH=%~dp0runtime;%PATH%"
set "HF_HOME=%~dp0whisper-cache"
"%~dp0runtime\python.exe" -c "import numpy, matplotlib, PySide6, sounddevice, faster_whisper, llama_cpp, docx, av, sherpa_onnx, httpx, onnx_asr, importlib.util; from pathlib import Path; speaker_model=Path('models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx'); llm_model=Path('models/Gemma-4-E2B-Uncensored-HauhauCS-Aggressive-Q6_K_P.gguf'); whisper_model=Path('whisper-cache/models--Systran--faster-whisper-small/snapshots'); assert speaker_model.is_file(), 'Не найдена ONNX-модель спикеров'; assert llm_model.is_file(), 'Не найдена модель Gemma GGUF'; assert whisper_model.is_dir() and any(whisper_model.iterdir()), 'Не найден локальный кэш Whisper small'; print('Lite 2 portable-среда готова: Whisper, GigaAM, Gemma, диаризация и быстрый режим доступны'); print('WASAPI loopback:', 'установлен' if importlib.util.find_spec('pyaudiowpatch') else 'не установлен (системный звук недоступен)')"
pause
