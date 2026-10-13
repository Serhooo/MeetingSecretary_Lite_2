# ИИ-Секретарь совещаний Lite 2

Отдельная portable-копия Lite для Windows: Whisper и GigaAM-v3 E2E RNNT, локальная Gemma GGUF, захват микрофона/системного звука, быстрый режим без диаризации и контекст повестки для протокола.

## Запуск

1. Перенесите папку `MeetingSecretary_Lite_2` целиком на целевой Windows-компьютер.
2. Запустите `Запустить_секретаря.bat`.
3. Выберите ASR и нажмите `⬆ ASR`, чтобы загрузить её и начать запись или импортировать аудиофайл. По умолчанию рекомендуется Whisper small; GigaAM-v3 доступна как альтернатива.
4. Для проверки стенограммы, создания протокола или Mind Map загрузите `⬆ LLM`. Если запустить одно из этих действий без LLM в памяти, приложение предложит её загрузить автоматически.
5. По завершении работы нажмите `⏏ ASR` и/или `⏏ LLM`, чтобы освободить память.

Для автономной работы переносите папки `runtime`, `models` и `whisper-cache` вместе с приложением. В `whisper-cache` включены веса Whisper small и кэш GigaAM-v3 ONNX. Другие Whisper-модели загружаются в этот кэш при первом использовании, поэтому для их первоначальной загрузки нужен интернет. Выбранная локальная Gemma GGUF находится в `models`.

Если приложение сразу закрывается, окно запуска теперь остаётся открытым. Текст запуска также сохраняется в `logs/startup.log`; для диагностики отправьте этот файл и `logs/application.log`.

## Комплектация

- `meeting_secretary_app.py` — приложение;
- `runtime` — portable Python и библиотеки;
- `models/Gemma-4-E2B-Uncensored-HauhauCS-Aggressive-Q6_K_P.gguf` — локальная LLM;
- `models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx` — модель эмбеддингов для диаризации;
- `whisper-cache/models--Systran--faster-whisper-small` — локальные веса Whisper small;
- `whisper-cache/hub/models--istupakov--gigaam-v3-onnx` — локальные файлы ASR GigaAM-v3;
- `speaker_profiles.json` — создаётся при сохранении профилей голосов;
- `requirements-audio.txt` — зависимость для WASAPI loopback;
- BAT-файлы запуска и проверки среды.

## Структура папок и где скачать компоненты

В git входит только код и документация. Папки `runtime`, `models` и `whisper-cache` не загружаются на GitHub из-за размера (≈6 ГБ) — соберите их по структуре ниже:

```text
MeetingSecretary_Lite_2/
├── meeting_secretary_app.py            # приложение — из репозитория
├── Запустить_секретаря.bat             # запуск — из репозитория
├── Проверить_среду.bat                 # проверка среды — из репозитория
├── requirements-audio.txt              # PyAudioWPatch — из репозитория
├── meeting_agenda.json                 # повестка — из репозитория
├── README.md, ИНСТРУКЦИЯ_*.md          # документация — из репозитория
├── runtime/                            # portable Python 3.13.15 + библиотеки (~1.2 ГБ)
│   ├── python.exe, python313.dll, ...
│   └── Lib/site-packages/...
├── models/                             # скачать по ссылкам ниже (~3.7 ГБ)
│   ├── Gemma-4-E2B-Uncensored-HauhauCS-Aggressive-Q6_K_P.gguf
│   └── 3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx
├── whisper-cache/                      # кэш моделей распознавания (~1.3 ГБ)
│   ├── models--Systran--faster-whisper-small/     # Whisper small (463 МБ)
│   └── hub/models--istupakov--gigaam-v3-onnx/     # GigaAM-v3 E2E RNNT (~850 МБ)
├── logs/                               # создаётся автоматически
└── recovery/                           # создаётся автоматически (сессии)
```

### Ссылки на скачивание

| Файл | Куда положить | Скачать |
|---|---|---|
| Gemma-4-E2B-Uncensored-HauhauCS-Aggressive-Q6_K_P.gguf (3,6 ГБ) | `models/` | [HuggingFace: HauhauCS/Gemma-4-E2B-Uncensored-HauhauCS-Aggressive](https://huggingface.co/HauhauCS/Gemma-4-E2B-Uncensored-HauhauCS-Aggressive/resolve/main/Gemma-4-E2B-Uncensored-HauhauCS-Aggressive-Q6_K_P.gguf) |
| 3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx (38 МБ) | `models/` | [HuggingFace: csukuangfj/speaker-embedding-models](https://huggingface.co/csukuangfj/speaker-embedding-models/resolve/main/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx) |
| Whisper small (463 МБ) | `whisper-cache/models--Systran--faster-whisper-small/` | [HuggingFace: Systran/faster-whisper-small](https://huggingface.co/Systran/faster-whisper-small) |
| GigaAM-v3 E2E RNNT (~850 МБ) | `whisper-cache/hub/models--istupakov--gigaam-v3-onnx/` | [HuggingFace: istupakov/gigaam-v3-onnx](https://huggingface.co/istupakov/gigaam-v3-onnx) |
| Python 3.13.15 embeddable (64-bit) | основа `runtime/` | [python-3.13.15-embed-amd64.zip](https://www.python.org/ftp/python/3.13.15/python-3.13.15-embed-amd64.zip) |

Проще всего скачать Whisper small и GigaAM автоматически: положите пустые папки, подключите интернет и запустите распознавание один раз — `faster-whisper` и `onnx-asr` сами заполнят `whisper-cache` в нужной структуре. Другие размеры Whisper (`tiny`, `medium`, `large-v3-turbo`) лежат в том же формате: [Systran/faster-whisper-tiny](https://huggingface.co/Systran/faster-whisper-tiny), [Systran/faster-whisper-medium](https://huggingface.co/Systran/faster-whisper-medium), [Systran/faster-whisper-large-v3-turbo](https://huggingface.co/Systran/faster-whisper-large-v3-turbo).

### Сборка runtime

1. Распакуйте [python-3.13.15-embed-amd64.zip](https://www.python.org/ftp/python/3.13.15/python-3.13.15-embed-amd64.zip) в папку `runtime/`.
2. В файле `runtime/python313._pth` раскомментируйте строку `import site`.
3. Установите pip: `runtime\python.exe https://bootstrap.pypa.io/get-pip.py` (предварительно скачайте [get-pip.py](https://bootstrap.pypa.io/get-pip.py)).
4. Установите зависимости:
   `runtime\python.exe -m pip install PySide6 numpy matplotlib sounddevice faster-whisper llama-cpp-python python-docx av sherpa-onnx httpx "onnx-asr[cpu,hub]" PyAudioWPatch`
5. Проверьте результат `Проверить_среду.bat`.

Готовый `runtime` можно просто скопировать с другой рабочей установки этого же приложения.

Модели загружаются независимо. Для обычного распознавания LLM не нужна; для проверки текста, протокола и смысловой карты ASR может быть выгружена. При первом использовании Gemma загрузка GGUF занимает несколько гигабайт RAM.

## Диаризация и быстрый вывод

В поле `Спикеры` доступны три режима:

- `По профилям` — сопоставляет голос с сохранёнными профилями и разделяет остальные голоса;
- `Автоматическая` — разделяет голоса без использования сохранённых профилей;
- `Отключена · быстрый текст` — не запускает анализ голосовых эмбеддингов. ASR обрабатывает короткие аудиоблоки и дописывает результат в один непрерывный текст без имён спикеров и временных меток на каждом блоке. Кнопка `⏸ Пауза` останавливает захват и распознаёт остаток уже принятого аудио; `▶ Продолжить` вставляет одну метку времени и продолжает тот же текст после неё.

В быстром режиме спикеры не анализируются и не подписываются. Повестка не используется для автоматического присвоения имён. При создании протокола LLM получает повестку и может сгруппировать обсуждение по её темам, но не должна приписывать имя только по теме. ASR вызывается пакетно на каждом готовом блоке, а не выдаёт потоковые токены. По умолчанию быстрый режим накапливает не менее 3 секунд аудио перед распознаванием, чтобы реже разрывать предложения; увеличение `с фрагмент` дополнительно снижает частоту разделения ценой задержки. Фактическая задержка зависит также от выбранной модели и скорости компьютера.

## Повестка и частые темы

Нажмите `🗂 Повестка`, чтобы загрузить TXT, Markdown или Word DOCX, либо ввести темы и докладчиков вручную. Текущая повестка и библиотека сохраняются в `meeting_agenda.json` рядом с программой и автоматически передаются LLM при проверке стенограммы, генерации протокола и Mind Map. Файл можно переносить вместе с приложением и резервировать.

Кнопка `В библиотеку` сохраняет типовые повестки и повторяющиеся темы локально в настройках Lite 2; их можно затем вставлять и редактировать. Это библиотека справочных шаблонов, а не обучающая база: она не дообучает модель. Повестка помогает группировать обсуждение, но не подтверждает, что вопрос обсуждался или решение было принято.

## Аудио

В `Источник звука` доступны только два варианта: микрофон Windows по умолчанию и системный звук Windows с устройства вывода по умолчанию. Второй вариант использует WASAPI loopback и захватывает весь звук, который Windows направляет на выходное устройство, а не отдельное приложение. Если в списке нет системного звука, проверьте наличие PyAudioWPatch и перезапустите `Проверить_среду.bat`.

Сигнал приводится к mono/16 kHz. Чувствительность настраивает программный порог обнаружения речи, а не аппаратное усиление микрофона.

## Ограничения Lite

- Whisper small выбран по умолчанию; доступны Whisper tiny, medium, large-v3-turbo и GigaAM-v3 E2E RNNT. Для использования альтернативных Whisper-весов без интернета заранее загрузите их в `whisper-cache`.
- При отключённой диаризации спикеры не определяются по голосу.
- Редактор и контекстный словарь терминов удалены. GigaAM в текущей интеграции не принимает лексические подсказки из пользовательского словаря.
- API-режим LLM и другие GGUF можно выбрать в настройках приложения, если модель добавлена в `models` или указана через обзор.
- Не переименовывайте `runtime`, `models` и `whisper-cache`.

## Проверка комплекта

Запустите `Проверить_среду.bat`. Он проверяет portable Python и основные зависимости. Для системного loopback нужен пакет из `requirements-audio.txt`.
