import sys
import os
import glob
import time
import queue
import threading
import tempfile
import json
import logging
import traceback
import html
import ctypes
import ctypes.wintypes
import platform
import ipaddress
import wave
import zipfile
from urllib.parse import urlparse
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import Qt, QEvent, QThread, Signal, Slot, QTimer
from PySide6.QtCore import QSettings
from PySide6.QtGui import (
    QTextDocument, QPageSize, QPdfWriter, QBrush, QPen, QFont
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTextEdit, QLabel, QSplitter, QComboBox, QFileDialog,
    QMessageBox, QStatusBar, QGroupBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QInputDialog, QDialog, QDialogButtonBox, QProgressBar, QTabWidget,
    QGraphicsView, QGraphicsScene, QGraphicsRectItem, QGraphicsLineItem, QGridLayout,
    QGraphicsItem, QGraphicsTextItem, QSpinBox, QSlider, QLineEdit, QSizePolicy, QCheckBox,
    QToolTip
)

import numpy as np

# Matplotlib headless config for chart generation
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# Audio & ASR imports
try:
    import sounddevice as sd
except ImportError:
    sd = None
try:
    import pyaudiowpatch as pyaudio
except ImportError:
    pyaudio = None

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

try:
    import sherpa_onnx
except ImportError:
    sherpa_onnx = None

try:
    import av
except ImportError:
    av = None

try:
    import httpx
except ImportError:
    httpx = None

# Local LLM import via llama-cpp-python
try:
    from llama_cpp import Llama
except ImportError:
    Llama = None

try:
    from docx import Document
except ImportError:
    Document = None

SAMPLE_RATE = 16000
PROTOCOL_FORMAT_CURRENT = "current"
PROTOCOL_FORMAT_SAMPLE = "sample"
DIARIZATION_PROFILES = "profiles"
DIARIZATION_AUTOMATIC = "automatic"
DIARIZATION_DISABLED = "disabled"
LOCAL_NETWORK_IPV4 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
LOCAL_NETWORK_IPV6 = ipaddress.ip_network("fc00::/7")
ASR_WHISPER_MODELS = {
    "tiny": "tiny",
    "small": "small",
    "medium": "medium",
    "large-v3-turbo": "large-v3-turbo",
}
ASR_ONNX_MODELS = {
    "gigaam-v3-e2e-rnnt": "gigaam-v3-e2e-rnnt",
}


def is_local_network_host(hostname):
    if hostname is None:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    if address.version == 4:
        return any(address in network for network in LOCAL_NETWORK_IPV4)
    return address in LOCAL_NETWORK_IPV6


def openai_chat_completions_url(api_base_url):
    base_url = api_base_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    if base_url.endswith("/v1"):
        return f"{base_url}/chat/completions"
    return f"{base_url}/v1/chat/completions"


APP_DIR = os.path.dirname(
    sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
)
SPEAKER_PROFILES_PATH = os.path.join(APP_DIR, "speaker_profiles.json")
MEETING_AGENDA_PATH = os.path.join(APP_DIR, "meeting_agenda.json")
API_CREDENTIAL_TARGET = "MeetingSecretary.OpenAICompatibleAPI"
LOG_DIR = os.path.join(APP_DIR, "logs")
RECOVERY_DIR = os.path.join(APP_DIR, "recovery")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(RECOVERY_DIR, exist_ok=True)

logger = logging.getLogger("meeting_secretary")
logger.setLevel(logging.INFO)
if not logger.handlers:
    file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "application.log"),
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(file_handler)
    logger.addHandler(logging.StreamHandler(sys.stderr))


def handle_uncaught_exception(exc_type, exc_value, exc_traceback):
    logger.critical("Необработанная ошибка", exc_info=(exc_type, exc_value, exc_traceback))


sys.excepthook = handle_uncaught_exception


def find_gguf_models():
    """Search for .gguf model files in current folder and ./models subfolder."""
    search_paths = [
        os.path.join(APP_DIR, "*.gguf"),
        os.path.join(APP_DIR, "models", "*.gguf"),
    ]
    found_models = []
    for pattern in search_paths:
        found_models.extend(glob.glob(pattern))
    return sorted(list(set(found_models)))


class _WindowsCredential(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.wintypes.DWORD),
        ("Type", ctypes.wintypes.DWORD),
        ("TargetName", ctypes.wintypes.LPWSTR),
        ("Comment", ctypes.wintypes.LPWSTR),
        ("LastWritten", ctypes.wintypes.FILETIME),
        ("CredentialBlobSize", ctypes.wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.wintypes.DWORD),
        ("AttributeCount", ctypes.wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.wintypes.LPWSTR),
        ("UserName", ctypes.wintypes.LPWSTR),
    ]


def save_api_credential(secret):
    if os.name != "nt":
        raise OSError("Хранилище учетных данных Windows доступно только в Windows")
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    write_credential = advapi32.CredWriteW
    write_credential.argtypes = [ctypes.POINTER(_WindowsCredential), ctypes.wintypes.DWORD]
    write_credential.restype = ctypes.wintypes.BOOL
    secret_bytes = secret.encode("utf-8")
    secret_buffer = ctypes.create_string_buffer(secret_bytes)
    credential = _WindowsCredential(
        0,
        1,
        API_CREDENTIAL_TARGET,
        None,
        ctypes.wintypes.FILETIME(),
        len(secret_bytes),
        ctypes.cast(secret_buffer, ctypes.POINTER(ctypes.c_ubyte)),
        2,
        0,
        None,
        None,
        "Meeting Secretary",
    )
    if not write_credential(ctypes.byref(credential), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def load_api_credential():
    if os.name != "nt":
        return ""
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    read_credential = advapi32.CredReadW
    read_credential.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_WindowsCredential)),
    ]
    read_credential.restype = ctypes.wintypes.BOOL
    credential_ptr = ctypes.POINTER(_WindowsCredential)()
    if not read_credential(API_CREDENTIAL_TARGET, 1, 0, ctypes.byref(credential_ptr)):
        error = ctypes.get_last_error()
        if error == 1168:
            return ""
        raise ctypes.WinError(error)
    try:
        credential = credential_ptr.contents
        secret = ctypes.string_at(
            credential.CredentialBlob, credential.CredentialBlobSize
        )
        return secret.decode("utf-8")
    finally:
        advapi32.CredFree.argtypes = [ctypes.c_void_p]
        advapi32.CredFree(credential_ptr)


def normalize_speaker_profiles(profiles):
    normalized = {}
    dimensions = set()
    normalized_names = set()
    if not isinstance(profiles, dict):
        raise ValueError("Профили спикеров должны быть объектом JSON.")
    for raw_name, raw_embedding in profiles.items():
        name = str(raw_name).strip()
        if not name or not isinstance(raw_embedding, list) or not raw_embedding:
            raise ValueError("Каждый профиль должен содержать имя и непустой вектор эмбеддинга.")
        if name.casefold() in normalized_names:
            raise ValueError(f"Имя профиля «{name}» повторяется.")
        normalized_names.add(name.casefold())
        try:
            embedding = np.asarray(raw_embedding, dtype=np.float32)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Вектор профиля «{name}» содержит некорректные значения.") from exc
        if embedding.ndim != 1 or not np.isfinite(embedding).all():
            raise ValueError(f"Вектор профиля «{name}» имеет неверный формат.")
        norm = float(np.linalg.norm(embedding))
        if not norm:
            raise ValueError(f"Вектор профиля «{name}» не может быть нулевым.")
        dimensions.add(len(embedding))
        normalized[name] = (embedding / norm).tolist()
    if len(dimensions) > 1:
        raise ValueError("Размерность эмбеддингов должна совпадать у всех профилей.")
    return normalized


def load_speaker_profiles(path=SPEAKER_PROFILES_PATH):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as file:
            payload = json.load(file)
        if not isinstance(payload, dict) or payload.get("format_version") != 1:
            raise ValueError("Неподдерживаемый формат файла профилей спикеров.")
        return normalize_speaker_profiles(payload.get("profiles"))
    except (OSError, json.JSONDecodeError, ValueError):
        logger.exception("Не удалось загрузить профили спикеров из %s", path)
        raise


def save_speaker_profiles(profiles, path=SPEAKER_PROFILES_PATH):
    validated = normalize_speaker_profiles(profiles)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(
                {"format_version": 1, "profiles": validated},
                file,
                ensure_ascii=False,
                indent=2,
            )
        os.replace(temporary, path)
    except OSError:
        logger.exception("Не удалось сохранить профили спикеров в %s", path)
        raise


def load_meeting_agenda(path=MEETING_AGENDA_PATH):
    if not os.path.isfile(path):
        return "", {}
    try:
        with open(path, "r", encoding="utf-8") as file:
            payload = json.load(file)
        if not isinstance(payload, dict) or payload.get("format_version") != 1:
            raise ValueError("Неподдерживаемый формат файла повестки.")
        current = payload.get("current", "")
        library = payload.get("library", {})
        if not isinstance(current, str):
            raise ValueError("Текст текущей повестки должен быть строкой.")
        if not isinstance(library, dict) or not all(
            isinstance(title, str)
            and title.strip()
            and isinstance(content, str)
            for title, content in library.items()
        ):
            raise ValueError("Библиотека повесток имеет неверный формат.")
        return current, library
    except (OSError, json.JSONDecodeError, ValueError):
        logger.exception("Не удалось загрузить повестку из %s", path)
        raise


def save_meeting_agenda(current, library, path=MEETING_AGENDA_PATH):
    if not isinstance(current, str):
        raise ValueError("Текст текущей повестки должен быть строкой.")
    if not isinstance(library, dict) or not all(
        isinstance(title, str)
        and title.strip()
        and isinstance(content, str)
        for title, content in library.items()
    ):
        raise ValueError("Библиотека повесток имеет неверный формат.")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(
                {
                    "format_version": 1,
                    "current": current,
                    "library": library,
                },
                file,
                ensure_ascii=False,
                indent=2,
            )
        os.replace(temporary, path)
    except OSError:
        logger.exception("Не удалось сохранить повестку в %s", path)
        raise


def get_pc_configuration():
    processor = platform.processor().strip() or os.environ.get(
        "PROCESSOR_IDENTIFIER", "CPU не определён"
    ).strip()
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        ) as key:
            processor = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    except (ImportError, OSError):
        pass

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.wintypes.DWORD),
            ("dwMemoryLoad", ctypes.wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    memory = MemoryStatus()
    memory.dwLength = ctypes.sizeof(MemoryStatus)
    total_ram_gb = None
    if hasattr(ctypes, "windll") and ctypes.windll.kernel32.GlobalMemoryStatusEx(
        ctypes.byref(memory)
    ):
        total_ram_gb = round(memory.ullTotalPhys / (1024 ** 3))
    parts = [processor]
    if total_ram_gb:
        parts.append(f"{total_ram_gb} GB RAM")
    parts.append(f"{platform.system()} {platform.release()}")
    return " | ".join(parts)


def build_protocol_system_prompt(protocol_format, meeting_datetime):
    if protocol_format == PROTOCOL_FORMAT_SAMPLE:
        return (
            "Ты — секретарь официального совещания. Составь по стенограмме протокол на русском "
            "языке в строгой деловой форме, близкой к образцу официального ведомственного "
            "протокола. Верни обычный текст без Markdown-разметки и пояснений.\n\n"
            "Структура документа:\n"
            "1. В шапке укажи наименование организации и территорию только если они явно "
            "установлены стенограммой; иначе оставь нейтральный заполнитель "
            "[Наименование организации].\n"
            "2. Отдельной строкой заголовок «П Р О Т О К О Л».\n"
            "3. Ниже — краткое наименование совещания, установленное по обсуждению; "
            "не придумывай тему, которой нет в стенограмме.\n"
            f"4. Строка «{meeting_datetime}    № ______».\n"
            "5. Блоки «Председательствовал:» и «Присутствовали». Включай только участников "
            "и должности, прямо названные в стенограмме. Если известно только обозначение "
            "«Спикер N», используй его; неизвестные должности или председательствующего "
            "не выдумывай и обозначай «не указан в стенограмме».\n"
            "6. Сгруппируй обсуждение по вопросам повестки. Каждый вопрос оформляй отдельным "
            "пунктом римскими цифрами (I., II., III.); после заголовка вопроса при наличии "
            "укажи докладчика/участников в скобках.\n"
            "7. Внутри каждого вопроса добавь строку «Решения:» и изложи решения и поручения "
            "нумерованными пунктами. Срок каждого поручения указывай отдельной строкой "
            "«Срок: ...», если он прозвучал. Сохраняй ответственного, действие и ограничения "
            "из стенограммы; не назначай исполнителя и не выдумывай срок. Если ответственный "
            "не назван, прямо укажи «Ответственный не указан».\n"
            "8. В конце добавь блок подписи председательствующего только если он достоверно "
            "известен; иначе оставь строку «Председательствующий: не указан в стенограмме».\n\n"
            "Сохраняй принятые решения, поручения, сроки, открытые вопросы, возражения и "
            "согласия. Отличай принятое решение от предложения и короткого подтверждения. "
            "Не добавляй сведения от себя. Если решения по вопросу не сформулированы, "
            "напиши это явно, а не создавай поручение.\n\n"
        )

    return (
        "Ты — ИИ-секретарь совещания. Проанализируй стенограмму с именами спикеров "
        "и сформируй структурированный протокол на русском языке в формате Markdown.\n\n"
        "Сохраняй ход обсуждения: отмечай явные возражения, перебивания, ответы и согласия "
        "с указанием участников и времени, если они есть в стенограмме. Короткое «да» или "
        "«согласен» не превращай в отдельное решение без контекста. Различай предложения, "
        "подтвержденные решения и нерешенные вопросы; не достраивай пропущенные реплики.\n\n"
        f"В самое начало протокола обязательно поставь заголовок '# Протокол совещания' "
        f"и строку '**Дата и время:** {meeting_datetime}'. Не придумывай другие даты.\n\n"
        "Разделы протокола:\n"
        "1. **Принятые решения**\n"
        "2. **Задачи и поручения** (с указанием конкретных исполнителей)\n"
        "3. **Открытые вопросы и риски**"
    )


class SimpleSpeakerEmbedder:
    """Speaker diarization using ONNX embeddings, with acoustic fallback."""
    def __init__(
        self,
        energy_threshold=0.01,
        speaker_profiles=None,
        use_profiles=True,
    ):
        self.speaker_centroids = {}
        self.embedding_centroids = {}
        self.confirmed_profiles = set()
        self.use_profiles = use_profiles
        self._lock = threading.RLock()
        profiles = normalize_speaker_profiles(speaker_profiles or {}) if use_profiles else {}
        for name, embedding in profiles.items():
            self.embedding_centroids[name] = np.asarray(embedding, dtype=np.float32)
            self.confirmed_profiles.add(name)
        self.speaker_counter = 1
        self.last_speaker = None
        self.last_feature = None
        self.last_activity = 0.0
        self.last_switch_time = 0.0
        self.extractor = None
        self.pending_embedding = None
        self.pending_embedding_count = 0
        self.last_embedding_candidate_pending = False
        self.energy_threshold = energy_threshold

    def initialize(self):
        model_path = os.path.join(
            APP_DIR,
            "models",
            "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx",
        )
        if sherpa_onnx is None:
            logger.warning("sherpa-onnx is not installed; using basic speaker heuristic")
            return False
        if not os.path.isfile(model_path):
            logger.warning("Speaker embedding model is missing: %s", model_path)
            return False
        try:
            config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=model_path,
                num_threads=2,
                provider="cpu",
            )
            if not config.validate():
                raise ValueError("Invalid speaker embedding model configuration")
            self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
            logger.info("Speaker embedding model loaded, dimension=%s", self.extractor.dim)
            return True
        except Exception:
            logger.exception("Failed to load speaker embedding model; using basic heuristic")
            self.extractor = None
            return False

    def reset_session(self):
        with self._lock:
            self.speaker_centroids.clear()
            if self.use_profiles:
                self.embedding_centroids = {
                    name: embedding
                    for name, embedding in self.embedding_centroids.items()
                    if name in self.confirmed_profiles
                }
            else:
                self.embedding_centroids.clear()
                self.confirmed_profiles.clear()
            self.speaker_counter = 1
            self.last_speaker = None
            self.last_feature = None
            self.last_activity = 0.0
            self.last_switch_time = 0.0
            self.pending_embedding = None
            self.pending_embedding_count = 0
            self.last_embedding_candidate_pending = False

    def export_profiles(self):
        with self._lock:
            return {
                name: embedding.tolist()
                for name, embedding in self.embedding_centroids.items()
                if name in self.confirmed_profiles
            }

    def rename_profile(self, current_name, new_name):
        with self._lock:
            new_name = new_name.strip()
            if not new_name:
                raise ValueError("Введите имя профиля.")
            if current_name not in self.embedding_centroids:
                return False
            if new_name != current_name and new_name in self.embedding_centroids:
                raise ValueError(f"Профиль «{new_name}» уже существует.")
            self.embedding_centroids[new_name] = self.embedding_centroids.pop(current_name)
            if current_name in self.confirmed_profiles:
                self.confirmed_profiles.remove(current_name)
            self.confirmed_profiles.add(new_name)
            if self.last_speaker == current_name:
                self.last_speaker = new_name
            return True

    def replace_profiles(self, profiles):
        with self._lock:
            normalized = normalize_speaker_profiles(profiles)
            if not self.use_profiles:
                return
            existing_names = set(self.confirmed_profiles)
            centroids = {
                name: centroid
                for name, centroid in self.embedding_centroids.items()
                if name not in existing_names
            }
            centroids.update({
                name: np.asarray(embedding, dtype=np.float32)
                for name, embedding in normalized.items()
            })
            self.embedding_centroids = centroids
            self.confirmed_profiles = set(normalized)

    def get_speaker(self, audio_chunk):
        if self.extractor is not None:
            speaker = self._get_embedding_speaker(audio_chunk)
            if speaker is not None:
                return speaker
        return self._get_fallback_speaker(audio_chunk)

    def _get_embedding_speaker(self, audio_chunk):
        self.last_embedding_candidate_pending = False
        if len(audio_chunk) < SAMPLE_RATE:
            return self.last_speaker

        rms = float(np.sqrt(np.mean(np.square(audio_chunk))))
        if rms < self.energy_threshold:
            return self.last_speaker

        try:
            stream = self.extractor.create_stream()
            stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=audio_chunk)
            stream.input_finished()
            if not self.extractor.is_ready(stream):
                return self.last_speaker
            embedding = np.asarray(self.extractor.compute(stream), dtype=np.float32)
            norm = float(np.linalg.norm(embedding))
            if not norm:
                return self.last_speaker
            embedding /= norm
        except Exception:
            logger.exception("Speaker embedding inference failed; using acoustic fallback")
            self.extractor = None
            return None

        best_speaker = None
        best_similarity = -1.0
        for name, centroid in self.embedding_centroids.items():
            if centroid.shape != embedding.shape:
                continue
            similarity = float(np.dot(embedding, centroid))
            if similarity > best_similarity:
                best_speaker = name
                best_similarity = similarity

        if best_speaker is not None and best_similarity >= 0.55:
            centroid = self.embedding_centroids[best_speaker]
            updated = 0.8 * centroid + 0.2 * embedding
            self.embedding_centroids[best_speaker] = updated / np.linalg.norm(updated)
            self.pending_embedding = None
            self.pending_embedding_count = 0
            self.last_speaker = best_speaker
            logger.info("Speaker matched by embedding: %s (similarity=%.3f)", best_speaker, best_similarity)
            return best_speaker

        if best_speaker is not None and best_similarity >= 0.35:
            self.pending_embedding = None
            self.pending_embedding_count = 0
            self.last_speaker = best_speaker
            return best_speaker

        if not self.embedding_centroids:
            name = self._next_speaker_name()
            self.embedding_centroids[name] = embedding
            self.last_speaker = name
            return name

        candidate_similarity = (
            float(np.dot(embedding, self.pending_embedding))
            if self.pending_embedding is not None
            else -1.0
        )
        if candidate_similarity >= 0.75:
            pending = 0.5 * self.pending_embedding + 0.5 * embedding
            self.pending_embedding = pending / np.linalg.norm(pending)
            self.pending_embedding_count += 1
        else:
            self.pending_embedding = embedding
            self.pending_embedding_count = 1

        if self.pending_embedding_count < 2 or len(self.embedding_centroids) >= 8:
            self.last_embedding_candidate_pending = True
            return best_speaker or self.last_speaker or "Спикер 1"

        name = self._next_speaker_name()
        self.embedding_centroids[name] = self.pending_embedding
        self.pending_embedding = None
        self.pending_embedding_count = 0
        self.last_speaker = name
        logger.info("New speaker by embedding: %s (similarity=%.3f)", name, best_similarity)
        return name

    def _next_speaker_name(self):
        while f"Спикер {self.speaker_counter}" in self.embedding_centroids:
            self.speaker_counter += 1
        name = f"Спикер {self.speaker_counter}"
        self.speaker_counter += 1
        return name

    def get_speaker_timeline(self, audio_chunk, window_seconds=3.0, step_seconds=2.0):
        with self._lock:
            return self._get_speaker_timeline(audio_chunk, window_seconds, step_seconds)

    def _get_speaker_timeline(self, audio_chunk, window_seconds, step_seconds):
        duration = len(audio_chunk) / SAMPLE_RATE
        window_samples = int(window_seconds * SAMPLE_RATE)
        if len(audio_chunk) <= window_samples:
            return [(0.0, duration, self.get_speaker(audio_chunk))]

        step_samples = int(step_seconds * SAMPLE_RATE)
        last_start = len(audio_chunk) - window_samples
        starts = list(range(0, last_start + 1, step_samples))
        if starts[-1] != last_start:
            starts.append(last_start)

        timeline = []
        pending_indices = []
        for start in starts:
            end = start + window_samples
            window = audio_chunk[start:end]
            rms = float(np.sqrt(np.mean(np.square(window))))
            if rms < self.energy_threshold:
                continue
            speaker_count = len(self.embedding_centroids)
            speaker = self.get_speaker(window)
            if self.last_embedding_candidate_pending:
                pending_indices.append(len(timeline))
                timeline.append((start / SAMPLE_RATE, end / SAMPLE_RATE, None))
                continue
            if len(self.embedding_centroids) > speaker_count and speaker is not None:
                for index in pending_indices:
                    window_start, window_end, _ = timeline[index]
                    timeline[index] = (window_start, window_end, speaker)
            elif pending_indices:
                fallback = next(
                    (entry[2] for entry in reversed(timeline) if entry[2] is not None),
                    speaker,
                )
                for index in pending_indices:
                    window_start, window_end, _ = timeline[index]
                    timeline[index] = (window_start, window_end, fallback)
            pending_indices.clear()
            timeline.append((start / SAMPLE_RATE, end / SAMPLE_RATE, speaker))
        if pending_indices:
            fallback = next(
                (entry[2] for entry in reversed(timeline) if entry[2] is not None),
                self.last_speaker or "Спикер 1",
            )
            for index in pending_indices:
                window_start, window_end, _ = timeline[index]
                timeline[index] = (window_start, window_end, fallback)
        if not timeline:
            timeline.append((0.0, duration, self.get_speaker(audio_chunk)))
        return timeline

    @staticmethod
    def speaker_for_interval(start, end, timeline, default_speaker="Спикер 1"):
        overlaps = {}
        for window_start, window_end, speaker in timeline:
            if speaker is None:
                continue
            overlap = max(0.0, min(end, window_end) - max(start, window_start))
            if overlap:
                overlaps[speaker] = overlaps.get(speaker, 0.0) + overlap
        return max(overlaps, key=overlaps.get) if overlaps else default_speaker

    def _get_fallback_speaker(self, audio_chunk):
        if len(audio_chunk) < SAMPLE_RATE * 0.35:
            return self.last_speaker or "Спикер 1"

        rms = float(np.sqrt(np.mean(np.square(audio_chunk))))
        if rms < self.energy_threshold:
            return self.last_speaker or "Спикер 1"

        energy = float(np.mean(np.abs(audio_chunk)))
        std_dev = float(np.std(audio_chunk))
        zero_crossings = float(np.mean(np.diff(np.sign(audio_chunk)) != 0))
        vec = np.array([energy, std_dev, zero_crossings], dtype=np.float64)
        now = time.monotonic()

        if self.last_speaker is None:
            self.last_feature = vec.copy()
            self.last_activity = energy
            self.last_switch_time = now

        if self.last_speaker is not None and self.last_feature is not None:
            last_dist = float(np.linalg.norm(vec - self.last_feature))
            if last_dist < 0.12:
                self.speaker_centroids[self.last_speaker] = 0.8 * self.speaker_centroids.get(self.last_speaker, self.last_feature) + 0.2 * vec
                self.last_feature = 0.8 * self.last_feature + 0.2 * vec
                self.last_activity = energy
                self.last_switch_time = now
                return self.last_speaker

        if not self.speaker_centroids:
            name = f"Спикер {self.speaker_counter}"
            self.speaker_counter += 1
            self.speaker_centroids[name] = vec
            self.last_speaker = name
            self.last_feature = vec.copy()
            self.last_activity = energy
            self.last_switch_time = now
            return name

        best_speaker = None
        min_dist = float('inf')

        for name, centroid in self.speaker_centroids.items():
            dist = np.linalg.norm(vec - centroid)
            if dist < min_dist:
                min_dist = dist
                best_speaker = name

        if best_speaker is not None and min_dist <= 0.22:
            self.speaker_centroids[best_speaker] = 0.8 * self.speaker_centroids[best_speaker] + 0.2 * vec
            self.last_speaker = best_speaker
            self.last_feature = self.speaker_centroids[best_speaker].copy()
            self.last_activity = energy
            self.last_switch_time = now
            return best_speaker

        if self.last_speaker is not None and now - self.last_switch_time < 1.5:
            self.speaker_centroids[self.last_speaker] = 0.9 * self.speaker_centroids.get(self.last_speaker, self.last_feature) + 0.1 * vec
            self.last_feature = self.speaker_centroids[self.last_speaker].copy()
            self.last_activity = energy
            return self.last_speaker

        if len(self.speaker_centroids) < 6:
            name = f"Спикер {self.speaker_counter}"
            self.speaker_counter += 1
            self.speaker_centroids[name] = vec
            self.last_speaker = name
            self.last_feature = vec.copy()
            self.last_activity = energy
            self.last_switch_time = now
            return name

        if best_speaker is not None and min_dist <= 0.32 and (self.last_speaker is None or best_speaker == self.last_speaker or now - self.last_switch_time >= 1.5):
            self.speaker_centroids[best_speaker] = 0.8 * self.speaker_centroids[best_speaker] + 0.2 * vec
            self.last_speaker = best_speaker
            self.last_feature = self.speaker_centroids[best_speaker].copy()
            self.last_activity = energy
            self.last_switch_time = now
            return best_speaker

        self.last_feature = 0.9 * self.last_feature + 0.1 * vec if self.last_feature is not None else vec.copy()
        self.last_activity = energy
        return self.last_speaker or "Спикер 1"


class AudioRecorderThread(QThread):
    audio_chunk_ready = Signal(np.ndarray)
    level_ready = Signal(int)
    error_signal = Signal(str)
    pause_ready = Signal()

    def __init__(
        self,
        sample_rate=SAMPLE_RATE,
        output_path=None,
        audio_source=None,
    ):
        super().__init__()
        self.sample_rate = sample_rate
        self.output_path = output_path
        self.audio_source = dict(audio_source or {
            "id": "default",
            "kind": "input",
            "device": None,
            "channels": 1,
            "sample_rate": sample_rate,
        })
        self.running = False
        self.paused = False
        self._pause_flush_pending = False
        self._audio_buffer = []
        self._audio_buffer_lock = threading.Lock()

    def set_paused(self, paused):
        with self._audio_buffer_lock:
            if self.paused == paused:
                return
            self.paused = paused
            self._pause_flush_pending = paused

    def run(self):
        self.running = True

        def queue_audio(indata):
            samples = np.asarray(indata, dtype=np.float32)
            if samples.ndim == 2:
                samples = samples.mean(axis=1)
            else:
                samples = samples.reshape(-1)
            input_rate = int(self.audio_source["sample_rate"])
            if input_rate != self.sample_rate and samples.size:
                output_count = max(
                    1, int(round(samples.size * self.sample_rate / input_rate))
                )
                source_positions = np.arange(samples.size, dtype=np.float64)
                target_positions = (
                    np.arange(output_count, dtype=np.float64)
                    * input_rate
                    / self.sample_rate
                )
                samples = np.interp(
                    target_positions, source_positions, samples
                ).astype(np.float32)
            if self.running and samples.size:
                with self._audio_buffer_lock:
                    if self.paused:
                        return
                    self._audio_buffer.append(samples.copy())

        def sounddevice_callback(indata, frames, time_info, status):
            if status:
                logger.warning("Audio capture status: %s", status)
            if self.running:
                queue_audio(indata)

        def loopback_callback(in_data, frame_count, time_info, status):
            if status:
                logger.warning("WASAPI loopback capture status: %s", status)
            if self.running and in_data:
                channels = int(self.audio_source["channels"])
                samples = np.frombuffer(in_data, dtype=np.float32)
                if channels > 1:
                    samples = samples.reshape(-1, channels)
                queue_audio(samples)
            return (None, pyaudio.paContinue)

        try:
            output_file = wave.open(self.output_path, "wb") if self.output_path else None
            if output_file:
                output_file.setnchannels(1)
                output_file.setsampwidth(2)
                output_file.setframerate(self.sample_rate)

            def flush_audio_buffer():
                with self._audio_buffer_lock:
                    chunks = self._audio_buffer
                    self._audio_buffer = []
                    pause_ready = self._pause_flush_pending and self.paused
                    if pause_ready:
                        self._pause_flush_pending = False
                if chunks:
                    concatenated = np.concatenate(chunks).astype(np.float32, copy=False)
                    if output_file:
                        pcm = np.rint(np.clip(concatenated, -1.0, 0.999969) * 32768).astype("<i2")
                        output_file.writeframesraw(pcm.tobytes())
                    rms_level = int(min(100, np.sqrt(np.mean(np.square(concatenated))) * 350))
                    self.level_ready.emit(rms_level)
                    self.audio_chunk_ready.emit(concatenated)
                if pause_ready:
                    self.pause_ready.emit()

            try:
                if self.audio_source["kind"] == "loopback":
                    if pyaudio is None:
                        raise RuntimeError(
                            "Для записи системного звука нужен PyAudioWPatch "
                            "в portable runtime."
                        )
                    audio = pyaudio.PyAudio()
                    try:
                        stream = audio.open(
                            format=pyaudio.paFloat32,
                            channels=int(self.audio_source["channels"]),
                            rate=int(self.audio_source["sample_rate"]),
                            input=True,
                            input_device_index=int(self.audio_source["device"]),
                            frames_per_buffer=1024,
                            stream_callback=loopback_callback,
                        )
                        try:
                            while self.running:
                                time.sleep(0.1)
                                flush_audio_buffer()
                            flush_audio_buffer()
                        finally:
                            if stream.is_active():
                                stream.stop_stream()
                            stream.close()
                    finally:
                        audio.terminate()
                else:
                    with sd.InputStream(
                        samplerate=int(self.audio_source["sample_rate"]),
                        device=self.audio_source["device"],
                        channels=int(self.audio_source["channels"]),
                        dtype="float32",
                        callback=sounddevice_callback,
                    ):
                        while self.running:
                            time.sleep(0.1)
                            flush_audio_buffer()
                    flush_audio_buffer()
            finally:
                if output_file:
                    output_file.close()
        except Exception as e:
            logger.exception("Ошибка захвата аудио")
            self.error_signal.emit(str(e))

    def stop(self):
        self.running = False
        self.wait()


def enumerate_audio_sources():
    sources = [{
        "id": "default",
        "label": "Микрофон Windows по умолчанию",
        "kind": "input",
        "device": None,
        "channels": 1,
        "sample_rate": SAMPLE_RATE,
    }]
    if sd is not None:
        try:
            devices = sd.query_devices()
            default_device = sd.default.device[0]
            default_info = (
                devices[int(default_device)]
                if default_device is not None and int(default_device) >= 0
                else None
            )
            if default_info:
                sources[0]["sample_rate"] = int(default_info["default_samplerate"])
        except Exception:
            logger.exception("Не удалось получить список устройств ввода")

    if pyaudio is not None:
        try:
            with pyaudio.PyAudio() as audio:
                loopback_devices = list(audio.get_loopback_device_info_generator())
                default_output_name = ""
                try:
                    default_output_name = str(
                        audio.get_default_output_device_info().get("name", "")
                    ).casefold()
                except (OSError, IOError):
                    logger.info("Не удалось определить устройство вывода Windows по умолчанию")
                device = next(
                    (
                        candidate
                        for candidate in loopback_devices
                        if default_output_name
                        and default_output_name in str(candidate["name"]).casefold()
                    ),
                    loopback_devices[0] if loopback_devices else None,
                )
                if device is not None:
                    channels = min(2, int(device["maxInputChannels"]))
                    sources.append({
                        "id": f'loopback:{int(device["index"])}',
                        "label": "Системный звук Windows (выход по умолчанию)",
                        "kind": "loopback",
                        "device": int(device["index"]),
                        "channels": channels,
                        "sample_rate": int(device["defaultSampleRate"]),
                    })
        except Exception:
            logger.exception("Не удалось получить список WASAPI loopback устройств")
    return sources


class AudioFileThread(QThread):
    audio_chunk_ready = Signal(np.ndarray)
    progress_signal = Signal(int)
    status_signal = Signal(str)
    error_signal = Signal(str)
    finished_signal = Signal()

    def __init__(self, file_path):
        super().__init__()
        self.file_path = file_path
        self.running = False

    def run(self):
        if av is None:
            self.error_signal.emit("Модуль PyAV не установлен")
            return

        self.running = True
        try:
            with av.open(self.file_path) as container:
                audio_stream = next(iter(container.streams.audio), None)
                if audio_stream is None:
                    raise RuntimeError("В файле нет аудиодорожки")

                duration = 0.0
                if container.duration:
                    duration = float(container.duration) / av.time_base
                resampler = av.audio.resampler.AudioResampler(
                    format="flt", layout="mono", rate=SAMPLE_RATE
                )
                audio_buffer = np.array([], dtype=np.float32)
                self.status_signal.emit(f"Чтение аудиофайла: {os.path.basename(self.file_path)}")

                for frame in container.decode(audio=0):
                    if not self.running:
                        break
                    resampled_frames = resampler.resample(frame)
                    if not isinstance(resampled_frames, list):
                        resampled_frames = [resampled_frames]
                    for resampled in resampled_frames:
                        samples = resampled.to_ndarray().reshape(-1).astype(np.float32)
                        audio_buffer = np.concatenate((audio_buffer, samples))
                        while len(audio_buffer) >= SAMPLE_RATE:
                            chunk = audio_buffer[:SAMPLE_RATE]
                            audio_buffer = audio_buffer[SAMPLE_RATE:]
                            self.audio_chunk_ready.emit(chunk)
                            level = int(min(100, np.sqrt(np.mean(np.square(chunk))) * 350))
                            self.progress_signal.emit(level)

                    if duration and frame.pts is not None and frame.time_base:
                        percent = int(min(100, frame.pts * frame.time_base / duration * 100))
                        self.status_signal.emit(f"Импорт аудио: {percent}%")

                if len(audio_buffer):
                    self.audio_chunk_ready.emit(audio_buffer)
                self.progress_signal.emit(100)
                self.status_signal.emit("Импорт аудио завершен")
        except Exception as e:
            logger.exception("Ошибка чтения аудиофайла: %s", self.file_path)
            self.error_signal.emit(str(e))
        finally:
            self.running = False
            self.finished_signal.emit()

    def stop(self):
        self.running = False
        self.wait()


class ASRWorkerThread(QThread):
    text_recognized = Signal(str, str, float)  # speaker, text, duration
    status_signal = Signal(str)
    progress_signal = Signal(int)
    processing_progress_signal = Signal(int)
    speaker_profiles_updated = Signal(object)
    model_ready = Signal()
    model_failed = Signal(str)
    processing_finished = Signal()

    def __init__(
        self,
        model_size="gigaam-v3-e2e-rnnt",
        device="cpu",
        compute_type="int8",
        chunk_seconds=10,
        vad_silence_ms=1200,
        voice_threshold=0.015,
        segment_mode="pause",
        speaker_profiles=None,
        diarization_mode=DIARIZATION_PROFILES,
    ):
        super().__init__()
        self.model_size = model_size
        self.model_backend = (
            "whisper" if model_size in ASR_WHISPER_MODELS else "onnx"
        )
        self.device = device
        self.compute_type = compute_type
        self.chunk_seconds = chunk_seconds
        self.vad_silence_ms = vad_silence_ms
        self.voice_threshold = voice_threshold
        self.segment_mode = segment_mode
        if diarization_mode not in {
            DIARIZATION_PROFILES,
            DIARIZATION_AUTOMATIC,
            DIARIZATION_DISABLED,
        }:
            raise ValueError(f"Неизвестный режим диаризации: {diarization_mode}")
        self.diarization_mode = diarization_mode
        self.diarization_enabled = diarization_mode != DIARIZATION_DISABLED
        self.audio_queue = queue.Queue()
        self.running = False
        self.processing = False
        self.model = None
        self.embedder = SimpleSpeakerEmbedder(
            energy_threshold=voice_threshold * (2 / 3),
            speaker_profiles=speaker_profiles,
            use_profiles=self.diarization_mode == DIARIZATION_PROFILES,
        )
        self.last_voice_activity = time.monotonic()
        self.has_voice_since_start = False

    def set_voice_threshold(self, threshold):
        self.voice_threshold = threshold
        self.embedder.energy_threshold = threshold * (2 / 3)

    def add_audio(self, numpy_chunk):
        if self.processing and self.model is not None:
            self.audio_queue.put(numpy_chunk)
            logger.info("ASR queue size: %s, chunk_len=%s", self.audio_queue.qsize(), len(numpy_chunk))

    def _transcribe_phrase(self, audio_chunk):
        duration = len(audio_chunk) / SAMPLE_RATE
        if self.model_backend == "whisper":
            whisper_segments, _ = self.model.transcribe(
                audio_chunk,
                language="ru",
                beam_size={"tiny": 1, "small": 3}.get(self.model_size, 5),
                vad_filter=False,
                condition_on_previous_text=False,
                temperature=0.0,
                compression_ratio_threshold=2.4,
                log_prob_threshold=-1.0,
                no_speech_threshold=0.6,
            )
            segments = [
                (
                    segment.text,
                    float(segment.start),
                    max(float(segment.start), float(segment.end)),
                )
                for segment in whisper_segments
            ]
        else:
            result = self.model.recognize(audio_chunk, sample_rate=SAMPLE_RATE)
            if isinstance(result, (list, tuple)):
                result = result[0] if result else ""
            recognized_text = getattr(result, "text", result)
            segments = [(str(recognized_text or ""), 0.0, duration)]

        if self.diarization_enabled:
            timeline = self.embedder.get_speaker_timeline(audio_chunk)
            self.speaker_profiles_updated.emit(self.embedder.export_profiles())
            fallback_speaker = timeline[0][2] if timeline else "Спикер 1"
        else:
            timeline = []
            fallback_speaker = "Спикер не определён"
        utterances = []
        for raw_text, start, end in segments:
            text = str(raw_text).strip()
            if not text:
                continue
            end = max(start, end)
            speaker = (
                self.embedder.speaker_for_interval(start, end, timeline, fallback_speaker)
                if self.diarization_enabled
                else fallback_speaker
            )
            duration = max(0.0, end - start)
            if (
                utterances
                and utterances[-1][0] == speaker
                and not (
                    self.segment_mode == "semantic"
                    and utterances[-1][1].rstrip().endswith((".", "!", "?", "…", "。", "！", "？"))
                )
            ):
                utterances[-1][1] = f"{utterances[-1][1]} {text}"
                utterances[-1][2] += duration
            else:
                utterances.append([speaker, text, duration])
        return utterances

    def run(self):
        self.running = True
        # Optimized for CPU execution (int8 quantization)
        try:
            self.status_signal.emit("Загрузка модели распознавания...")
            self.progress_signal.emit(10)
            if self.model_backend == "whisper":
                if WhisperModel is None:
                    raise RuntimeError(
                        "Для выбранной модели Whisper нужен пакет faster-whisper."
                    )
                self.model = WhisperModel(
                    ASR_WHISPER_MODELS[self.model_size],
                    device=self.device,
                    compute_type=self.compute_type,
                    download_root=os.path.join(APP_DIR, "whisper-cache"),
                )
            else:
                try:
                    import onnx_asr
                except ImportError as exc:
                    raise RuntimeError(
                        "Backend GigaAM-v3 не найден в portable runtime. "
                        'Из папки приложения выполните: '
                        '.\\runtime\\python.exe -m pip install "onnx-asr[cpu,hub]", '
                        "затем перезапустите приложение."
                    ) from exc
                self.model = onnx_asr.load_model(ASR_ONNX_MODELS[self.model_size])
            self.progress_signal.emit(100)
            self.status_signal.emit("Распознавание готово")
            self.model_ready.emit()
            if not self.diarization_enabled:
                self.status_signal.emit("Распознавание готово (быстрый режим без диаризации)")
            elif self.embedder.initialize():
                self.status_signal.emit("Распознавание и определение спикеров готовы")
            else:
                self.status_signal.emit("Распознавание готово; модель спикеров недоступна")
        except Exception as e:
            logger.exception("Ошибка загрузки ASR-модели %s", self.model_size)
            self.status_signal.emit(f"Ошибка ASR: {e}")
            self.model_failed.emit(str(e))
            self.running = False
            return

        phrase_buffer = np.array([], dtype=np.float32)
        minimum_seconds = (
            max(3.0, float(self.chunk_seconds))
            if not self.diarization_enabled
            else max(2.0, float(self.chunk_seconds))
        )
        silence_threshold = max(0.7, self.vad_silence_ms / 1000.0)
        max_chunk_seconds = min(120.0, max(self.chunk_seconds + 12.0, minimum_seconds * 2.0))
        if self.model_size in ASR_ONNX_MODELS:
            max_chunk_seconds = min(max_chunk_seconds, 18.0)
        max_samples = int(SAMPLE_RATE * max_chunk_seconds)
        silence_elapsed = 0.0
        voice_active = False
        samples_since_speaker_check = 0
        baseline_speaker = None
        candidate_speaker = None
        candidate_speaker_count = 0

        while self.running:
            try:
                chunk = self.audio_queue.get(timeout=0.5)
                if chunk is None:
                    minimum_tail_seconds = (
                        0.25 if not self.diarization_enabled else 0.5
                    )
                    if (
                        len(phrase_buffer) >= SAMPLE_RATE * minimum_tail_seconds
                        and self.model is not None
                    ):
                        self.status_signal.emit("Распознавание последнего фрагмента...")
                        utterances = self._transcribe_phrase(phrase_buffer)
                        for speaker, text, duration in utterances:
                            self.text_recognized.emit(speaker, text, duration)
                    self.processing = False
                    phrase_buffer = np.array([], dtype=np.float32)
                    silence_elapsed = 0.0
                    voice_active = False
                    self.processing_progress_signal.emit(100)
                    self.status_signal.emit("Распознавание остановлено; остаток обработан")
                    self.processing_finished.emit()
                    continue
                if len(chunk) == 0:
                    continue

                phrase_buffer = np.concatenate((phrase_buffer, chunk))
                chunk_rms = float(np.sqrt(np.mean(np.square(chunk)))) if len(chunk) else 0.0

                if chunk_rms > self.voice_threshold:
                    voice_active = True
                    silence_elapsed = 0.0
                    self.last_voice_activity = time.monotonic()
                    self.has_voice_since_start = True
                elif voice_active:
                    silence_elapsed += len(chunk) / SAMPLE_RATE

                min_samples = int(SAMPLE_RATE * minimum_seconds)
                speaker_change_ready = False
                if (
                    self.diarization_enabled
                    and self.segment_mode == "speaker"
                    and voice_active
                    and len(phrase_buffer) >= SAMPLE_RATE * 3
                    and len(phrase_buffer) - samples_since_speaker_check >= SAMPLE_RATE
                ):
                    samples_since_speaker_check = len(phrase_buffer)
                    observed_speaker = self.embedder.get_speaker(
                        phrase_buffer[-SAMPLE_RATE * 3:]
                    )
                    if baseline_speaker is None:
                        baseline_speaker = observed_speaker
                    elif observed_speaker != baseline_speaker:
                        if observed_speaker == candidate_speaker:
                            candidate_speaker_count += 1
                        else:
                            candidate_speaker = observed_speaker
                            candidate_speaker_count = 1
                        speaker_change_ready = candidate_speaker_count >= 2
                    else:
                        candidate_speaker = None
                        candidate_speaker_count = 0

                pause_boundary = (
                    self.segment_mode in {"pause", "semantic"}
                    and silence_elapsed >= silence_threshold
                )
                duration_boundary = (
                    self.segment_mode == "duration"
                    and len(phrase_buffer) >= min_samples
                )
                fast_transcription_boundary = (
                    not self.diarization_enabled
                    and len(phrase_buffer) >= min_samples
                )
                phrase_ready = (
                    voice_active
                    and len(phrase_buffer) >= min_samples
                    and (
                        pause_boundary
                        or duration_boundary
                        or speaker_change_ready
                        or fast_transcription_boundary
                    )
                )
                force_ready = len(phrase_buffer) >= max_samples

                if phrase_ready or force_ready:
                    self.status_signal.emit("Распознавание фрагмента...")
                    self.processing_progress_signal.emit(25)
                    utterances = self._transcribe_phrase(phrase_buffer)
                    if utterances:
                        recognized_text = " ".join(item[1] for item in utterances)
                        logger.info("Распознан фрагмент ASR: %s", recognized_text[:200])
                        for speaker, text, duration in utterances:
                            self.text_recognized.emit(speaker, text, duration)

                    self.processing_progress_signal.emit(100)
                    self.status_signal.emit("Распознавание готово")
                    phrase_buffer = np.array([], dtype=np.float32)
                    silence_elapsed = 0.0
                    voice_active = False
                    samples_since_speaker_check = 0
                    baseline_speaker = None
                    candidate_speaker = None
                    candidate_speaker_count = 0

            except queue.Empty:
                continue
            except Exception as e:
                print(f"ASR error: {e}", file=sys.stderr)
                logger.exception("Ошибка ASR")

        self.processing = False
    def stop(self):
        self.running = False
        self.wait()
        self.model = None

    def start_processing(self):
        if not self.isRunning():
            raise RuntimeError("Модель распознавания не запущена")
        with self.audio_queue.mutex:
            self.audio_queue.queue.clear()
        if self.diarization_enabled:
            self.embedder.reset_session()
        self.processing = True
        self.status_signal.emit("Распознавание: поток ожидает речь")

    def stop_processing(self):
        self.processing = False
        with self.audio_queue.mutex:
            self.audio_queue.queue.clear()

    def finish_input(self):
        if self.processing:
            self.audio_queue.put(None)


class LLMProtocolThread(QThread):
    protocol_ready = Signal(str)
    mindmap_ready = Signal(str)
    mindmap_analysis_finished = Signal()
    transcript_review_ready = Signal(str)
    transcript_review_failed = Signal(str)
    protocol_due = Signal()
    protocol_generation_finished = Signal(bool)
    status_signal = Signal(str)
    progress_signal = Signal(int)
    model_ready = Signal()
    model_failed = Signal(str)

    def __init__(
        self,
        model_path=None,
        interval_seconds=300,
        backend="local",
        api_endpoint="",
        api_model="",
        api_key="",
    ):
        super().__init__()
        self.model_path = model_path
        self.interval_seconds = interval_seconds  # Default 5 minutes (300s) for CPU
        # Размер контекста локальной модели. Слишком длинный промпт (например,
        # большая стенограмма) иначе вызывает ошибку
        # "Requested tokens exceed context size". Можно переопределить
        # переменной окружения MEETING_LLM_N_CTX.
        try:
            self.n_ctx = max(2048, int(os.environ.get("MEETING_LLM_N_CTX", "16384")))
        except ValueError:
            self.n_ctx = 16384
        self.backend = backend
        self.api_endpoint = api_endpoint.rstrip("/")
        self.api_model = api_model
        self.api_key = api_key
        self.transcript_text = ""
        self.agenda_text = ""
        self.protocol_format = PROTOCOL_FORMAT_CURRENT
        self.lock = threading.Lock()
        self.llm_call_lock = threading.Lock()
        self.running = False
        self.llm = None
        self._is_ready = False

    def update_transcript(self, text):
        with self.lock:
            self.transcript_text = text

    def update_agenda(self, text):
        with self.lock:
            self.agenda_text = text

    def set_interval(self, interval_seconds):
        self.interval_seconds = interval_seconds

    def run(self):
        self.running = True
        if self.backend == "api":
            if httpx is None:
                self.model_failed.emit("Для API-режима не установлен пакет httpx")
                return
            if not self.api_endpoint or not self.api_model or not self.api_key:
                self.model_failed.emit("Укажите адрес API, модель и ключ")
                return
            self.status_signal.emit(f"Подключение к LLM API: {self.api_model}")
            self.progress_signal.emit(100)
            self._is_ready = True
            self.model_ready.emit()
        else:
            if Llama is None:
                self.status_signal.emit("Ошибка: библиотека llama-cpp-python не установлена.")
                self.model_failed.emit("llama-cpp-python не установлена")
                return
            self.status_signal.emit(f"Загрузка GGUF в RAM (CPU): {os.path.basename(self.model_path)}...")
            self.progress_signal.emit(10)
            try:
                self.llm = Llama(
                    model_path=self.model_path,
                    n_gpu_layers=0,
                    n_ctx=self.n_ctx,
                    n_threads=4,
                    verbose=False
                )
                self.status_signal.emit("Модель успешно загружена в ОЗУ (CPU режим).")
                self.progress_signal.emit(100)
                self._is_ready = True
                self.model_ready.emit()
            except Exception as e:
                self.status_signal.emit(f"Ошибка загрузки LLM: {e}")
                self.model_failed.emit(str(e))
                return

        last_run_time = time.time()

        while self.running:
            time.sleep(1)
            # If interval_seconds > 0, check timer
            if self.interval_seconds > 0:
                elapsed = time.time() - last_run_time
                if elapsed >= self.interval_seconds:
                    self.protocol_due.emit()
                    last_run_time = time.time()

    def _count_tokens(self, text):
        """Возвращает длину текста в токенах (реальный токенизатор модели, если загружен)."""
        if not text:
            return 0
        if self.backend == "local" and self.llm is not None:
            try:
                return len(
                    self.llm.tokenize(
                        text.encode("utf-8"), add_bos=False, special=False
                    )
                )
            except Exception:
                logger.debug(
                    "Не удалось токенизировать текст, используется оценка",
                    exc_info=True,
                )
        # Консервативная оценка: 2 символа на токен — не занижает длину промпта.
        return max(1, -(-len(text) // 2))

    def _fit_text(self, text, budget_tokens):
        """Сокращает текст под бюджет токенов, сохраняя его начало и конец."""
        if budget_tokens <= 0:
            return ""
        if self._count_tokens(text) <= budget_tokens:
            return text
        marker = (
            "\n[... середина стенограммы пропущена из-за ограничения "
            "контекста LLM ...]\n"
        )
        best = ""
        low, high = 0, len(text)
        while low <= high:
            middle = (low + high) // 2
            head_len = middle // 3
            tail_len = middle - head_len
            candidate = (
                text[:head_len]
                + (marker if tail_len else "")
                + (text[len(text) - tail_len:] if tail_len else "")
            )
            if self._count_tokens(candidate) <= budget_tokens:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        return best

    def _fit_messages(self, messages, max_tokens):
        """Гарантирует, что промпт локальной модели помещается в контекст."""
        if self.backend != "local" or self.llm is None:
            return messages
        # Запас на BOS, шаблон чата, служебные токены и /no_think у Qwen3.
        reserve = 256 + 32 * (len(messages) + 1)
        budget = self.n_ctx - max_tokens - reserve
        if budget <= 0:
            return messages
        counts = [
            self._count_tokens(str(message.get("content") or ""))
            for message in messages
        ]
        overflow = sum(counts) - budget
        if overflow <= 0:
            return messages
        candidates = [
            index
            for index, message in enumerate(messages)
            if message.get("role") == "user"
        ] or list(range(len(messages)))
        target = max(candidates, key=lambda index: counts[index])
        allowed = counts[target] - overflow
        if allowed <= 0:
            raise ValueError(
                "Текст не помещается в контекст LLM даже после сокращения. "
                "Очистите часть стенограммы или повестку."
            )
        fitted = [message.copy() for message in messages]
        fitted[target]["content"] = self._fit_text(
            str(fitted[target]["content"]), allowed
        )
        logger.warning(
            "Промпт LLM не влезал в контекст (%d > %d токенов): сокращён элемент %d",
            sum(counts),
            budget,
            target,
        )
        self.status_signal.emit(
            "Текст длиннее контекста LLM — часть стенограммы сокращена для генерации."
        )
        return fitted

    def _chat_completion(self, messages, max_tokens, temperature):
        if self.backend == "local":
            messages = self._fit_messages(messages, max_tokens)
            if self.model_path and "qwen3" in os.path.basename(self.model_path).casefold():
                messages = [message.copy() for message in messages]
                for message in reversed(messages):
                    if message.get("role") == "user" and isinstance(message.get("content"), str):
                        message["content"] = f"{message['content'].rstrip()} /no_think"
                        break
            return self.llm.create_chat_completion(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )["choices"][0]["message"]["content"]

        parsed_url = urlparse(self.api_endpoint)
        if parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
            raise ValueError("В URL API не допускаются встроенные учетные данные, параметры или фрагмент")
        if parsed_url.scheme != "https" and not (
            parsed_url.scheme == "http"
            and is_local_network_host(parsed_url.hostname)
        ):
            raise ValueError(
                "Для публичного API требуется HTTPS. HTTP разрешён только для localhost "
                "или IP-адресов частной сети."
            )
        response = httpx.post(
            openai_chat_completions_url(self.api_endpoint),
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.api_model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=httpx.Timeout(120.0, connect=20.0),
        )
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("API вернул пустой ответ")
        return content

    def generate_now(self):
        with self.lock:
            current_text = self.transcript_text.strip()
            agenda_text = self.agenda_text.strip()

        if not current_text or not self._is_ready:
            self.protocol_generation_finished.emit(False)
            return

        meeting_datetime = time.strftime("%d.%m.%Y %H:%M:%S")
        system_prompt = build_protocol_system_prompt(self.protocol_format, meeting_datetime)
        if agenda_text:
            system_prompt += (
                "\n\nИспользуй приложенную повестку как план и справочный контекст: "
                "соотноси с её пунктами только реально обсуждённые темы. Повестка не "
                "доказывает, что решение принято. Не выдумывай участников, реплики, "
                "решения или поручения. Если в стенограмме нет надёжных сведений о том, "
                "кто выступает, не назначай имя по одной лишь теме."
            )
        meeting_context = (
            f"Повестка совещания (контекст, не источник фактов):\n{agenda_text}\n\n"
            if agenda_text
            else ""
        )

        succeeded = False
        try:
            backend_status = "API" if self.backend == "api" else "CPU"
            self.status_signal.emit(f"Формирование протокола через {backend_status} (пожалуйста, подождите)...")
            self.progress_signal.emit(25)
            with self.llm_call_lock:
                protocol_text = self._chat_completion(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": (
                                f"{meeting_context}Стенограмма встречи:\n"
                                f"\"\"\"\n{current_text}\n\"\"\""
                            ),
                        }
                    ],
                    max_tokens=800,
                    temperature=0.2
                )
            if protocol_text:
                if (
                    self.protocol_format == PROTOCOL_FORMAT_CURRENT
                    and "Дата и время:" not in protocol_text
                ):
                    protocol_text = f"# Протокол совещания\n\n**Дата и время:** {meeting_datetime}\n\n{protocol_text}"
                self.protocol_ready.emit(protocol_text)
                self.status_signal.emit("Протокол успешно обновлен.")
                self.progress_signal.emit(100)
                succeeded = True
                self.analyze_mind_map(protocol_text)
        except Exception as e:
            logger.exception("Ошибка формирования протокола")
            self.status_signal.emit(f"Ошибка генерации LLM: {e}")
        finally:
            self.protocol_generation_finished.emit(succeeded)

    def review_transcript(self, transcript_text):
        transcript_text = transcript_text.strip()
        if not transcript_text or not self._is_ready:
            self.transcript_review_failed.emit(
            "Нет стенограммы для проверки или LLM ещё не загружена."
            )
            return

        system_prompt = (
            "Ты — редактор стенограмм русскоязычных совещаний. Проверь только явные "
            "опечатки и очевидные ошибки распознавания по контексту. Не пересказывай, "
            "не сокращай и не дополняй реплики. Сохраняй исходный смысл, временные "
            "метки, имена спикеров, порядок реплик и разбиение на строки. Проверь "
            "связность смысла между соседними репликами и исправляй вероятную ошибку распознавания "
            "только при высокой уверенности. Если слово, связь или смысл неясны, "
            "не угадывай и оставь исходный вариант. Верни только исправленный "
            "текст без заголовков, комментариев и Markdown."
        )
        with self.lock:
            agenda_text = self.agenda_text.strip()
        agenda_context = ""
        if agenda_text:
            system_prompt += (
                " Повестка ниже — только контекст тем и возможных имён. Не добавляй её "
                "текст в стенограмму и не меняй спикера лишь на основании темы."
            )
            agenda_context = f"Контекст повестки:\n{agenda_text}\n\n"

        source_lines = transcript_text.splitlines()
        chunks = []
        current_lines = []
        current_length = 0
        for line in source_lines:
            if current_lines and current_length + len(line) + 1 > 2200:
                chunks.append("\n".join(current_lines))
                current_lines = []
                current_length = 0
            current_lines.append(line)
            current_length += len(line) + 1
        if current_lines:
            chunks.append("\n".join(current_lines))

        reviewed_chunks = []
        try:
            self.status_signal.emit("Проверка стенограммы: смысл и ошибки распознавания...")
            with self.llm_call_lock:
                for chunk in chunks:
                    reviewed = self._chat_completion(
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {
                                "role": "user",
                                "content": f"{agenda_context}Стенограмма:\n{chunk}",
                            },
                        ],
                        max_tokens=1800,
                        temperature=0.0,
                    )
                    if not reviewed or not reviewed.strip():
                        raise ValueError("LLM вернула пустой текст при проверке стенограммы")
                    reviewed_chunks.append(reviewed.strip("\n"))
            self.transcript_review_ready.emit("\n".join(reviewed_chunks))
            self.status_signal.emit("Проверка стенограммы завершена; проверьте предложенные изменения.")
        except Exception as exc:
            logger.exception("Ошибка проверки стенограммы")
            self.transcript_review_failed.emit(str(exc))

    def analyze_mind_map(self, protocol_text):
        with self.lock:
            transcript_text = self.transcript_text.strip()[-5000:]
            agenda_text = self.agenda_text.strip()
        if not self._is_ready or not transcript_text:
            self.mindmap_analysis_finished.emit()
            return

        prompt = (
            "Преобразуй совещание в содержательную mind map. Используй одновременно стенограмму и протокол. "
            "Не создавай узлы из каждой реплики: сгруппируй повторяющиеся мысли в темы и подтемы. "
            "Выделяй темы и подтемы, решения, задачи, риски, открытые вопросы, явно "
            "подтверждённые согласия и возражения. Показывай их связь с темами, а не "
            "создавай узел для каждой реплики. Короткое «да» не считай отдельным решением. "
            "Не делай вывод о перебивании, если стенограмма этого не подтверждает. "
            "Верни только валидный JSON без Markdown и пояснений такого вида: "
            '{"title":"Тема совещания","nodes":[{"id":"n1","label":"Краткий заголовок",'
            '"type":"topic|decision|task|risk|question|agreement|objection",'
            '"details":"Краткий контекст, участник, срок или временная метка",'
            '"parent_id":"root"}],"edges":[{"source":"root","target":"n1","relation":"обсуждает"}]}. '
            "Используй 5-20 наиболее значимых узлов. Строй понятную иерархию parent_id: "
            "узлы решений, задач, рисков и вопросов располагай под соответствующей темой. "
            "Связями relation дополнительно показывай подтвержденные отношения (например, "
            "«решает», «порождает риск», «возражает»). У каждого узла должен быть уникальный id. "
            "Связи должны ссылаться на существующие id. Сохраняй имена и метки времени, "
            "если они есть в стенограмме. Не выдумывай факты."
        )
        try:
            self.status_signal.emit("LLM анализирует темы и связи Mind Map...")
            agenda_context = (
                f"ПОВЕСТКА (ориентир для группировки тем, не источник фактов):\n{agenda_text}\n\n"
                if agenda_text
                else ""
            )
            with self.llm_call_lock:
                result = self._chat_completion(
                    messages=[
                        {"role": "system", "content": prompt},
                        {
                            "role": "user",
                            "content": (
                                f"{agenda_context}СТЕНОГРАММА:\n{transcript_text}\n\n"
                                f"ПРОТОКОЛ:\n{protocol_text[-1500:]}"
                            ),
                        },
                    ],
                    max_tokens=900,
                    temperature=0.1
                )
            result = result.strip()
            self.mindmap_ready.emit(result)
            logger.info("LLM сформировала Mind Map")
        except Exception as e:
            logger.exception("Ошибка анализа Mind Map")
            self.status_signal.emit(f"Ошибка анализа Mind Map: {e}")
        finally:
            self.mindmap_analysis_finished.emit()

    def stop(self):
        self.running = False
        self.wait()
        self.llm = None
        self.api_key = ""
        self._is_ready = False


class MindMapConnection(QGraphicsLineItem):
    def __init__(self, source_node, target_node, relation=""):
        super().__init__()
        self.source_node = source_node
        self.target_node = target_node
        self.relation_label = None
        self.setZValue(-1)
        edge_pen = QPen("#78909c")
        edge_pen.setWidth(2)
        self.setPen(edge_pen)
        source_node.connections.append(self)
        target_node.connections.append(self)
        self.update_position()

    def update_position(self):
        source_rect = self.source_node.sceneBoundingRect()
        target_rect = self.target_node.sceneBoundingRect()
        source = source_rect.center()
        target = target_rect.center()
        dx = target.x() - source.x()
        dy = target.y() - source.y()
        if not dx and not dy:
            return
        source_ratio = min(
            source_rect.width() / (2 * abs(dx)) if dx else float("inf"),
            source_rect.height() / (2 * abs(dy)) if dy else float("inf"),
        )
        target_ratio = min(
            target_rect.width() / (2 * abs(dx)) if dx else float("inf"),
            target_rect.height() / (2 * abs(dy)) if dy else float("inf"),
        )
        self.setLine(
            source.x() + dx * source_ratio,
            source.y() + dy * source_ratio,
            target.x() - dx * target_ratio,
            target.y() - dy * target_ratio,
        )
        if self.relation_label is not None:
            self.relation_label.setPos((source.x() + target.x()) / 2, (source.y() + target.y()) / 2)


class MindMapNodeItem(QGraphicsRectItem):
    def __init__(self, node_id, label, details, node_type, width, height, fill_color):
        super().__init__(0, 0, width, height)
        self.node_id = node_id
        self.connections = []
        self.setBrush(QBrush(fill_color))
        outline_pen = QPen("#607d8b")
        outline_pen.setWidthF(1.5)
        self.setPen(outline_pen)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setZValue(1)
        self.text_item = QGraphicsTextItem(self)
        self.text_item.setTextWidth(width - 20)
        self.text_item.setPos(10, 6)
        self.text_item.setDefaultTextColor("#ffffff" if node_type == "root" else "#263238")
        self.text_item.setFont(QFont("Segoe UI", 9))
        type_labels = {
            "root": "СОВЕЩАНИЕ",
            "topic": "ТЕМА",
            "decision": "РЕШЕНИЕ",
            "task": "ЗАДАЧА",
            "risk": "РИСК",
            "question": "ОТКРЫТЫЙ ВОПРОС",
            "agreement": "СОГЛАСИЕ",
            "objection": "ВОЗРАЖЕНИЕ",
        }
        details = details.strip()
        visible_details = details if len(details) <= 260 else f"{details[:257].rstrip()}..."
        text = f"{label}\n{type_labels.get(node_type, 'ТЕМА')}"
        if visible_details:
            text += f"\n{visible_details}"
        self.text_item.setPlainText(text)
        self.text_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setToolTip(f"{label}\n{type_labels.get(node_type, 'ТЕМА')}\n{details}".strip())
        self.setRect(0, 0, width, max(height, self.text_item.boundingRect().height() + 14))

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            for connection in self.connections:
                connection.update_position()
        return super().itemChange(change, value)


class MindMapView(QGraphicsView):
    def __init__(self):
        super().__init__()
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.style_name = "Деловой"
        self.layout_name = "Иерархический"
        self.graph = {"title": "Совещание", "nodes": [], "edges": []}
        self.node_items = {}
        self._zoom = 1.0
        self._has_fitted = False
        self._panning = False
        self._last_pan_pos = None
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.viewport().installEventFilter(self)

    def fit_graph(self):
        if not self.scene.items():
            return
        self._zoom = 1.0
        self.resetTransform()
        self.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self._has_fitted = True

    def zoom_by(self, factor):
        next_zoom = self._zoom * factor
        if 0.25 <= next_zoom <= 4.0:
            self.scale(factor, factor)
            self._zoom = next_zoom

    def set_options(self, style_name, layout_name):
        layout_changed = self.layout_name != layout_name
        self.style_name = style_name
        self.layout_name = layout_name
        if layout_changed:
            self.node_items.clear()
            self._has_fitted = False

    def set_graph(self, graph):
        if graph != self.graph:
            self._has_fitted = False
        self.graph = graph
        self.rebuild()

    def rebuild(self):
        previous_positions = {
            node_id: (item.pos().x(), item.pos().y())
            for node_id, item in self.node_items.items()
            if self.layout_name == "Свободный"
        }
        self.scene.clear()
        self.node_items = {}
        title = self.graph.get("title") or "Совещание"
        nodes = self.graph.get("nodes", [])
        edges = self.graph.get("edges", [])
        palette = {
            "Деловой": {"root": "#1565c0", "topic": "#e3f2fd", "decision": "#e8f5e9", "task": "#fff3e0", "risk": "#ffebee", "question": "#f3e5f5", "agreement": "#e0f2f1", "objection": "#fff8e1"},
            "Цветной": {"root": "#7b1fa2", "topic": "#ede7f6", "decision": "#dcedc8", "task": "#ffe0b2", "risk": "#ffcdd2", "question": "#e1f5fe", "agreement": "#b2dfdb", "objection": "#ffecb3"},
            "Монохромный": {"root": "#455a64", "topic": "#eceff1", "decision": "#e0e0e0", "task": "#cfd8dc", "risk": "#b0bec5", "question": "#f5f5f5", "agreement": "#d7e0e2", "objection": "#c5cdd0"},
        }.get(self.style_name, {})
        node_width, node_height = 250, 82

        normalized_nodes = [{"id": "root", "label": title, "details": "Совещание", "type": "root", "parent_id": None}] + nodes
        node_map = {}
        for index, data in enumerate(normalized_nodes):
            node_id = str(data.get("id") or f"node-{index}")
            node = MindMapNodeItem(
                node_id, str(data.get("label", "Тема")), str(data.get("details", "")),
                str(data.get("type", "topic")) if str(data.get("type", "topic")) in {"root", "topic", "decision", "task", "risk", "question", "agreement", "objection"} else "topic",
                node_width, node_height,
                palette.get(str(data.get("type", "topic")), palette.get("topic", "#e3f2fd"))
            )
            self.scene.addItem(node)
            self.node_items[node_id] = node
            node_map[node_id] = data

        if self.layout_name == "Радиальный":
            radial_nodes = list(self.node_items.values())[1:]
            for index, node in enumerate(radial_nodes):
                ring = index // 8
                ring_index = index % 8
                ring_count = min(8, len(radial_nodes) - ring * 8)
                angle = 2 * np.pi * ring_index / max(1, ring_count)
                radius = 390 + ring * 310
                node.setPos(
                    radius * np.cos(angle) - node_width / 2,
                    radius * np.sin(angle) - node_height / 2,
                )
            self.node_items["root"].setPos(-node_width / 2, -node_height / 2)
        elif self.layout_name == "Свободный" and previous_positions:
            for node_id, position in previous_positions.items():
                if node_id in self.node_items:
                    self.node_items[node_id].setPos(*position)
            if "root" not in previous_positions:
                self.node_items["root"].setPos(-node_width / 2, -node_height / 2)
        else:
            parent_by_id = {"root": None}
            for data in nodes:
                node_id = str(data.get("id"))
                parent_id = str(data.get("parent_id") or "root")
                parent_by_id[node_id] = (
                    parent_id if parent_id in self.node_items and parent_id != node_id else "root"
                )

            def build_children():
                result = {node_id: [] for node_id in self.node_items}
                for node_id, parent_id in parent_by_id.items():
                    if node_id != "root" and parent_id in result:
                        result[parent_id].append(node_id)
                return result

            children = build_children()
            reachable = {"root"}
            pending = ["root"]
            while pending:
                parent_id = pending.pop(0)
                for child_id in children.get(parent_id, []):
                    if child_id not in reachable:
                        reachable.add(child_id)
                        pending.append(child_id)
            for node_id in self.node_items:
                if node_id not in reachable:
                    parent_by_id[node_id] = "root"
            children = build_children()
            horizontal_gap = 42
            vertical_gap = 190
            subtree_widths = {}

            def measure_subtree(node_id, visiting=None):
                if node_id in subtree_widths:
                    return subtree_widths[node_id]
                visiting = visiting or set()
                if node_id in visiting:
                    return node_width
                visiting.add(node_id)
                child_ids = children.get(node_id, [])
                children_width = sum(
                    measure_subtree(child_id, visiting) for child_id in child_ids
                ) + horizontal_gap * max(0, len(child_ids) - 1)
                visiting.remove(node_id)
                subtree_widths[node_id] = max(node_width, children_width)
                return subtree_widths[node_id]

            def place_subtree(node_id, left, depth):
                subtree_width = measure_subtree(node_id)
                self.node_items[node_id].setPos(
                    left + (subtree_width - node_width) / 2 - node_width / 2,
                    depth * vertical_gap,
                )
                child_ids = children.get(node_id, [])
                children_width = sum(
                    subtree_widths[child_id] for child_id in child_ids
                ) + horizontal_gap * max(0, len(child_ids) - 1)
                child_left = left + (subtree_width - children_width) / 2
                for child_id in child_ids:
                    place_subtree(child_id, child_left, depth + 1)
                    child_left += subtree_widths[child_id] + horizontal_gap

            place_subtree("root", 0, 0)

        added_edges = set()
        for edge in edges:
            source_id = str(edge.get("source", "root"))
            target_id = str(edge.get("target", ""))
            if (
                source_id in self.node_items
                and target_id in self.node_items
                and source_id != target_id
                and (source_id, target_id) not in added_edges
            ):
                connection = MindMapConnection(self.node_items[source_id], self.node_items[target_id], edge.get("relation", ""))
                self.scene.addItem(connection)
                added_edges.add((source_id, target_id))
                relation = str(edge.get("relation", "")).strip()
                if relation:
                    connection.relation_label = self.scene.addText(relation, QFont("Segoe UI", 8))
                    connection.relation_label.setDefaultTextColor("#78909c")
                    connection.relation_label.setZValue(0)
                    connection.update_position()
        for data in nodes:
            target_id = str(data.get("id", ""))
            parent_id = str(data.get("parent_id") or "root")
            if (
                target_id in self.node_items
                and parent_id in self.node_items
                and parent_id != target_id
                and (parent_id, target_id) not in added_edges
            ):
                self.scene.addItem(
                    MindMapConnection(self.node_items[parent_id], self.node_items[target_id])
                )

        if not nodes:
            hint = self.scene.addText("Сформируйте протокол, чтобы LLM построила карту тем, решений, задач и рисков")
            hint.setDefaultTextColor("#9e9e9e")
            hint.setPos(-200, 130)
        self.scene.setSceneRect(self.scene.itemsBoundingRect().adjusted(-100, -100, 100, 100))
        if not self._has_fitted:
            self.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
            self._has_fitted = True

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.zoom_by(factor)
        event.accept()

    def eventFilter(self, watched, event):
        if watched is self.viewport():
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.MiddleButton:
                self._panning = True
                self._last_pan_pos = event.position().toPoint()
                self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
                return True
            if event.type() == QEvent.Type.MouseMove and self._panning and self._last_pan_pos is not None:
                current_pos = event.position().toPoint()
                delta = current_pos - self._last_pan_pos
                self._last_pan_pos = current_pos
                self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
                self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
                return True
            if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.MiddleButton:
                self._panning = False
                self._last_pan_pos = None
                self.viewport().unsetCursor()
                return True
        return super().eventFilter(watched, event)


class SpeakerProfilesDialog(QDialog):
    def __init__(self, profiles, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Профили спикеров")
        self.resize(620, 420)
        self.profiles = normalize_speaker_profiles(profiles)
        self.result_profiles = {}
        self.renamed_profiles = {}

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Переименуйте, импортируйте или удалите профили голосов. Чтобы создать профиль "
            "для текущего участника, используйте кнопку «Спикер» после распознавания его речи. "
            "Файл содержит биометрические данные голоса — храните и передавайте его осторожно."
        ))
        self.table = QTableWidget(0, 1)
        self.table.setHorizontalHeaderLabels(["Имя профиля"])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked
            | QTableWidget.EditTrigger.EditKeyPressed
            | QTableWidget.EditTrigger.AnyKeyPressed
        )
        self.refresh_table()
        layout.addWidget(self.table)

        controls = QHBoxLayout()
        self.btn_import = QPushButton("Импорт JSON")
        self.btn_export = QPushButton("Экспорт JSON")
        self.btn_delete = QPushButton("Удалить выбранные")
        self.btn_import.clicked.connect(self.import_profiles)
        self.btn_export.clicked.connect(self.export_profiles)
        self.btn_delete.clicked.connect(self.remove_selected)
        controls.addWidget(self.btn_import)
        controls.addWidget(self.btn_export)
        controls.addWidget(self.btn_delete)
        controls.addStretch()
        layout.addLayout(controls)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        self.buttons.accepted.connect(self.save_profiles)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def refresh_table(self):
        self.table.setRowCount(len(self.profiles))
        for row, (name, _embedding) in enumerate(
            sorted(self.profiles.items(), key=lambda item: item[0].casefold())
        ):
            item = QTableWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.table.setItem(row, 0, item)

    def collect_profiles(self):
        profiles = {}
        renamed = {}
        seen = set()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None:
                continue
            name = item.text().strip()
            original_name = str(item.data(Qt.ItemDataRole.UserRole) or "")
            if not name:
                raise ValueError("Имя профиля не может быть пустым.")
            normalized = name.casefold()
            if normalized in seen:
                raise ValueError(f"Профиль «{name}» указан несколько раз.")
            seen.add(normalized)
            if original_name not in self.profiles:
                raise ValueError(f"Не найден эмбеддинг профиля «{original_name}».")
            profiles[name] = self.profiles[original_name]
            if name != original_name:
                renamed[original_name] = name
        return normalize_speaker_profiles(profiles), renamed

    def remove_selected(self):
        rows = sorted(
            {index.row() for index in self.table.selectionModel().selectedRows()},
            reverse=True,
        )
        for row in rows:
            self.table.removeRow(row)

    def import_profiles(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Импорт профилей спикеров", "", "JSON (*.json)"
        )
        if not path:
            return
        try:
            incoming = load_speaker_profiles(path)
            conflicts = set(self.profiles).intersection(incoming)
            replace_conflicts = False
            if conflicts:
                answer = QMessageBox.question(
                    self,
                    "Профили уже существуют",
                    "Заменить одноимённые профили импортированными? "
                    "При ответе «Нет» будут добавлены только новые профили.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                replace_conflicts = answer == QMessageBox.StandardButton.Yes
            merged = dict(self.profiles)
            for name, embedding in incoming.items():
                existing_name = next(
                    (old for old in merged if old.casefold() == name.casefold()),
                    None,
                )
                if existing_name is None:
                    merged[name] = embedding
                elif replace_conflicts:
                    del merged[existing_name]
                    merged[name] = embedding
            merged = normalize_speaker_profiles(merged)
            self.profiles = merged
            self.refresh_table()
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.exception("Не удалось импортировать профили спикеров из %s", path)
            QMessageBox.critical(self, "Ошибка импорта профилей", str(exc))

    def export_profiles(self):
        try:
            profiles, _ = self.collect_profiles()
        except ValueError as exc:
            QMessageBox.warning(self, "Некорректные профили", str(exc))
            return
        if not profiles:
            QMessageBox.information(self, "Нет профилей", "Сначала сохраните или импортируйте профили.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт профилей спикеров", "speaker_profiles.json", "JSON (*.json)"
        )
        if not path:
            return
        if not path.casefold().endswith(".json"):
            path += ".json"
        try:
            save_speaker_profiles(profiles, path)
            QMessageBox.information(
                self, "Экспорт завершён", f"Профили сохранены:\n{path}"
            )
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Ошибка экспорта профилей", str(exc))

    def save_profiles(self):
        try:
            self.result_profiles, self.renamed_profiles = self.collect_profiles()
        except ValueError as exc:
            QMessageBox.warning(self, "Некорректные профили", str(exc))
            return
        self.accept()


class MeetingAgendaDialog(QDialog):
    def __init__(self, agenda_text, agenda_library, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Повестка и частые темы")
        self.resize(760, 560)
        self.result_agenda = str(agenda_text or "")
        self.result_library = dict(agenda_library)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Впишите план, темы и докладчиков или загрузите файл. LLM использует повестку "
            "как контекст для группировки обсуждения, но не как доказательство решений."
        ))
        self.editor = QTextEdit()
        self.editor.setPlaceholderText(
            "Например:\n1. Бюджет проекта — докладчик: Иван Иванов\n"
            "2. Сроки запуска — докладчик: Мария Петрова"
        )
        self.editor.setPlainText(self.result_agenda)
        layout.addWidget(self.editor, 1)

        library_row = QHBoxLayout()
        self.template_combo = QComboBox()
        self.template_combo.addItem("Библиотека частых повесток", None)
        for title in sorted(self.result_library, key=str.casefold):
            self.template_combo.addItem(title, title)
        self.template_combo.setToolTip(
            "Локальная библиотека часто используемых тем и шаблонов повесток."
        )
        insert_button = QPushButton("Вставить")
        insert_button.clicked.connect(self.insert_template)
        add_button = QPushButton("В библиотеку")
        add_button.clicked.connect(self.add_template)
        remove_button = QPushButton("Удалить шаблон")
        remove_button.clicked.connect(self.remove_template)
        import_button = QPushButton("Загрузить файл")
        import_button.setToolTip("Загрузить текстовую повестку из TXT, Markdown или Word DOCX.")
        import_button.clicked.connect(self.import_agenda)
        library_row.addWidget(self.template_combo, 1)
        library_row.addWidget(insert_button)
        library_row.addWidget(add_button)
        library_row.addWidget(remove_button)
        library_row.addWidget(import_button)
        layout.addLayout(library_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить повестку")
        buttons.accepted.connect(self.save_agenda)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def insert_template(self):
        title = self.template_combo.currentData()
        if title:
            self.editor.setPlainText(self.result_library[title])

    def add_template(self):
        title, accepted = QInputDialog.getText(
            self, "Сохранить частую тему", "Название шаблона:"
        )
        title = title.strip()
        content = self.editor.toPlainText().strip()
        if not accepted or not title or not content:
            if accepted and (not title or not content):
                QMessageBox.information(
                    self, "Пустой шаблон", "Укажите название и заполните текст повестки."
                )
            return
        existing = next(
            (name for name in self.result_library if name.casefold() == title.casefold()),
            None,
        )
        if existing:
            answer = QMessageBox.question(
                self,
                "Шаблон уже существует",
                f"Заменить шаблон «{existing}» текущей повесткой?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            del self.result_library[existing]
            for index in range(1, self.template_combo.count()):
                if self.template_combo.itemData(index) == existing:
                    self.template_combo.removeItem(index)
                    break
        self.result_library[title] = content
        self.template_combo.addItem(title, title)
        self.template_combo.setCurrentIndex(self.template_combo.count() - 1)

    def remove_template(self):
        title = self.template_combo.currentData()
        if not title:
            return
        del self.result_library[title]
        self.template_combo.removeItem(self.template_combo.currentIndex())

    def import_agenda(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Загрузить повестку",
            "",
            "Повестка (*.txt *.md *.docx);;Текстовые файлы (*.txt *.md);;Word (*.docx)",
        )
        if not path:
            return
        try:
            extension = os.path.splitext(path)[1].casefold()
            if extension in {".txt", ".md"}:
                with open(path, "r", encoding="utf-8-sig") as agenda_file:
                    content = agenda_file.read()
            elif extension == ".docx":
                if Document is None:
                    raise RuntimeError("Для чтения DOCX установите python-docx.")
                content = "\n".join(
                    paragraph.text
                    for paragraph in Document(path).paragraphs
                    if paragraph.text.strip()
                )
            else:
                raise ValueError("Поддерживаются только TXT, Markdown и DOCX.")
            self.editor.setPlainText(content)
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
            logger.exception("Не удалось загрузить повестку из %s", path)
            QMessageBox.critical(self, "Ошибка загрузки повестки", str(exc))

    def save_agenda(self):
        self.result_agenda = self.editor.toPlainText().strip()
        self.accept()


class MeetingSecretaryMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"ИИ-Секретарь Lite 2 | {get_pc_configuration()}")
        screen = QApplication.primaryScreen()
        available_geometry = screen.availableGeometry() if screen else None
        target_width = min(1200, available_geometry.width()) if available_geometry else 1200
        target_height = min(780, available_geometry.height()) if available_geometry else 780
        self.setMinimumSize(min(720, target_width), min(560, target_height))
        self.resize(target_width, target_height)
        if available_geometry:
            self.move(
                available_geometry.x() + (available_geometry.width() - target_width) // 2,
                available_geometry.y() + (available_geometry.height() - target_height) // 2,
            )

        self.full_transcript = []  # dict: {raw_speaker, display_speaker, text, duration, timestamp}
        self.speaker_names = {}    # original -> custom
        self.speaker_airtime = {}  # speaker -> total seconds
        self.settings = QSettings("MeetingSecretary", "MeetingSecretaryLite2")
        self.agenda_load_error = ""
        try:
            self.agenda_text, self.agenda_library = load_meeting_agenda()
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            self.agenda_text = ""
            self.agenda_library = {}
            self.agenda_load_error = str(exc)
        self.speaker_profile_load_error = ""
        try:
            self.speaker_profiles = load_speaker_profiles()
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            self.speaker_profiles = {}
            self.speaker_profile_load_error = str(exc)
        self.protocol_versions = [] # list of (timestamp, md_text)

        self.audio_thread = None
        self.audio_file_thread = None
        self.asr_thread = None
        self.llm_thread = None
        self.models_loaded = False
        self.recording_active = False
        self.recording_paused = False
        self._pause_pending = False
        self._updating_transcript = False
        self._asr_loaded = False
        self._llm_loaded = False
        self._asr_loading = False
        self._llm_loading = False
        self._asr_text_connected = False
        self._asr_finishing = False
        self._asr_job_kind = None
        self._review_in_progress = False
        self._review_source_text = ""
        self._reviewed_transcript = ""
        self._pending_protocol_after_review = False
        self._protocol_generation_active = False
        self._mind_map_analysis_active = False
        self._pending_llm_action = None
        self.mind_map_graph = {"title": "Совещание", "nodes": [], "edges": []}
        self.recovery_timer = QTimer(self)
        self.recovery_timer.timeout.connect(self.save_recovery_snapshot)
        self.recovery_timer.start(10000)

        self._init_ui()
        try:
            self.api_key_edit.setText(load_api_credential())
        except OSError as exc:
            logger.exception("Не удалось получить API-ключ из Windows Credential Manager")
            self.status_bar.showMessage(f"Ошибка Credential Manager: {exc}")
        self.refresh_model_list()
        if self.speaker_profile_load_error:
            self.status_bar.showMessage(
                f"Профили спикеров не загружены: {self.speaker_profile_load_error}"
            )
        if self.agenda_load_error:
            self.status_bar.showMessage(
                f"Повестка не загружена: {self.agenda_load_error}"
            )

    def _init_ui(self):
        app = QApplication.instance()
        if app:
            app.setStyleSheet(
                app.styleSheet()
                + "\nQToolTip { color: #ffffff; background-color: #263238; "
                "border: 1px solid #607d8b; padding: 5px; }"
            )
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)

        vertical_splitter = QSplitter(Qt.Vertical)
        action_status_widget = QWidget()
        action_status_layout = QVBoxLayout(action_status_widget)
        action_status_layout.setContentsMargins(0, 0, 0, 0)

        # 1. Top Bar: Recording, Models & Interval Mode
        top_group = QGroupBox("Настройки Lite")
        top_group.setMinimumWidth(0)
        top_group.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        top_layout = QVBoxLayout(top_group)
        top_layout.setSpacing(4)
        control_row = QHBoxLayout()
        control_row.setSpacing(5)
        audio_row = QHBoxLayout()
        audio_row.setSpacing(5)

        self.btn_start = QPushButton("▶ Запись")
        self.btn_start.setEnabled(False)
        self.btn_start.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold; padding: 6px;")
        self.btn_start.setToolTip("Начать запись с выбранного микрофона и распознавать речь по ходу совещания.")
        self.btn_start.clicked.connect(self.start_meeting)

        self.btn_stop = QPushButton("⏹ Стоп")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet("background-color: #c62828; color: white; font-weight: bold; padding: 6px;")
        self.btn_stop.setToolTip("Остановить запись и отправить накопленный фрагмент на распознавание.")
        self.btn_stop.clicked.connect(self.stop_meeting)

        self.btn_pause = QPushButton("⏸ Пауза")
        self.btn_pause.setEnabled(False)
        self.btn_pause.setToolTip(
            "В быстром режиме приостановить захват, дождаться распознавания остатка "
            "и затем продолжить встречу с временной меткой."
        )
        self.btn_pause.clicked.connect(self.toggle_recording_pause)

        self.save_audio_checkbox = QCheckBox("Сохранять аудио")
        self.save_audio_checkbox.setChecked(
            self.settings.value("audio/save_recording", False, type=bool)
        )
        self.save_audio_checkbox.setToolTip(
            "При начале записи предложить место сохранения оригинального звука в WAV "
            "(моно, 16 кГц). Запись выключена по умолчанию."
        )
        self.save_audio_checkbox.toggled.connect(
            lambda checked: self.settings.setValue("audio/save_recording", checked)
        )

        self.btn_load_models = QPushButton("⬆ ASR")
        self.btn_load_models.setStyleSheet("background-color: #00838f; color: white; font-weight: bold; padding: 6px;")
        self.btn_load_models.setToolTip(
            "Загрузить или выгрузить модель распознавания речи. Для записи и импорта "
            "аудио достаточно загрузить ASR; LLM загружается отдельно для проверки и протокола."
        )
        self.btn_load_models.clicked.connect(self.toggle_asr_model)
        self.btn_load_llm = QPushButton("⬆ LLM")
        self.btn_load_llm.setToolTip(
            "Загрузить или выгрузить выбранную модель для проверки стенограммы, протокола "
            "и анализа Mind Map. LLM можно загрузить только после распознавания аудио."
        )
        self.btn_load_llm.clicked.connect(self.toggle_llm_model)

        self.combo_whisper = QComboBox()
        asr_choices = [
            ("Whisper tiny", "tiny", "Whisper tiny — самый быстрый и наименее точный вариант."),
            ("Whisper small", "small", "Whisper small — рекомендуемый баланс качества русского распознавания и скорости на CPU."),
            ("Whisper medium", "medium", "Whisper medium — обычно точнее small, но заметно медленнее и требует больше памяти."),
            ("Whisper large-v3-turbo", "large-v3-turbo", "Whisper large-v3-turbo — высокая точность и скорость, но требуется больше памяти."),
            ("GigaAM v3", "gigaam-v3-e2e-rnnt", "GigaAM-v3 E2E RNNT — русскоязычная модель; при первом запуске без локального кэша нужны файлы модели."),
        ]
        for label, model_key, description in asr_choices:
            self.combo_whisper.addItem(label, userData=model_key)
            self.combo_whisper.setItemData(
                self.combo_whisper.count() - 1,
                description,
                Qt.ItemDataRole.ToolTipRole,
            )
        saved_asr_model = str(self.settings.value("asr/model", "small"))
        saved_asr_index = self.combo_whisper.findData(saved_asr_model)
        self.combo_whisper.setCurrentIndex(
            saved_asr_index
            if saved_asr_index >= 0
            else self.combo_whisper.findData("small")
        )
        self.combo_whisper.currentIndexChanged.connect(self.on_asr_model_changed)
        self.combo_whisper.setEnabled(True)
        self.combo_whisper.setMaximumWidth(130)
        self.combo_whisper.setToolTip(
            "Выберите ASR перед нажатием «⬆ ASR». Whisper small — рекомендуемый баланс; "
            "tiny быстрее, medium и large-v3-turbo требуют больше времени/памяти. "
            "Веса Whisper small включены в portable-кэш; остальные модели при первом запуске "
            "загружаются в папку whisper-cache. GigaAM доступна как альтернативный backend."
        )
        self.chunk_seconds_spin = QSpinBox()
        self.chunk_seconds_spin.setRange(1, 30)
        self.chunk_seconds_spin.setSingleStep(1)
        self.chunk_seconds_spin.setValue(3)
        self.chunk_seconds_spin.setSuffix(" с фрагмент")
        self.chunk_seconds_spin.setMaximumWidth(118)
        self.chunk_seconds_spin.setToolTip(
            "Длина аудиоблока до распознавания. В быстром режиме используется минимум 3 секунды: "
            "это уменьшает разбиение фраз, сохраняя вывод текста во время записи. Увеличьте "
            "длительность, чтобы реже делить предложения; фактическая задержка зависит от скорости ASR. "
            "В режимах с диаризацией используется минимум 2 секунды."
        )
        self.vad_silence_spin = QSpinBox()
        self.vad_silence_spin.setRange(700, 3000)
        self.vad_silence_spin.setSingleStep(100)
        self.vad_silence_spin.setValue(700)
        self.vad_silence_spin.setSuffix(" мс пауза")
        self.vad_silence_spin.setMaximumWidth(120)
        self.vad_silence_spin.setToolTip(
            "Сколько тишины в конце фразы нужно, чтобы отправить её на распознавание. "
            "Меньше значение — текст появляется быстрее, но фраза может разделиться; "
            "больше значение лучше объединяет речь, но увеличивает задержку. Обычно подходит 700-1200 мс."
        )
        self.combo_segment_mode = QComboBox()
        self.combo_segment_mode.addItem("Паузы", "pause")
        self.combo_segment_mode.addItem("Длительность", "duration")
        self.combo_segment_mode.addItem("Смысл", "semantic")
        self.combo_segment_mode.addItem("Спикер", "speaker")
        self.combo_segment_mode.setMinimumContentsLength(10)
        self.combo_segment_mode.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.combo_segment_mode.setToolTip(
            "Паузы — естественные границы (рекомендуется). Длительность — фиксированные интервалы. "
            "Смысловые фразы объединяют распознанные слова до пунктуации. Смена спикера — экспериментальный режим, "
            "нужна модель эмбеддингов и несколько секунд на подтверждение."
        )
        self.combo_diarization = QComboBox()
        self.combo_diarization.addItem("По профилям", DIARIZATION_PROFILES)
        self.combo_diarization.addItem(
            "Автоматическая", DIARIZATION_AUTOMATIC
        )
        self.combo_diarization.addItem(
            "Отключена · быстрый текст", DIARIZATION_DISABLED
        )
        saved_diarization_mode = str(
            self.settings.value("diarization/mode", DIARIZATION_PROFILES)
        )
        saved_diarization_index = self.combo_diarization.findData(
            saved_diarization_mode
        )
        self.combo_diarization.setCurrentIndex(
            saved_diarization_index
            if saved_diarization_index >= 0
            else self.combo_diarization.findData(DIARIZATION_PROFILES)
        )
        self.combo_diarization.setToolTip(
            "По профилям — сопоставлять голоса с сохранёнными профилями и автоматически "
            "разделять незнакомые голоса. Автоматическая — создавать номера спикеров "
            "заново для этой сессии, не загружая сохранённые профили. Имена можно менять "
            "кнопкой «Спикер» прямо во время записи. Чтобы сменить режим после загрузки "
            "моделей, сначала выгрузите их. Быстрый режим отключает анализ голосов: текст "
            "выводится короткими блоками, но имя говорящего по голосу не определяется."
        )
        self.combo_diarization.setMinimumContentsLength(20)
        self.combo_diarization.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.combo_diarization.setMaximumWidth(205)
        self.combo_diarization.currentIndexChanged.connect(
            self.on_diarization_mode_changed
        )
        self.combo_audio_source = QComboBox()
        self.combo_audio_source.setMinimumContentsLength(15)
        self.combo_audio_source.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.combo_audio_source.setMaximumWidth(260)
        self.combo_audio_source.view().setMinimumWidth(440)
        self.combo_audio_source.setToolTip(
            "Выберите микрофон, другой аудиовход или системный звук с устройства "
            "вывода. Для системного звука используется WASAPI loopback."
        )
        self.combo_audio_source.currentIndexChanged.connect(
            self.on_audio_source_changed
        )
        self.btn_refresh_audio_sources = QPushButton("↻")
        self.btn_refresh_audio_sources.setMaximumWidth(32)
        self.btn_refresh_audio_sources.setToolTip(
            "Повторно найти аудиоустройства, подключённые к компьютеру."
        )
        self.btn_refresh_audio_sources.clicked.connect(
            self.refresh_audio_sources
        )
        self.refresh_audio_sources()
        mic_widget = QWidget()
        mic_widget.setMaximumWidth(165)
        mic_layout = QVBoxLayout(mic_widget)
        mic_layout.setContentsMargins(0, 0, 0, 0)
        mic_heading = QHBoxLayout()
        mic_heading.addWidget(QLabel("Микрофон:"))
        self.mic_sensitivity_label = QLabel()
        mic_heading.addWidget(self.mic_sensitivity_label)
        mic_layout.addLayout(mic_heading)
        self.mic_sensitivity_slider = QSlider(Qt.Orientation.Horizontal)
        self.mic_sensitivity_slider.setRange(1, 100)
        self.mic_sensitivity_slider.setValue(int(self.settings.value("audio/sensitivity", 50)))
        self.mic_sensitivity_slider.setToolTip(
            "Программный порог обнаружения речи. Выше чувствительность — лучше слышна тихая речь, "
            "но выше риск реакции на фоновый шум. Не меняет аппаратное усиление."
        )
        self.mic_sensitivity_slider.valueChanged.connect(self.on_mic_sensitivity_changed)
        mic_layout.addWidget(self.mic_sensitivity_slider)
        mic_layout.addWidget(self.save_audio_checkbox)

        self.combo_gguf = QComboBox()
        self.combo_gguf.setMinimumContentsLength(10)
        self.combo_gguf.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.combo_gguf.setMaximumWidth(150)
        self.combo_gguf.setToolTip(
            "Выберите локальную GGUF-модель LLM. Она не отправляет стенограмму в интернет. "
            "Для Qwen3 приложение автоматически добавляет /no_think к запросам."
        )
        self.combo_gguf.currentIndexChanged.connect(self.on_gguf_model_changed)
        self.btn_browse = QPushButton("📁 Обзор...")
        self.btn_browse.setToolTip("Найти локальную GGUF-модель на диске.")
        self.btn_browse.clicked.connect(self.browse_gguf)

        self.combo_interval = QComboBox()
        self.combo_interval.addItems(["Вручную", "Каждые 5 мин", "Каждые 10 мин"])
        self.combo_interval.setMinimumContentsLength(10)
        self.combo_interval.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.combo_interval.setMaximumWidth(145)
        self.combo_interval.setToolTip("Как часто автоматически обновлять протокол по текущей стенограмме.")
        self.combo_interval.currentIndexChanged.connect(self.on_interval_changed)
        self.combo_llm_backend = QComboBox()
        self.combo_llm_backend.addItem("Локальная", "local")
        self.combo_llm_backend.addItem("API", "api")
        self.combo_llm_backend.setMinimumContentsLength(9)
        self.combo_llm_backend.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.combo_llm_backend.setToolTip(
            "Выберите локальную GGUF-модель или удалённый OpenAI-совместимый API. "
            "В API-режиме стенограмма передаётся выбранному провайдеру."
        )
        backend_index = self.combo_llm_backend.findData(self.settings.value("llm/backend", "local"))
        self.combo_llm_backend.setCurrentIndex(max(0, backend_index))
        self.combo_llm_backend.currentIndexChanged.connect(self.on_llm_backend_changed)
        self.lbl_gguf = QLabel("Модель:")

        control_row.addWidget(self.btn_load_models)
        control_row.addWidget(self.btn_start)
        control_row.addWidget(self.btn_stop)
        control_row.addWidget(self.btn_pause)
        control_row.addWidget(QLabel("ASR:"))
        control_row.addWidget(self.combo_whisper)
        control_row.addWidget(self.combo_segment_mode)
        control_row.addWidget(QLabel("Спикеры:"))
        control_row.addWidget(self.combo_diarization)
        control_row.addWidget(QLabel("Протокол:"))
        control_row.addWidget(self.combo_interval)
        top_layout.addLayout(control_row)
        audio_row.addWidget(QLabel("Источник звука:"))
        audio_row.addWidget(self.combo_audio_source)
        audio_row.addWidget(self.btn_refresh_audio_sources)
        audio_row.addWidget(self.chunk_seconds_spin)
        audio_row.addWidget(self.vad_silence_spin)
        audio_row.addWidget(mic_widget)
        audio_row.addWidget(QLabel("LLM:"))
        audio_row.addWidget(self.combo_llm_backend)
        audio_row.addWidget(self.lbl_gguf)
        audio_row.addWidget(self.combo_gguf, 1)
        audio_row.addWidget(self.btn_browse)
        audio_row.addWidget(self.btn_load_llm)
        top_layout.addLayout(audio_row)

        self.llm_api_group = QGroupBox("Подключение OpenAI-совместимого API")
        api_layout = QHBoxLayout(self.llm_api_group)
        self.api_endpoint_edit = QLineEdit(
            str(self.settings.value("llm/api_endpoint", "https://api.openai.com/v1"))
        )
        self.api_endpoint_edit.setPlaceholderText("https://api.openai.com/v1")
        self.api_endpoint_edit.setToolTip(
            "Базовый URL OpenAI-совместимого API; если путь не указан, приложение добавит /v1. "
            "Для публичного сервера требуется HTTPS. "
            "HTTP разрешён для localhost и частных IP локальной сети (10.x.x.x, 172.16-31.x.x, "
            "192.168.x.x), но соединение не шифруется: используйте только доверенную сеть."
        )
        self.api_model_edit = QLineEdit(
            str(self.settings.value("llm/api_model", "gpt-4o-mini"))
        )
        self.api_model_edit.setPlaceholderText("Имя модели провайдера")
        self.api_model_edit.setToolTip("Имя модели, доступное для выбранного API-аккаунта.")
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("Ключ хранится в Windows Credential Manager")
        self.api_key_edit.setToolTip(
            "API-ключ. Кнопка сохранения поместит его в Windows Credential Manager для текущего пользователя."
        )
        self.btn_save_api_key = QPushButton("Сохранить ключ")
        self.btn_save_api_key.setToolTip("Сохранить API-ключ в защищённое хранилище Windows Credential Manager.")
        self.btn_save_api_key.clicked.connect(self.save_api_key)
        self.llm_api_group.setToolTip(
            "В API-режиме текст стенограммы и запросы протокола отправляются на указанный сервер. "
            "Для HTTP в локальной сети трафик, включая стенограмму и API-ключ, не шифруется. "
            "Используйте только доверенную сеть или настройте HTTPS. Ключ хранится в Windows Credential Manager."
        )
        api_layout.addWidget(self.api_endpoint_edit, 2)
        api_layout.addWidget(self.api_model_edit, 1)
        api_layout.addWidget(self.api_key_edit, 2)
        api_layout.addWidget(self.btn_save_api_key)
        self.llm_api_group.setVisible(self.combo_llm_backend.currentData() == "api")
        self.api_endpoint_edit.editingFinished.connect(
            lambda: self.settings.setValue("llm/api_endpoint", self.api_endpoint_edit.text().strip())
        )
        self.api_model_edit.editingFinished.connect(
            lambda: self.settings.setValue("llm/api_model", self.api_model_edit.text().strip())
        )

        # 2. Interactive UI Action Buttons
        btn_group = QGroupBox("Панель действий и экспорта")
        btn_group.setMinimumWidth(0)
        btn_group.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        btn_layout = QGridLayout(btn_group)

        self.btn_force_llm = QPushButton("🔄 Протокол")
        self.btn_force_llm.setStyleSheet("background-color: #e65100; color: white; font-weight: bold; padding: 6px;")
        self.btn_force_llm.setToolTip(
            "Сформировать протокол в выбранной форме. Перед генерацией вычитка "
            "выполняется только если включён флажок «Проверять перед протоколом»."
        )
        self.btn_force_llm.clicked.connect(self.force_llm_generation)

        self.btn_save_version = QPushButton("📑 Версия")
        self.btn_save_version.setToolTip("Добавить текущий протокол в список версий и сохранить его снимок.")
        self.btn_save_version.clicked.connect(self.save_protocol_version)

        self.btn_rename_speaker = QPushButton("👤 Спикер")
        self.btn_rename_speaker.setToolTip(
            "Назначить имя обнаруженному спикеру. Можно переименовывать прямо во время "
            "записи; при доступной модели голосовой профиль будет сохранён."
        )
        self.btn_rename_speaker.clicked.connect(self.rename_speaker_dialog)

        self.btn_manage_speakers = QPushButton("👥 Профили")
        self.btn_manage_speakers.setToolTip(
            "Редактировать, импортировать и экспортировать сохранённые профили голосов. "
            "Профиль создаётся при переименовании обнаруженного спикера."
        )
        self.btn_manage_speakers.clicked.connect(self.manage_speaker_profiles)

        self.btn_agenda = QPushButton(
            "🗂 Повестка ✓" if self.agenda_text.strip() else "🗂 Повестка"
        )
        self.btn_agenda.setToolTip(
            "Загрузить или написать темы и докладчиков совещания и сохранить частые повестки "
            "в локальную библиотеку. LLM получит этот контекст при проверке и создании протокола; "
            "в режиме API повестка отправляется выбранному провайдеру вместе со стенограммой."
        )
        self.btn_agenda.clicked.connect(self.manage_agenda)

        self.btn_import_audio = QPushButton("🎧 Аудио")
        self.btn_import_audio.setStyleSheet("background-color: #00838f; color: white; font-weight: bold; padding: 6px;")
        self.btn_import_audio.setToolTip("Распознать ранее записанный аудиофайл локально выбранной ASR-моделью.")
        self.btn_import_audio.clicked.connect(self.import_audio_file)

        self.btn_restore = QPushButton("↩ Восстановить")
        self.btn_restore.setToolTip("Восстановить последний автоматически сохранённый протокол, стенограмму и карту.")
        self.btn_restore.clicked.connect(self.restore_recovery_session)

        self.btn_export_pdf = QPushButton("📄 PDF-Отчет")
        self.btn_export_pdf.setStyleSheet("background-color: #1565c0; color: white; font-weight: bold; padding: 6px;")
        self.btn_export_pdf.setToolTip("Сохранить PDF с протоколом, временем речи, диаграммой и стенограммой.")
        self.btn_export_pdf.clicked.connect(self.export_pdf_report)

        self.btn_export_docx = QPushButton("📘 DOCX-Отчет")
        self.btn_export_docx.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold; padding: 6px;")
        self.btn_export_docx.setToolTip("Сохранить Word-документ с протоколом, полной стенограммой и эфирным временем.")
        self.btn_export_docx.clicked.connect(self.export_docx_report)

        btn_layout.setHorizontalSpacing(8)
        btn_layout.setVerticalSpacing(6)
        action_buttons = (
            self.btn_force_llm,
            self.btn_save_version,
            self.btn_rename_speaker,
            self.btn_manage_speakers,
            self.btn_agenda,
            self.btn_import_audio,
            self.btn_restore,
            self.btn_export_pdf,
            self.btn_export_docx,
        )
        for index, button in enumerate(action_buttons):
            button.setMinimumWidth(0)
            button.setMaximumHeight(36)
            button.setStyleSheet(button.styleSheet() + " QPushButton { padding: 3px; }")
            button.setSizePolicy(
                QSizePolicy.Policy.Ignored,
                QSizePolicy.Policy.Preferred,
            )
            btn_layout.addWidget(button, index // 5, index % 5)
            btn_layout.setColumnStretch(index % 5, 1)
        self.update_diarization_controls()

        action_status_layout.addWidget(btn_group)

        progress_group = QGroupBox("Состояние обработки")
        progress_layout = QGridLayout(progress_group)
        progress_layout.setContentsMargins(7, 5, 7, 5)
        progress_layout.setHorizontalSpacing(12)
        progress_layout.setVerticalSpacing(2)
        self.recording_status = QLabel("Запись: остановлена")
        recording_row = QHBoxLayout()
        recording_row.setContentsMargins(0, 0, 0, 0)
        recording_row.setSpacing(8)
        recording_row.addWidget(self.recording_status, 1)
        self.recording_progress = QProgressBar()
        self.recording_progress.setRange(0, 100)
        self.recording_progress.setValue(0)
        self.recording_progress.setOrientation(Qt.Orientation.Vertical)
        self.recording_progress.setTextVisible(False)
        self.recording_progress.setFixedSize(10, 30)
        self.recording_progress.setToolTip("Уровень микрофона")
        recording_row.addWidget(self.recording_progress, 0, Qt.AlignmentFlag.AlignRight)
        self.asr_load_status = QLabel("Модель распознавания: не загружена")
        self.asr_load_progress = QProgressBar()
        self.asr_load_progress.setRange(0, 100)
        self.asr_load_progress.setValue(0)
        self.asr_load_progress.setFormat("Загрузка ASR: %p%")
        self.asr_status = QLabel("Распознавание: ожидание")
        self.asr_progress = QProgressBar()
        self.asr_progress.setRange(0, 100)
        self.asr_progress.setValue(0)
        self.protocol_status = QLabel("Протокол: ожидание")
        self.protocol_progress = QProgressBar()
        self.protocol_progress.setRange(0, 100)
        self.protocol_progress.setValue(0)
        self.protocol_load_status = QLabel("Модель протокола: не загружена")
        self.protocol_load_progress = QProgressBar()
        self.protocol_load_progress.setRange(0, 100)
        self.protocol_load_progress.setValue(0)
        self.protocol_load_progress.setFormat("Загрузка LLM: %p%")
        self.asr_load_progress.setToolTip("Загрузка выбранной модели распознавания речи.")
        self.asr_progress.setToolTip("Ход обработки текущего аудиофрагмента.")
        self.protocol_load_progress.setToolTip("Подготовка выбранной локальной LLM или API.")
        self.protocol_progress.setToolTip("Ход формирования протокола.")
        for progress_bar, color in (
            (self.asr_load_progress, "#26a69a"),
            (self.asr_progress, "#7e57c2"),
            (self.protocol_load_progress, "#ffb300"),
            (self.protocol_progress, "#ef6c00"),
        ):
            progress_bar.setStyleSheet(
                "QProgressBar { border: 1px solid #555; border-radius: 7px; "
                "background: #202124; height: 7px; }"
                f"QProgressBar::chunk {{ background-color: {color}; border-radius: 4px; }}"
            )
            progress_bar.setTextVisible(False)
            progress_bar.setFixedSize(68, 8)
        self.recording_progress.setStyleSheet(
            "QProgressBar { border: 1px solid #555; border-radius: 5px; "
            "background: #202124; }"
            "QProgressBar::chunk { background-color: #00bcd4; border-radius: 4px; }"
        )

        def add_progress_row(row, column, status_label, progress_bar):
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            row_layout.addWidget(status_label, 1)
            row_layout.addWidget(progress_bar, 0, Qt.AlignmentFlag.AlignRight)
            progress_layout.addWidget(row_widget, row, column)

        progress_layout.addLayout(recording_row, 0, 0)
        add_progress_row(0, 1, self.asr_load_status, self.asr_load_progress)
        add_progress_row(1, 0, self.asr_status, self.asr_progress)
        add_progress_row(1, 1, self.protocol_load_status, self.protocol_load_progress)
        add_progress_row(2, 0, self.protocol_status, self.protocol_progress)
        progress_layout.setColumnStretch(0, 1)
        progress_layout.setColumnStretch(1, 1)
        progress_group.setMaximumWidth(720)
        action_status_layout.addWidget(progress_group, 0, Qt.AlignmentFlag.AlignLeft)

        # 3. Main Splitter
        splitter = QSplitter(Qt.Horizontal)

        # Left Panel - Transcript & Speakers
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        transcript_header = QHBoxLayout()
        transcript_header.addWidget(QLabel("<b>Стенограмма (оперативное распознавание ASR):</b>"))
        transcript_header.addStretch()
        self.btn_review_transcript = QPushButton("Проверить текст")
        self.btn_review_transcript.setToolTip(
            "Проверить стенограмму с помощью LLM на очевидные ошибки распознавания "
            "и опечатки. Изменения показываются отдельно и применяются только после подтверждения."
        )
        self.btn_review_transcript.clicked.connect(self.request_transcript_review)
        transcript_header.addWidget(self.btn_review_transcript)
        self.btn_clear_transcript = QPushButton("Очистить стенограмму")
        self.btn_clear_transcript.setToolTip(
            "Удалить текущие стенограмму и протокол, сбросить статистику спикеров и карту. "
            "Перед удалением потребуется подтверждение; сохранённые версии протокола не удаляются."
        )
        self.btn_clear_transcript.clicked.connect(self.clear_transcript)
        transcript_header.addWidget(self.btn_clear_transcript)
        left_layout.addLayout(transcript_header)

        self.txt_transcript = QTextEdit()
        self.txt_transcript.setReadOnly(False)
        self.txt_transcript.setPlaceholderText("Здесь появится стенограмма. Текст можно редактировать прямо в этом поле.")
        self.txt_transcript.setToolTip("Полная распознанная речь с временными метками. Текст можно исправлять вручную.")
        self.txt_transcript.textChanged.connect(self.on_transcript_edited)
        left_layout.addWidget(self.txt_transcript)

        # Right Panel - Protocol
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        protocol_header_layout = QHBoxLayout()
        self.protocol_header = QLabel("<b>Протокол встречи (локальная LLM на CPU):</b>")
        protocol_header_layout.addWidget(self.protocol_header, 1)
        protocol_header_layout.addWidget(QLabel("Форма:"))
        self.combo_protocol_format = QComboBox()
        self.combo_protocol_format.addItem(
            "Текущая", PROTOCOL_FORMAT_CURRENT
        )
        self.combo_protocol_format.addItem(
            "По образцу", PROTOCOL_FORMAT_SAMPLE
        )
        self.combo_protocol_format.setToolTip(
            "Текущая — привычный протокол с решениями, задачами и рисками. "
            "По образцу — официальная форма из приложенного документа: шапка, "
            "председательствующий, присутствующие и вопросы римскими цифрами "
            "с блоками «Решения» и сроками поручений."
        )
        saved_protocol_format = str(
            self.settings.value(
                "protocol/format", PROTOCOL_FORMAT_CURRENT
            )
        )
        saved_protocol_format_index = self.combo_protocol_format.findData(
            saved_protocol_format
        )
        self.combo_protocol_format.setCurrentIndex(
            saved_protocol_format_index
            if saved_protocol_format_index >= 0
            else self.combo_protocol_format.findData(PROTOCOL_FORMAT_CURRENT)
        )
        self.combo_protocol_format.currentIndexChanged.connect(
            self.on_protocol_format_changed
        )
        protocol_header_layout.addWidget(self.combo_protocol_format)
        self.chk_review_before_protocol = QCheckBox("Проверять перед протоколом")
        self.chk_review_before_protocol.setChecked(
            self.settings.value("protocol/review_before_generation", False, type=bool)
        )
        self.chk_review_before_protocol.setToolTip(
            "Если включено, перед каждым протоколом LLM предложит вычитку стенограммы, "
            "а протокол будет сформирован после вашего подтверждения. Ручная кнопка "
            "«Проверить текст» доступна независимо от этой настройки."
        )
        self.chk_review_before_protocol.toggled.connect(
            lambda checked: self.settings.setValue(
                "protocol/review_before_generation", checked
            )
        )
        protocol_header_layout.addWidget(self.chk_review_before_protocol)
        right_layout.addLayout(protocol_header_layout)

        self.txt_protocol = QTextEdit()
        self.txt_protocol.setToolTip("Сформированный протокол. Текст можно править вручную перед экспортом.")
        right_layout.addWidget(self.txt_protocol)

        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setSizes([550, 600])

        self.workspace_tabs = QTabWidget()
        self.workspace_tabs.addTab(splitter, "Стенограмма и протокол")
        map_page = QWidget()
        map_layout = QVBoxLayout(map_page)
        map_controls = QHBoxLayout()
        map_controls.addWidget(QLabel("Стиль карты:"))
        self.map_style_combo = QComboBox()
        self.map_style_combo.addItems(["Деловой", "Цветной", "Монохромный"])
        self.map_style_combo.setToolTip("Выберите цветовую схему узлов карты.")
        map_controls.addWidget(self.map_style_combo)
        map_controls.addWidget(QLabel("Вид:"))
        self.map_layout_combo = QComboBox()
        self.map_layout_combo.addItems(["Иерархический", "Радиальный", "Свободный"])
        self.map_layout_combo.setToolTip("Выберите расположение узлов карты. В свободном виде карточки можно перетаскивать.")
        map_controls.addWidget(self.map_layout_combo)
        self.btn_map_zoom_out = QPushButton("−")
        self.btn_map_zoom_out.setToolTip("Уменьшить масштаб карты.")
        self.btn_map_zoom_out.clicked.connect(lambda: self.mind_map_view.zoom_by(1 / 1.15))
        map_controls.addWidget(self.btn_map_zoom_out)
        self.btn_map_zoom_in = QPushButton("+")
        self.btn_map_zoom_in.setToolTip("Увеличить масштаб карты.")
        self.btn_map_zoom_in.clicked.connect(lambda: self.mind_map_view.zoom_by(1.15))
        map_controls.addWidget(self.btn_map_zoom_in)
        self.btn_map_fit = QPushButton("Вписать")
        self.btn_map_fit.setToolTip("Подобрать масштаб и положение, чтобы вся карта помещалась в окне.")
        self.btn_map_fit.clicked.connect(lambda: self.mind_map_view.fit_graph())
        map_controls.addWidget(self.btn_map_fit)
        self.btn_update_map = QPushButton("🗺 Обновить карту")
        self.btn_update_map.setToolTip("Перерисовать карту по уже разобранным темам и решениям.")
        self.btn_update_map.clicked.connect(self.update_mind_map)
        map_controls.addWidget(self.btn_update_map)
        self.btn_analyze_map = QPushButton("✦ Анализ LLM")
        self.btn_analyze_map.setToolTip("Извлечь темы, решения, задачи, риски и связи из стенограммы и протокола")
        self.btn_analyze_map.clicked.connect(self.request_mind_map_analysis)
        map_controls.addWidget(self.btn_analyze_map)
        map_controls.addStretch()
        map_layout.addLayout(map_controls)
        self.mind_map_view = MindMapView()
        self.mind_map_view.setToolTip("Колесо мыши меняет масштаб; средняя кнопка мыши перемещает область карты.")
        map_layout.addWidget(self.mind_map_view)
        self.map_style_combo.currentTextChanged.connect(self.update_mind_map)
        self.map_layout_combo.currentTextChanged.connect(self.update_mind_map)
        self.workspace_tabs.addTab(map_page, "Mind Map")
        self.workspace_tabs.setTabToolTip(0, "Живая стенограмма, которую можно редактировать, и итоговый протокол.")
        self.workspace_tabs.setTabToolTip(1, "Карта тем, решений, задач, рисков и вопросов по стенограмме.")

        vertical_splitter.addWidget(top_group)
        vertical_splitter.addWidget(self.llm_api_group)
        vertical_splitter.addWidget(action_status_widget)
        vertical_splitter.addWidget(self.workspace_tabs)
        vertical_splitter.setSizes([130, 40, 210, 500])
        main_layout.addWidget(vertical_splitter)

        # Status Bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Lite 2 готова. Сначала загрузите ASR для записи или распознавания аудио.")
        self.on_mic_sensitivity_changed(self.mic_sensitivity_slider.value())
        self.on_llm_backend_changed(self.combo_llm_backend.currentIndex())

    def refresh_model_list(self):
        preferred_path = str(self.settings.value("llm/model_path", ""))
        self.combo_gguf.clear()
        models = find_gguf_models()
        if models:
            for m in models:
                self.combo_gguf.addItem(m, userData=m)
            gemma_filename = (
                "gemma-4-e2b-uncensored-hauhaucs-aggressive-q6_k_p.gguf"
            )
            selected_path = preferred_path if preferred_path in models else next(
                (
                    path for path in models
                    if os.path.basename(path).casefold() == gemma_filename
                ),
                models[0],
            )
            self.combo_gguf.setCurrentIndex(self.combo_gguf.findData(selected_path))
            self.settings.setValue("llm/model_path", selected_path)
        else:
            self.combo_gguf.addItem("Модели .gguf не найдены в папке ./models", userData=None)

    @Slot(int)
    def on_gguf_model_changed(self, _index):
        model_path = self.combo_gguf.currentData()
        if model_path:
            self.settings.setValue("llm/model_path", str(model_path))

    def browse_gguf(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выберите GGUF модель для CPU", "", "GGUF Models (*.gguf)")
        if path:
            self.combo_gguf.addItem(os.path.basename(path), userData=path)
            self.combo_gguf.setCurrentIndex(self.combo_gguf.count() - 1)

    def on_interval_changed(self, index):
        interval_map = {0: 0, 1: 300, 2: 600}
        sec = interval_map.get(index, 0)
        if self.llm_thread:
            self.llm_thread.set_interval(sec)
        mode_str = "По кнопке" if sec == 0 else f"Каждые {sec//60} мин"
        self.status_bar.showMessage(f"Режим обновления LLM изменен: {mode_str}")

    @Slot(int)
    def on_protocol_format_changed(self, _index):
        protocol_format = self.combo_protocol_format.currentData()
        if protocol_format:
            self.settings.setValue("protocol/format", protocol_format)

    @Slot(int)
    def on_asr_model_changed(self, _index):
        model_key = self.combo_whisper.currentData()
        if model_key:
            self.settings.setValue("asr/model", str(model_key))

    def refresh_audio_sources(self):
        current_source = (
            self.combo_audio_source.currentData()
            if hasattr(self, "combo_audio_source")
            else None
        )
        selected_id = (
            current_source["id"]
            if current_source
            else str(self.settings.value("audio/source", "default"))
        )
        sources = enumerate_audio_sources()
        self.combo_audio_source.blockSignals(True)
        try:
            self.combo_audio_source.clear()
            for source in sources:
                self.combo_audio_source.addItem(
                    source["label"], userData=source
                )
            selected_index = next((
                index
                for index in range(self.combo_audio_source.count())
                if self.combo_audio_source.itemData(index)["id"] == selected_id
            ), 0 if self.combo_audio_source.count() else -1)
            if selected_index < 0 and self.combo_audio_source.count():
                selected_index = 0
            self.combo_audio_source.setCurrentIndex(selected_index)
        finally:
            self.combo_audio_source.blockSignals(False)
        current_source = self.combo_audio_source.currentData()
        if current_source:
            self.settings.setValue("audio/source", current_source["id"])
        if hasattr(self, "status_bar"):
            self.status_bar.showMessage(
                f"Найдено аудиоустройств: {len(sources)}."
            )

    @Slot(int)
    def on_audio_source_changed(self, _index):
        source = self.combo_audio_source.currentData()
        if source:
            self.settings.setValue("audio/source", source["id"])

    @Slot(int)
    def on_diarization_mode_changed(self, _index):
        mode = self.combo_diarization.currentData()
        if mode in {
            DIARIZATION_PROFILES,
            DIARIZATION_AUTOMATIC,
            DIARIZATION_DISABLED,
        }:
            self.settings.setValue("diarization/mode", mode)
            self.update_diarization_controls()

    def update_diarization_controls(self):
        fast_mode = self.combo_diarization.currentData() == DIARIZATION_DISABLED
        diarization_enabled = not fast_mode
        self.btn_rename_speaker.setEnabled(diarization_enabled)
        self.btn_manage_speakers.setEnabled(diarization_enabled)
        self.btn_pause.setVisible(fast_mode)
        self.btn_pause.setEnabled(
            fast_mode
            and self.recording_active
            and not self._pause_pending
        )
        self.btn_pause.setText(
            "▶ Продолжить" if self.recording_paused else "⏸ Пауза"
        )
        if diarization_enabled:
            self.btn_rename_speaker.setToolTip(
                "Назначить имя обнаруженному спикеру. Можно переименовывать прямо во время "
                "записи; при доступной модели голосовой профиль будет сохранён."
            )
        else:
            self.btn_rename_speaker.setToolTip(
                "В быстром режиме голос не анализируется, поэтому спикера для переименования нет."
            )

    def manage_agenda(self):
        dialog = MeetingAgendaDialog(
            self.agenda_text, self.agenda_library, self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            save_meeting_agenda(
                dialog.result_agenda,
                dialog.result_library,
            )
        except (OSError, ValueError) as exc:
            QMessageBox.critical(
                self,
                "Ошибка сохранения повестки",
                f"Текущая повестка и библиотека не изменены.\n{exc}",
            )
            return
        self.agenda_text = dialog.result_agenda
        self.agenda_library = dialog.result_library
        self.btn_agenda.setText(
            "🗂 Повестка ✓" if self.agenda_text else "🗂 Повестка"
        )
        if self.llm_thread:
            self.llm_thread.update_agenda(self.agenda_text)
        self.status_bar.showMessage(
            "Повестка сохранена и будет использоваться при анализе LLM."
            if self.agenda_text
            else "Текущая повестка очищена."
        )

    @staticmethod
    def _voice_threshold_from_sensitivity(sensitivity):
        return 0.015 * (2 ** ((50 - sensitivity) / 50))

    @Slot(int)
    def on_mic_sensitivity_changed(self, sensitivity):
        threshold = self._voice_threshold_from_sensitivity(sensitivity)
        self.mic_sensitivity_label.setText(
            f"{sensitivity}% · порог {threshold:.4f}"
        )
        self.mic_sensitivity_slider.setToolTip(
            f"Чувствительность: {sensitivity}%. Порог обнаружения речи: {threshold:.4f}. "
            "Выше чувствительность — лучше слышна тихая речь, но выше риск реакции на шум. "
            "Настройка не меняет аппаратное усиление микрофона."
        )
        self.settings.setValue("audio/sensitivity", sensitivity)
        if self.asr_thread:
            self.asr_thread.set_voice_threshold(threshold)

    def on_llm_backend_changed(self, _index):
        backend = self.combo_llm_backend.currentData()
        is_api = backend == "api"
        self.llm_api_group.setVisible(is_api)
        self.lbl_gguf.setVisible(not is_api)
        self.combo_gguf.setVisible(not is_api)
        self.btn_browse.setVisible(not is_api)
        self.protocol_header.setText(
            "<b>Протокол встречи (OpenAI-совместимый API):</b>"
            if is_api
            else "<b>Протокол встречи (локальная LLM на CPU):</b>"
        )
        self.settings.setValue("llm/backend", backend)

    def save_api_key(self):
        secret = self.api_key_edit.text().strip()
        if not secret:
            QMessageBox.warning(self, "Пустой ключ", "Введите API-ключ перед сохранением.")
            return
        try:
            save_api_credential(secret)
            self.api_key_edit.setText(secret)
            self.status_bar.showMessage("API-ключ сохранён в Windows Credential Manager.")
            QMessageBox.information(
                self,
                "Ключ сохранён",
                "API-ключ сохранён в Windows Credential Manager для текущего пользователя.",
            )
        except OSError as exc:
            logger.exception("Не удалось сохранить API-ключ в Windows Credential Manager")
            QMessageBox.critical(self, "Ошибка сохранения ключа", str(exc))

    def toggle_asr_model(self):
        if self.asr_thread:
            if self._asr_loading:
                return
            self.unload_asr_model()
            return

        selected_asr = "gigaam-v3-e2e-rnnt"
        if sd is None:
            QMessageBox.critical(
                self,
                "Не установлены зависимости",
                "Для записи и распознавания нужен пакет sounddevice."
            )
            return
        self._asr_loading = True
        self._asr_loaded = False
        self.asr_load_progress.setValue(0)
        self.asr_load_status.setText("Модель распознавания: загружается...")
        self.btn_load_models.setText("⏳ ASR")
        self.btn_load_models.setEnabled(False)
        self.btn_start.setEnabled(False)
        self.combo_whisper.setEnabled(False)
        self.chunk_seconds_spin.setEnabled(False)
        self.vad_silence_spin.setEnabled(False)
        self.combo_segment_mode.setEnabled(False)
        self.combo_diarization.setEnabled(False)

        self.asr_thread = ASRWorkerThread(
            model_size=selected_asr,
            device="cpu",
            compute_type="int8",
            chunk_seconds=self.chunk_seconds_spin.value(),
            vad_silence_ms=self.vad_silence_spin.value(),
            voice_threshold=self._voice_threshold_from_sensitivity(
                self.mic_sensitivity_slider.value()
            ),
            segment_mode=self.combo_segment_mode.currentData(),
            speaker_profiles=self.speaker_profiles,
            diarization_mode=self.combo_diarization.currentData(),
        )
        self._asr_text_connected = False
        self.asr_thread.text_recognized.connect(self.on_text_recognized)
        self._asr_text_connected = True
        self.asr_thread.status_signal.connect(self.asr_status.setText)
        self.asr_thread.progress_signal.connect(self.asr_load_progress.setValue)
        self.asr_thread.processing_progress_signal.connect(self.asr_progress.setValue)
        self.asr_thread.model_ready.connect(self.on_asr_model_ready)
        self.asr_thread.model_failed.connect(self.on_asr_model_failed)
        self.asr_thread.speaker_profiles_updated.connect(
            self.on_speaker_profiles_updated
        )
        self.asr_thread.processing_finished.connect(self.on_asr_processing_finished)
        self.asr_thread.start()

    def toggle_llm_model(self):
        if self.llm_thread:
            if self._llm_loading:
                return
            if (
                self._review_in_progress
                or self._protocol_generation_active
                or self._mind_map_analysis_active
            ):
                QMessageBox.information(
                    self,
                    "LLM занята",
                    "Дождитесь завершения текущей проверки, протокола или анализа Mind Map.",
                )
                return
            self.unload_llm_model()
            return

        backend = self.combo_llm_backend.currentData()
        model_path = None
        api_key = ""
        if backend == "local":
            if Llama is None:
                QMessageBox.critical(
                    self,
                    "Не установлена зависимость",
                    "Для локальной LLM установите llama-cpp-python либо выберите API-режим.",
                )
                return
            model_path = self.combo_gguf.currentData()
            if not model_path or not os.path.exists(str(model_path)):
                QMessageBox.warning(self, "Ошибка", "Сначала выберите существующую GGUF-модель.")
                return
        else:
            if httpx is None:
                QMessageBox.critical(self, "Не установлена зависимость", "Для API-режима нужен пакет httpx.")
                return
            endpoint = self.api_endpoint_edit.text().strip().rstrip("/")
            parsed_url = urlparse(endpoint)
            if (
                not parsed_url.hostname
                or parsed_url.username
                or parsed_url.password
                or parsed_url.query
                or parsed_url.fragment
                or not (
                parsed_url.scheme == "https"
                or (
                    parsed_url.scheme == "http"
                    and is_local_network_host(parsed_url.hostname)
                )
                )
            ):
                QMessageBox.warning(
                    self,
                    "Некорректный URL",
                    "Для публичного API требуется HTTPS. HTTP разрешён только для localhost "
                    "или IP-адресов частной сети (10.x.x.x, 172.16-31.x.x, 192.168.x.x). "
                    "HTTP не шифрует стенограмму и API-ключ.",
                )
                return
            if not self.api_model_edit.text().strip():
                QMessageBox.warning(self, "Не указана модель", "Введите имя модели, доступной у API-провайдера.")
                return
            api_key = self.api_key_edit.text().strip()
            if not api_key:
                try:
                    api_key = load_api_credential()
                except OSError as exc:
                    QMessageBox.critical(self, "Ошибка Credential Manager", str(exc))
                    return
            if not api_key:
                QMessageBox.warning(
                    self,
                    "Не указан API-ключ",
                    "Введите API-ключ или сохраните его в Windows Credential Manager.",
                )
                return

        self._llm_loading = True
        self.combo_llm_backend.setEnabled(False)
        self.combo_gguf.setEnabled(False)
        self.btn_browse.setEnabled(False)
        self.api_endpoint_edit.setEnabled(False)
        self.api_model_edit.setEnabled(False)
        self.api_key_edit.setEnabled(False)
        self.btn_save_api_key.setEnabled(False)
        self._llm_loaded = False
        self.btn_load_llm.setText("⏳ LLM")
        self.btn_load_llm.setEnabled(False)
        self.protocol_load_progress.setValue(0)
        self.protocol_load_status.setText("Модель протокола: загружается...")

        interval_map = {0: 0, 1: 300, 2: 600}
        interval_sec = interval_map.get(self.combo_interval.currentIndex(), 0)
        self.llm_thread = LLMProtocolThread(
            model_path=model_path,
            interval_seconds=interval_sec,
            backend=backend,
            api_endpoint=self.api_endpoint_edit.text().strip(),
            api_model=self.api_model_edit.text().strip(),
            api_key=api_key,
        )
        self.llm_thread.update_agenda(self.agenda_text)
        self.llm_thread.status_signal.connect(self.status_bar.showMessage)
        self.llm_thread.status_signal.connect(self.protocol_status.setText)
        self.llm_thread.progress_signal.connect(self.protocol_load_progress.setValue)
        self.llm_thread.model_ready.connect(self.on_llm_model_ready)
        self.llm_thread.model_failed.connect(self.on_llm_model_failed)
        self.llm_thread.protocol_ready.connect(self.on_protocol_updated)
        self.llm_thread.mindmap_ready.connect(self.on_mind_map_analysis_ready)
        self.llm_thread.mindmap_analysis_finished.connect(
            self.on_mind_map_analysis_finished
        )
        self.llm_thread.transcript_review_ready.connect(self.on_transcript_review_ready)
        self.llm_thread.transcript_review_failed.connect(self.on_transcript_review_failed)
        self.llm_thread.protocol_due.connect(self.on_automatic_protocol_due)
        self.llm_thread.protocol_generation_finished.connect(
            self.on_protocol_generation_finished
        )
        self.llm_thread.start()

    def on_asr_model_ready(self):
        self._asr_loading = False
        self._asr_loaded = True
        self.asr_load_status.setText("Модель распознавания: загружена")
        self.check_models_ready()

    def on_llm_model_ready(self):
        self._llm_loading = False
        self._llm_loaded = True
        self.protocol_load_status.setText("Модель протокола: загружена")
        self.check_models_ready()
        pending_action = self._pending_llm_action
        self._pending_llm_action = None
        if pending_action == "protocol":
            self.force_llm_generation()
        elif pending_action == "review":
            self.request_transcript_review(auto_generate=self._pending_protocol_after_review)
        elif pending_action == "mindmap":
            self.request_mind_map_analysis()

    def on_asr_model_failed(self, message):
        self._asr_loading = False
        self._asr_loaded = False
        self.asr_load_status.setText(f"Ошибка загрузки ASR: {message}")
        if self.asr_thread:
            self.asr_thread.stop()
            self.asr_thread = None
        self._asr_text_connected = False
        self.combo_whisper.setEnabled(True)
        self.chunk_seconds_spin.setEnabled(True)
        self.vad_silence_spin.setEnabled(True)
        self.combo_segment_mode.setEnabled(True)
        self.combo_diarization.setEnabled(True)
        self.check_models_ready()
        self.status_bar.showMessage("Не удалось загрузить модель распознавания.")

    def on_llm_model_failed(self, message):
        self._llm_loading = False
        self._llm_loaded = False
        self.protocol_load_status.setText(f"Ошибка загрузки LLM: {message}")
        if self.llm_thread:
            self.llm_thread.stop()
            self.llm_thread = None
        self._pending_llm_action = None
        self.btn_load_llm.setEnabled(True)
        self.btn_load_llm.setText("⬆ LLM")
        self.combo_llm_backend.setEnabled(True)
        self.combo_gguf.setEnabled(True)
        self.btn_browse.setEnabled(True)
        self.api_endpoint_edit.setEnabled(True)
        self.api_model_edit.setEnabled(True)
        self.api_key_edit.setEnabled(True)
        self.btn_save_api_key.setEnabled(True)
        self.check_models_ready()
        self.status_bar.showMessage(f"Не удалось загрузить LLM: {message}")

    def check_models_ready(self):
        self.models_loaded = self._asr_loaded and self._llm_loaded
        self.btn_load_models.setText(
            "⏳ ASR" if self._asr_loading else "⏏ ASR" if self._asr_loaded else "⬆ ASR"
        )
        self.btn_load_models.setEnabled(
            not self._asr_loading and not self._asr_finishing
        )
        self.btn_load_llm.setText(
            "⏳ LLM" if self._llm_loading else "⏏ LLM" if self._llm_loaded else "⬆ LLM"
        )
        self.btn_load_llm.setEnabled(
            not self._llm_loading
            and not self._review_in_progress
            and not self._protocol_generation_active
            and not self._mind_map_analysis_active
        )
        self.btn_start.setEnabled(
            self._asr_loaded and not self.recording_active and not self._asr_finishing
        )

    def unload_asr_model(self):
        if self.recording_active:
            QMessageBox.information(
                self,
                "Запись выполняется",
                "Остановите запись и дождитесь завершения распознавания перед выгрузкой ASR.",
            )
            return
        if self._asr_finishing:
            self.status_bar.showMessage("Дождитесь завершения обработки очереди перед выгрузкой моделей.")
            return
        if self.asr_thread:
            self.asr_thread.stop()
            self.asr_thread = None
        self.models_loaded = False
        self._asr_loaded = False
        self._asr_loading = False
        self._asr_text_connected = False
        self.combo_whisper.setEnabled(True)
        self.chunk_seconds_spin.setEnabled(True)
        self.vad_silence_spin.setEnabled(True)
        self.combo_segment_mode.setEnabled(True)
        self.combo_diarization.setEnabled(True)
        self.asr_load_progress.setValue(0)
        self.asr_load_status.setText("Модель распознавания: выгружена")
        self.check_models_ready()
        self.status_bar.showMessage("ASR выгружена из памяти.")

    def unload_llm_model(self):
        if self.llm_thread:
            self.llm_thread.stop()
            self.llm_thread = None
        self._llm_loaded = False
        self._llm_loading = False
        self._pending_llm_action = None
        self.combo_llm_backend.setEnabled(True)
        self.combo_gguf.setEnabled(True)
        self.btn_browse.setEnabled(True)
        self.api_endpoint_edit.setEnabled(True)
        self.api_model_edit.setEnabled(True)
        self.api_key_edit.setEnabled(True)
        self.btn_save_api_key.setEnabled(True)
        self.protocol_load_progress.setValue(0)
        self.protocol_load_status.setText("Модель протокола: выгружена")
        self.check_models_ready()
        self.status_bar.showMessage("LLM выгружена из памяти.")

    def unload_models(self):
        self.unload_asr_model()
        self.unload_llm_model()

    def _connect_asr_signals(self):
        if not self.asr_thread:
            return
        if self._asr_text_connected:
            return
        self.asr_thread.text_recognized.connect(self.on_text_recognized)
        self._asr_text_connected = True

    def start_meeting(self):
        if self._asr_finishing or self.recording_active:
            QMessageBox.information(self, "Распознавание завершается", "Дождитесь обработки последнего фрагмента.")
            return
        if sd is None:
            QMessageBox.critical(
                self,
                "Не установлены зависимости",
                "Для записи нужен пакет sounddevice.\n"
                "Установите их командой из инструкции и перезапустите программу."
            )
            return

        if not self._asr_loaded or not self.asr_thread:
            QMessageBox.warning(self, "ASR не загружена", "Сначала загрузите модель распознавания (ASR).")
            return

        recording_path = None
        if self.save_audio_checkbox.isChecked():
            initial_directory = str(
                self.settings.value("audio/save_directory", os.path.expanduser("~"))
            )
            recording_path, _ = QFileDialog.getSaveFileName(
                self,
                "Сохранить аудиозапись совещания",
                os.path.join(initial_directory, f"Совещание_{time.strftime('%Y%m%d_%H%M%S')}.wav"),
                "WAV audio (*.wav)",
            )
            if not recording_path:
                self.status_bar.showMessage("Запись не начата: выбор файла аудио отменён.")
                return
            if not recording_path.lower().endswith(".wav"):
                recording_path += ".wav"
            self.settings.setValue("audio/save_directory", os.path.dirname(recording_path))

        self.full_transcript = []
        self.speaker_airtime = {}
        self.speaker_names = {}
        self.mind_map_graph = {"title": "Совещание", "nodes": [], "edges": []}
        self._reviewed_transcript = ""
        self._pending_protocol_after_review = False
        self.txt_transcript.clear()
        self.txt_protocol.clear()
        self.update_mind_map()

        audio_source = self.combo_audio_source.currentData()
        if not audio_source:
            QMessageBox.critical(
                self,
                "Нет источника аудио",
                "Обновите список аудиоустройств и выберите источник записи.",
            )
            return
        self.audio_thread = AudioRecorderThread(
            output_path=recording_path,
            audio_source=audio_source,
        )

        self.recording_paused = False
        self._pause_pending = False
        self.audio_thread.audio_chunk_ready.connect(
            self.asr_thread.add_audio,
            Qt.ConnectionType.DirectConnection,
        )
        self.audio_thread.pause_ready.connect(self.on_audio_pause_ready)
        self.audio_thread.level_ready.connect(self.on_microphone_level)
        self.audio_thread.error_signal.connect(self.on_recording_error)
        self._connect_asr_signals()

        self.asr_thread.start_processing()
        self._asr_job_kind = "microphone"
        logger.info("Live recording started, ASR processing enabled")
        self.audio_thread.start()

        self.recording_active = True
        self.update_diarization_controls()
        self.combo_audio_source.setEnabled(False)
        self.btn_refresh_audio_sources.setEnabled(False)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.save_audio_checkbox.setEnabled(False)
        self.btn_clear_transcript.setEnabled(False)
        self.btn_load_models.setEnabled(False)
        self.recording_status.setText(
            "Запись: выполняется, WAV сохраняется"
            if recording_path
            else "Запись: выполняется"
        )
        self.btn_start.setText("● Идёт запись")
        self.btn_stop.setText("⏹ Стоп")
        self.asr_status.setText("Распознавание: ожидание фрагмента")
        self.protocol_status.setText("Протокол: модель готова")
        self.status_bar.showMessage(
            f"Запись источника «{audio_source['label']}» и транскрибация на CPU..."
        )

    def toggle_recording_pause(self):
        if not self.recording_active or not self.audio_thread or not self.asr_thread:
            return
        if self._pause_pending:
            return
        if self.recording_paused:
            self.add_transcript_marker("Продолжение записи после паузы")
            self.asr_thread.start_processing()
            self._asr_job_kind = "microphone"
            self.audio_thread.set_paused(False)
            self.recording_paused = False
            self.btn_pause.setText("⏸ Пауза")
            self.btn_pause.setEnabled(True)
            self.recording_status.setText("Запись: выполняется")
            self.asr_status.setText("Распознавание: ожидание фрагмента")
            self.status_bar.showMessage(
                "Запись продолжена; стенограмма отмечена временной меткой."
            )
            logger.info("Recording resumed after pause")
            return

        self._pause_pending = True
        self.btn_pause.setEnabled(False)
        self.recording_status.setText(
            "Запись: пауза, завершается распознавание текущего фрагмента"
        )
        self.asr_status.setText("Распознавание остатка перед паузой...")
        self.status_bar.showMessage("Приостановка: обрабатывается поступивший аудиофрагмент.")
        self.audio_thread.set_paused(True)
        logger.info("Recording pause requested")

    @Slot()
    def on_audio_pause_ready(self):
        if (
            self._pause_pending
            and self.recording_active
            and self.asr_thread
        ):
            self.asr_thread.finish_input()

    def stop_meeting(self):
        if self.audio_thread: self.audio_thread.stop()
        finishing = bool(self.asr_thread and self.asr_thread.processing)
        self._pause_pending = False
        if finishing:
            self._asr_finishing = True
            self._asr_job_kind = "microphone"
            self.asr_thread.finish_input()
        logger.info("Live recording stopped; queued audio is being transcribed")

        self.audio_thread = None
        self.recording_active = False
        self.recording_paused = False
        if not finishing:
            self.combo_audio_source.setEnabled(True)
            self.btn_refresh_audio_sources.setEnabled(True)
        self.save_audio_checkbox.setEnabled(True)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("⏸ Пауза")
        self.btn_start.setText("▶ Запись")
        self.btn_stop.setText("⏹ Стоп")
        self.recording_status.setText("Запись: остановлена")
        self.recording_progress.setValue(0)
        self.asr_status.setText(
            "Распознавание последнего фрагмента..." if finishing else "Распознавание завершено"
        )
        self.protocol_status.setText("Протокол: готов к формированию")
        self.btn_clear_transcript.setEnabled(False)
        self.btn_load_models.setEnabled(finishing is False and self._asr_loaded)
        self.btn_clear_transcript.setEnabled(not finishing)
        self.status_bar.showMessage(
            "Запись остановлена. Обрабатываются накопленные аудиоданные."
            if finishing
            else "Запись остановлена."
        )
        self.update_mind_map()
        if not finishing:
            self.on_recording_asr_finished()

    @Slot()
    def on_recording_asr_finished(self):
        self._asr_finishing = False
        self._pause_pending = False
        self.recording_paused = False
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("⏸ Пауза")
        self.combo_audio_source.setEnabled(True)
        self.btn_refresh_audio_sources.setEnabled(True)
        self.asr_status.setText("Распознавание завершено")
        self.btn_clear_transcript.setEnabled(True)
        self.btn_load_models.setEnabled(self._asr_loaded)
        if self._asr_loaded:
            self.btn_start.setEnabled(True)
        self.status_bar.showMessage("Запись остановлена; накопленные аудиоданные обработаны.")

    def import_audio_file(self):
        if self._asr_finishing:
            QMessageBox.information(self, "Распознавание завершается", "Дождитесь обработки последнего фрагмента.")
            return
        if not self._asr_loaded or not self.asr_thread:
            QMessageBox.warning(self, "ASR не загружена", "Сначала загрузите модель распознавания (ASR).")
            return
        if av is None:
            QMessageBox.critical(self, "Нет поддержки аудио", "Не установлен PyAV.")
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите аудиозапись совещания",
            "",
            "Аудио (*.wav *.mp3 *.m4a *.aac *.ogg *.opus *.flac *.wma);;Все файлы (*.*)"
        )
        if not file_path:
            return
        if self.recording_active:
            self.stop_meeting()
            QMessageBox.information(
                self,
                "Запись остановлена",
                "Дождитесь завершения распознавания записи, затем повторно выберите аудиофайл.",
            )
            return

        self.full_transcript = []
        self.speaker_airtime = {}
        self.speaker_names = {}
        self._reviewed_transcript = ""
        self._pending_protocol_after_review = False
        self.txt_transcript.clear()
        self.txt_protocol.clear()
        self.recording_active = True
        self._asr_finishing = True
        self._asr_job_kind = "file"
        self.btn_start.setEnabled(False)
        self.btn_clear_transcript.setEnabled(False)
        self.btn_load_models.setEnabled(False)
        self.recording_status.setText(f"Импорт аудио: {os.path.basename(file_path)}")
        self.asr_status.setText("Распознавание аудиофайла...")
        self.protocol_status.setText("Протокол: готовится после распознавания")
        self.asr_thread.start_processing()

        self.audio_file_thread = AudioFileThread(file_path)
        self.audio_file_thread.audio_chunk_ready.connect(self.asr_thread.add_audio)
        self.audio_file_thread.progress_signal.connect(self.on_microphone_level)
        self.audio_file_thread.status_signal.connect(self.recording_status.setText)
        self.audio_file_thread.error_signal.connect(self.on_audio_file_error)
        self.audio_file_thread.finished_signal.connect(self.on_audio_file_finished)
        self.audio_file_thread.finished_signal.connect(self.asr_thread.finish_input)
        self._connect_asr_signals()
        self.audio_file_thread.start()
        logger.info("Начат импорт аудио: %s", file_path)

    @Slot()
    def on_audio_file_finished(self):
        if self.audio_file_thread:
            self.audio_file_thread = None
        self.recording_status.setText("Аудиофайл прочитан, завершается распознавание")

    def on_audio_processing_finished(self):
        self._asr_finishing = False
        self.recording_active = False
        self.save_audio_checkbox.setEnabled(True)
        self.btn_clear_transcript.setEnabled(True)
        self.btn_load_models.setEnabled(self._asr_loaded)
        if self._asr_loaded:
            self.btn_start.setEnabled(True)
        self.recording_status.setText("Импорт аудио завершен")
        self.asr_status.setText("Распознавание завершено")
        self.status_bar.showMessage("Аудио обработано. Можно сформировать протокол.")
        logger.info("Импорт аудио завершен")

    @Slot()
    def on_asr_processing_finished(self):
        if self._pause_pending and self.recording_active:
            self._pause_pending = False
            self.recording_paused = True
            self.btn_pause.setText("▶ Продолжить")
            self.btn_pause.setEnabled(True)
            self.recording_status.setText("Запись: пауза")
            self.asr_status.setText("Распознавание приостановлено")
            self.status_bar.showMessage(
                "Пауза. Нажмите «Продолжить», чтобы возобновить запись."
            )
            logger.info("Recording paused after current audio was transcribed")
            return
        if self._asr_job_kind == "file":
            self.on_audio_processing_finished()
        else:
            self.on_recording_asr_finished()
        self._asr_job_kind = None

    def clear_transcript(self):
        if self.recording_active or self._asr_finishing:
            QMessageBox.information(
                self,
                "Запись выполняется",
                "Сначала остановите запись и дождитесь завершения распознавания.",
            )
            return
        answer = QMessageBox.question(
            self,
            "Очистить стенограмму",
            "Удалить текущую стенограмму, протокол, статистику спикеров и карту? "
            "Сохранённые версии протокола и профили голосов останутся.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.full_transcript.clear()
        self.speaker_airtime.clear()
        self.speaker_names.clear()
        self._reviewed_transcript = ""
        self._pending_protocol_after_review = False
        self.mind_map_graph = {"title": "Совещание", "nodes": [], "edges": []}
        self._updating_transcript = True
        try:
            self.txt_transcript.clear()
            self.txt_protocol.clear()
        finally:
            self._updating_transcript = False
        if self.llm_thread:
            self.llm_thread.update_transcript("")
        self.recording_status.setText("Запись: остановлена")
        self.asr_status.setText("Распознавание: ожидание")
        self.protocol_status.setText("Протокол: ожидание")
        self.asr_progress.setValue(0)
        self.protocol_progress.setValue(0)
        self.update_mind_map()
        self.save_recovery_snapshot()
        self.status_bar.showMessage("Текущая стенограмма и протокол очищены.")

    @Slot(str)
    def on_audio_file_error(self, message):
        logger.error("Ошибка импорта аудио: %s", message)
        self.recording_status.setText(f"Ошибка аудио: {message}")
        self.status_bar.showMessage(f"Ошибка импорта аудио: {message}")
        if self.asr_thread:
            self.asr_thread.stop_processing()
        self.recording_active = False
        self._asr_finishing = False
        self._asr_job_kind = None
        self.btn_clear_transcript.setEnabled(True)
        self.btn_load_models.setEnabled(self._asr_loaded)
        if self._asr_loaded:
            self.btn_start.setEnabled(True)

    def save_recovery_snapshot(self):
        snapshot = {
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "transcript": self.full_transcript,
            "transcript_text": self.txt_transcript.toPlainText(),
            "protocol": self.txt_protocol.toPlainText(),
            "speaker_names": self.speaker_names,
            "speaker_airtime": self.speaker_airtime,
            "mind_map_graph": self.mind_map_graph,
        }
        target = os.path.join(RECOVERY_DIR, "session_latest.json")
        temporary = target + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as file:
                json.dump(snapshot, file, ensure_ascii=False, indent=2)
            os.replace(temporary, target)
        except Exception:
            logger.exception("Не удалось сохранить снимок восстановления")

    def restore_recovery_session(self):
        source = os.path.join(RECOVERY_DIR, "session_latest.json")
        if not os.path.exists(source):
            QMessageBox.information(self, "Восстановление", "Сохраненных сессий пока нет.")
            return
        try:
            with open(source, "r", encoding="utf-8") as file:
                snapshot = json.load(file)
            self.full_transcript = snapshot.get("transcript", [])
            self.speaker_names = snapshot.get("speaker_names", {})
            self.speaker_airtime = snapshot.get("speaker_airtime", {})
            self._reviewed_transcript = ""
            self.mind_map_graph = snapshot.get("mind_map_graph", {"title": "Совещание", "nodes": [], "edges": []})
            self._updating_transcript = True
            try:
                self.txt_transcript.setPlainText(snapshot.get("transcript_text", ""))
            finally:
                self._updating_transcript = False
            self.txt_protocol.setPlainText(snapshot.get("protocol", ""))
            self.on_transcript_edited()
            self.status_bar.showMessage(f"Сессия восстановлена: {snapshot.get('saved_at', 'дата неизвестна')}")
            logger.info("Сессия восстановлена из %s", source)
        except Exception as e:
            logger.exception("Ошибка восстановления сессии")
            QMessageBox.critical(self, "Ошибка восстановления", str(e))

    @Slot(int)
    def on_microphone_level(self, level):
        self.recording_progress.setValue(level)
        self.recording_progress.setToolTip(f"Уровень микрофона: {level}%")

    @Slot(str)
    def on_recording_error(self, message):
        self.recording_status.setText(f"Ошибка записи: {message}")
        self.status_bar.showMessage(f"Ошибка микрофона: {message}")
        if self.recording_active:
            self.stop_meeting()

    @Slot(str, str, float)
    def on_text_recognized(self, raw_speaker, text, duration):
        if self.asr_thread and not self.asr_thread.diarization_enabled:
            if (
                self.full_transcript
                and self.full_transcript[-1].get("fast_text")
                and not self.full_transcript[-1].get("marker")
            ):
                entry = self.full_transcript[-1]
                entry["text"] = f'{entry["text"].rstrip()} {text.strip()}'.strip()
                entry["duration"] += duration
            else:
                self.full_transcript.append({
                    "raw_speaker": "",
                    "display_speaker": "",
                    "text": text.strip(),
                    "duration": duration,
                    "timestamp": "",
                    "fast_text": True,
                })
            self._reviewed_transcript = ""
            self.render_transcript()
            self.update_mind_map()
            return

        display_speaker = self.speaker_names.get(raw_speaker, raw_speaker)
        timestamp = time.strftime("%H:%M:%S")

        self.full_transcript.append({
            "raw_speaker": raw_speaker,
            "display_speaker": display_speaker,
            "text": text,
            "duration": duration,
            "timestamp": timestamp
        })

        self.speaker_airtime[display_speaker] = self.speaker_airtime.get(display_speaker, 0) + duration
        self._updating_transcript = True
        try:
            current_text = self.txt_transcript.toPlainText().rstrip()
            new_line = f"[{timestamp}] {display_speaker}: {text}"
            self.txt_transcript.setPlainText(f"{current_text}\n{new_line}".strip())
            self.txt_transcript.moveCursor(self.txt_transcript.textCursor().MoveOperation.End)
        finally:
            self._updating_transcript = False
        self.on_transcript_edited()
        self.update_mind_map()

    def add_transcript_marker(self, text):
        timestamp = time.strftime("%H:%M:%S")
        self.full_transcript.append({
            "raw_speaker": "",
            "display_speaker": "",
            "text": text,
            "duration": 0.0,
            "timestamp": timestamp,
            "marker": True,
        })
        current_text = self.txt_transcript.toPlainText().rstrip()
        marker_line = f"--- [{timestamp}] {text} ---"
        self._updating_transcript = True
        try:
            self.txt_transcript.setPlainText(
                f"{current_text}\n{marker_line}".strip()
            )
            self.txt_transcript.moveCursor(
                self.txt_transcript.textCursor().MoveOperation.End
            )
        finally:
            self._updating_transcript = False
        self.on_transcript_edited()
        self.save_recovery_snapshot()

    def render_transcript(self):
        lines = []
        full_llm_text = []

        for item in self.full_transcript:
            if item.get("marker"):
                line = f"--- [{item['timestamp']}] {item['text']} ---"
                lines.append(line)
                full_llm_text.append(line)
                continue
            if item.get("fast_text"):
                if lines and not lines[-1].startswith("--- ["):
                    lines[-1] = f"{lines[-1].rstrip()} {item['text'].strip()}".strip()
                    full_llm_text[-1] = lines[-1]
                else:
                    lines.append(item["text"].strip())
                    full_llm_text.append(item["text"].strip())
                continue
            spk = self.speaker_names.get(item["raw_speaker"], item["display_speaker"])
            lines.append(f"[{item['timestamp']}] {spk}: {item['text']}")
            full_llm_text.append(f"[{item['timestamp']}] {spk}: {item['text']}")

        self._updating_transcript = True
        try:
            self.txt_transcript.setPlainText("\n".join(lines))
        finally:
            self._updating_transcript = False
        if self.llm_thread:
            self.llm_thread.update_transcript("\n".join(full_llm_text))

    def on_transcript_edited(self):
        if self._updating_transcript or not self.llm_thread:
            return
        self.llm_thread.update_transcript(self.txt_transcript.toPlainText())

    def update_mind_map(self):
        if not hasattr(self, "mind_map_view"):
            return
        self.mind_map_view.set_options(
            self.map_style_combo.currentText(),
            self.map_layout_combo.currentText()
        )
        self.mind_map_view.set_graph(self.mind_map_graph)

    def request_mind_map_analysis(self):
        if not self._llm_loaded or not self.llm_thread:
            self._pending_llm_action = "mindmap"
            self.toggle_llm_model()
            if not self.llm_thread:
                self._pending_llm_action = None
            return
        transcript_text = self.txt_transcript.toPlainText().strip()
        protocol_text = self.txt_protocol.toPlainText().strip()
        if not transcript_text:
            QMessageBox.information(self, "Нет стенограммы", "Запишите совещание или загрузите аудиофайл.")
            return
        self.llm_thread.update_transcript(transcript_text)
        self._mind_map_analysis_active = True
        self.btn_load_llm.setEnabled(False)
        self.protocol_status.setText("LLM анализирует контекст для Mind Map...")
        threading.Thread(
            target=self.llm_thread.analyze_mind_map,
            args=(protocol_text,),
            daemon=True
        ).start()

    @Slot()
    def on_mind_map_analysis_finished(self):
        self._mind_map_analysis_active = False
        self.check_models_ready()

    @Slot(str)
    def on_mind_map_analysis_ready(self, response_text):
        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
        try:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("Ответ модели не содержит JSON")
            graph = json.loads(cleaned[start:end + 1])
            if not isinstance(graph, dict):
                raise ValueError("Корнем Mind Map должен быть JSON-объект")
            nodes = graph.get("nodes", [])
            edges = graph.get("edges", [])
            if not isinstance(nodes, list) or not isinstance(edges, list):
                raise ValueError("В JSON должны быть списки nodes и edges")
            valid_nodes = []
            valid_ids = {"root"}
            for index, node in enumerate(nodes[:40]):
                if not isinstance(node, dict):
                    continue
                node_id = str(node.get("id") or f"node-{index}")
                if node_id == "root" or node_id in valid_ids:
                    logger.warning("Пропущен узел Mind Map с повторяющимся id: %s", node_id)
                    continue
                valid_ids.add(node_id)
                node_type = str(node.get("type") or "topic").lower()
                if node_type not in {
                    "topic", "decision", "task", "risk", "question", "agreement", "objection"
                }:
                    node_type = "topic"
                valid_nodes.append({
                    "id": node_id,
                    "label": str(node.get("label") or "Тема")[:100],
                    "type": node_type,
                    "details": str(node.get("details") or "")[:500],
                    "parent_id": str(node.get("parent_id") or "root"),
                })
            for node in valid_nodes:
                if node["parent_id"] not in valid_ids or node["parent_id"] == node["id"]:
                    node["parent_id"] = "root"
            valid_edges = [
                {
                    "source": str(edge.get("source", "root")),
                    "target": str(edge.get("target", "")),
                    "relation": str(edge.get("relation", ""))[:80],
                }
                for edge in edges
                if isinstance(edge, dict)
                and str(edge.get("source", "root")) in valid_ids
                and str(edge.get("target", "")) in valid_ids
            ]
            self.mind_map_graph = {
                "title": str(graph.get("title") or "Совещание")[:120],
                "nodes": valid_nodes,
                "edges": valid_edges,
            }
            self.update_mind_map()
            self.protocol_status.setText(f"Mind Map обновлена: {len(valid_nodes)} смысловых узлов")
            self.save_recovery_snapshot()
        except Exception as e:
            logger.exception("Некорректный ответ Mind Map: %s", response_text)
            self.protocol_status.setText(f"Не удалось разобрать Mind Map: {e}")

    @Slot(str)
    def on_protocol_updated(self, protocol_md):
        self.txt_protocol.setMarkdown(protocol_md)
        self.protocol_progress.setValue(100)
        self.update_mind_map()
        self.save_recovery_snapshot()

    def on_automatic_protocol_due(self):
        if self.recording_active or self._asr_finishing:
            self.status_bar.showMessage(
                "Автоматический протокол отложен до окончания записи и проверки стенограммы."
            )
            return
        if not self.txt_transcript.toPlainText().strip():
            self.status_bar.showMessage("Автоматический протокол пропущен: стенограмма пока пуста.")
            return
        self.force_llm_generation()

    def request_transcript_review(self, auto_generate=False):
        if self._protocol_generation_active:
            message = "Дождитесь завершения формирования протокола."
            if auto_generate:
                self.status_bar.showMessage(message)
            else:
                QMessageBox.information(self, "Протокол формируется", message)
            return
        if self._review_in_progress:
            self._pending_protocol_after_review |= auto_generate
            return
        if self.recording_active or self._asr_finishing:
            message = "Остановите запись и дождитесь обработки аудио перед проверкой стенограммы."
            if auto_generate:
                self.status_bar.showMessage(message)
            else:
                QMessageBox.information(self, "Запись выполняется", message)
            return
        source_text = self.txt_transcript.toPlainText().strip()
        if not source_text:
            QMessageBox.information(self, "Нет стенограммы", "Сначала запишите совещание или загрузите аудио.")
            return

        if not self._llm_loaded or not self.llm_thread:
            self._pending_protocol_after_review = auto_generate
            self._pending_llm_action = "review"
            self.toggle_llm_model()
            if not self.llm_thread:
                self._pending_llm_action = None
                self._pending_protocol_after_review = False
            return

        self._review_source_text = source_text
        self._pending_protocol_after_review = auto_generate
        self._review_in_progress = True
        self.btn_review_transcript.setEnabled(False)
        self.btn_force_llm.setEnabled(False)
        self.btn_clear_transcript.setEnabled(False)
        self.btn_load_llm.setEnabled(False)
        self.txt_transcript.setEnabled(False)
        self.btn_start.setEnabled(False)
        self.protocol_status.setText("Проверка стенограммы...")
        threading.Thread(
            target=self.llm_thread.review_transcript,
            args=(source_text,),
            daemon=True,
        ).start()

    @Slot(str)
    def on_transcript_review_ready(self, reviewed_text):
        source_text = self._review_source_text
        if self.txt_transcript.toPlainText().strip() != source_text:
            self.on_transcript_review_failed(
                "Стенограмма изменилась во время проверки. Запустите проверку ещё раз."
            )
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Проверка стенограммы")
        dialog.resize(1000, 620)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(
            "Сравните исходный текст с предложением LLM. Модель может ошибаться; "
            "исправления попадут в стенограмму только после вашего подтверждения."
        ))
        editors = QHBoxLayout()
        original_column = QVBoxLayout()
        original_column.addWidget(QLabel("Исходная стенограмма"))
        original_edit = QTextEdit()
        original_edit.setReadOnly(True)
        original_edit.setPlainText(source_text)
        original_column.addWidget(original_edit)
        proposed_column = QVBoxLayout()
        proposed_column.addWidget(QLabel("Предложение LLM (можно редактировать)"))
        proposed_edit = QTextEdit()
        proposed_edit.setPlainText(reviewed_text)
        proposed_column.addWidget(proposed_edit)
        editors.addLayout(original_column, 1)
        editors.addLayout(proposed_column, 1)
        layout.addLayout(editors)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Принять предложенный текст")
        keep_original = buttons.addButton(
            "Оставить исходник", QDialogButtonBox.ButtonRole.ActionRole
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        keep_original.clicked.connect(lambda: dialog.done(2))
        layout.addWidget(buttons)

        result = dialog.exec()
        if result == QDialog.DialogCode.Accepted:
            accepted_text = proposed_edit.toPlainText().strip()
        elif result == 2:
            accepted_text = source_text
        else:
            accepted_text = ""

        should_generate = self._pending_protocol_after_review and bool(accepted_text)
        self._review_in_progress = False
        self._pending_protocol_after_review = False
        self.btn_review_transcript.setEnabled(True)
        self.btn_force_llm.setEnabled(True)
        self.btn_clear_transcript.setEnabled(True)
        self.btn_load_llm.setEnabled(self._llm_loaded)
        self.txt_transcript.setEnabled(True)
        self.check_models_ready()

        if not accepted_text:
            self._reviewed_transcript = ""
            self.protocol_status.setText("Проверка отменена; стенограмма не изменена.")
            return

        self._updating_transcript = True
        try:
            self.txt_transcript.setPlainText(accepted_text)
        finally:
            self._updating_transcript = False
        self._reviewed_transcript = accepted_text
        self.on_transcript_edited()
        self.save_recovery_snapshot()
        self.protocol_status.setText("Стенограмма проверена и подтверждена.")
        if should_generate:
            self._start_protocol_generation()

    @Slot(str)
    def on_transcript_review_failed(self, message):
        self._review_in_progress = False
        self._pending_protocol_after_review = False
        self.btn_review_transcript.setEnabled(True)
        self.btn_force_llm.setEnabled(True)
        self.btn_clear_transcript.setEnabled(True)
        self.btn_load_llm.setEnabled(self._llm_loaded)
        self.txt_transcript.setEnabled(True)
        self.check_models_ready()
        self.protocol_status.setText(f"Ошибка проверки стенограммы: {message}")
        QMessageBox.critical(self, "Проверка стенограммы не выполнена", message)

    def force_llm_generation(self):
        if self._review_in_progress:
            self._pending_protocol_after_review = True
            return
        if self.recording_active or self._asr_finishing:
            QMessageBox.information(
                self,
                "Запись выполняется",
                "Сначала остановите запись и дождитесь завершения распознавания.",
            )
            return
        current_text = self.txt_transcript.toPlainText().strip()
        if not current_text:
            QMessageBox.information(self, "Пустая стенограмма", "Нечего включать в протокол.")
            return
        if not self._llm_loaded or not self.llm_thread:
            self._pending_llm_action = "protocol"
            self.toggle_llm_model()
            if not self.llm_thread:
                self._pending_llm_action = None
            return
        if not self.chk_review_before_protocol.isChecked():
            self._reviewed_transcript = current_text
            self._start_protocol_generation()
            return
        if self._reviewed_transcript != current_text:
            self.request_transcript_review(auto_generate=True)
            return
        self._start_protocol_generation()

    def _start_protocol_generation(self):
        if not self.llm_thread or self._protocol_generation_active:
            return
        protocol_format = self.combo_protocol_format.currentData()
        current_text = self.txt_transcript.toPlainText().strip()
        if (
            self.chk_review_before_protocol.isChecked()
            and self._reviewed_transcript != current_text
        ):
            self.request_transcript_review(auto_generate=True)
            return
        if not self.chk_review_before_protocol.isChecked():
            self._reviewed_transcript = current_text
        self.llm_thread.protocol_format = protocol_format
        current_datetime = time.strftime("%d.%m.%Y %H:%M:%S")
        self.txt_protocol.setPlainText(
            (
                f"# Протокол совещания\n\n**Дата и время:** {current_datetime}\n\n"
                if protocol_format == PROTOCOL_FORMAT_CURRENT
                else f"П Р О Т О К О Л\n\n{current_datetime}    № ______\n\n"
            )
            + (
                "Формируется по проверенной стенограмме..."
                if self.chk_review_before_protocol.isChecked()
                else "Формируется по текущей стенограмме..."
            )
        )
        self.llm_thread.update_transcript(self._reviewed_transcript)
        self._protocol_generation_active = True
        self.btn_force_llm.setEnabled(False)
        self.btn_load_llm.setEnabled(False)
        self.combo_protocol_format.setEnabled(False)
        self.protocol_status.setText(
            (
                "Протокол по образцу: формирование по проверенной стенограмме..."
                if protocol_format == PROTOCOL_FORMAT_SAMPLE
                else "Протокол: формирование по проверенной стенограмме..."
            )
            if self.chk_review_before_protocol.isChecked()
            else (
                "Протокол по образцу: формирование по текущей стенограмме..."
                if protocol_format == PROTOCOL_FORMAT_SAMPLE
                else "Протокол: формирование по текущей стенограмме..."
            )
        )
        self.protocol_progress.setValue(25)
        threading.Thread(target=self.llm_thread.generate_now, daemon=True).start()

    @Slot(bool)
    def on_protocol_generation_finished(self, succeeded):
        self._protocol_generation_active = False
        self.btn_force_llm.setEnabled(True)
        self.btn_load_llm.setEnabled(self._llm_loaded)
        self.combo_protocol_format.setEnabled(True)
        if not succeeded:
            self.protocol_progress.setValue(0)

    def save_protocol_version(self):
        current_md = self.txt_protocol.toPlainText()
        if not current_md.strip():
            QMessageBox.warning(self, "Предупреждение", "Протокол пока пуст.")
            return

        ts = time.strftime("%H:%M:%S")
        self.protocol_versions.append((ts, current_md))
        QMessageBox.information(
            self, "Сохранено",
            f"Версия протокола за {ts} успешно сохранена! (Всего версий: {len(self.protocol_versions)})"
        )

    def export_docx_report(self):
        if Document is None:
            QMessageBox.warning(
                self,
                "Нужна зависимость",
                "Для экспорта DOCX установите python-docx:\n"
                "python -m pip install python-docx"
            )
            return

        protocol_text = self.txt_protocol.toPlainText().strip()
        if not protocol_text and not self.full_transcript:
            QMessageBox.warning(self, "Предупреждение", "Нет данных для генерации DOCX-отчета.")
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить DOCX-отчет", "protocol_report.docx", "Word Documents (*.docx)"
        )
        if not file_path:
            return

        try:
            document = Document()
            document.add_heading("Протокол совещания", level=1)
            document.add_paragraph(f"Дата формирования: {time.strftime('%d.%m.%Y %H:%M')}")
            document.add_heading("Итоговый протокол", level=2)
            for line in protocol_text.splitlines():
                document.add_paragraph(line)

            document.add_heading("Полная стенограмма", level=2)
            for line in self.txt_transcript.toPlainText().splitlines():
                document.add_paragraph(line)

            document.add_heading("Эфирное время спикеров", level=2)
            table = document.add_table(rows=1, cols=3)
            table.style = "Table Grid"
            headers = table.rows[0].cells
            headers[0].text = "Спикер"
            headers[1].text = "Время речи"
            headers[2].text = "Доля эфира"
            total_seconds = sum(self.speaker_airtime.values())
            for speaker, seconds in self.speaker_airtime.items():
                row = table.add_row().cells
                row[0].text = speaker
                row[1].text = f"{int(seconds // 60)} мин {int(seconds % 60)} сек"
                share = (seconds / total_seconds * 100) if total_seconds else 0
                row[2].text = f"{share:.1f}%"

            document.save(file_path)
            QMessageBox.information(self, "Успех", f"DOCX-отчет сохранен:\n{file_path}")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка сохранения DOCX", str(e))

    def rename_speaker_dialog(self):
        if self._asr_finishing:
            QMessageBox.information(
                self,
                "Распознавание выполняется",
                "Дождитесь завершения обработки последнего фрагмента, "
                "чтобы безопасно изменить имя и профиль спикера.",
            )
            return
        speaker_aliases = {}
        for item in self.full_transcript:
            if item.get("marker"):
                continue
            raw_name = item["raw_speaker"]
            display_name = self.speaker_names.get(
                raw_name, item.get("display_speaker", raw_name)
            )
            speaker_aliases.setdefault(display_name, set()).add(raw_name)
        speakers = sorted(speaker_aliases, key=str.casefold)
        if not speakers:
            QMessageBox.information(self, "Информация", "Спикеры пока не обнаружены.")
            return

        target_display, ok = QInputDialog.getItem(
            self, "Переименовать спикера", "Выберите спикера:", speakers, 0, False
        )
        if ok and target_display:
            new_name, ok2 = QInputDialog.getText(
                self, "Новое имя", f"Введите имя для {target_display}:"
            )
            if ok2 and new_name.strip():
                new_name = new_name.strip()
                profile_saved = False
                aliases = speaker_aliases[target_display]
                if self.asr_thread:
                    try:
                        for current_name in [target_display, *sorted(aliases)]:
                            if self.asr_thread.embedder.rename_profile(
                                current_name, new_name
                            ):
                                profile_saved = True
                                break
                        if profile_saved:
                            profiles = self.asr_thread.embedder.export_profiles()
                            save_speaker_profiles(profiles)
                            self.speaker_profiles = profiles
                    except (OSError, ValueError) as exc:
                        logger.exception("Не удалось сохранить профиль спикера")
                        QMessageBox.critical(
                            self, "Ошибка профиля спикера", str(exc)
                        )
                        return
                for alias in aliases:
                    self.speaker_names[alias] = new_name
                updated_text = self.txt_transcript.toPlainText().replace(
                    f"] {target_display}:", f"] {new_name}:"
                )
                self._updating_transcript = True
                try:
                    self.txt_transcript.setPlainText(updated_text)
                finally:
                    self._updating_transcript = False
                for item in self.full_transcript:
                    if item["raw_speaker"] in aliases:
                        item["display_speaker"] = new_name
                self.speaker_airtime = {}
                for item in self.full_transcript:
                    if item.get("marker"):
                        continue
                    display_name = self.speaker_names.get(
                        item["raw_speaker"], item["display_speaker"]
                    )
                    self.speaker_airtime[display_name] = (
                        self.speaker_airtime.get(display_name, 0)
                        + item["duration"]
                    )
                self.on_transcript_edited()
                if profile_saved:
                    self.status_bar.showMessage(
                        f"Спикер «{new_name}» сохранён как профиль голоса."
                    )
                else:
                    self.status_bar.showMessage(
                        "Имя изменено. Эмбеддинг голоса недоступен, поэтому профиль не сохранён."
                    )

    @Slot(object)
    def on_speaker_profiles_updated(self, profiles):
        try:
            current_profiles = (
                self.asr_thread.embedder.export_profiles()
                if self.asr_thread
                else profiles
            )
            validated = normalize_speaker_profiles(current_profiles)
            if (
                self.asr_thread
                and self.asr_thread.diarization_mode == DIARIZATION_AUTOMATIC
                and not validated
            ):
                return
            if validated == self.speaker_profiles:
                return
            save_speaker_profiles(validated)
            self.speaker_profiles = validated
        except (OSError, ValueError) as exc:
            logger.exception("Не удалось сохранить обновлённые профили спикеров")
            self.status_bar.showMessage(f"Не удалось сохранить профили спикеров: {exc}")

    def manage_speaker_profiles(self):
        if self.recording_active or self._asr_finishing:
            QMessageBox.information(
                self,
                "Запись выполняется",
                "Дождитесь завершения обработки, прежде чем изменять профили голосов.",
            )
            return
        dialog = SpeakerProfilesDialog(self.speaker_profiles, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            save_speaker_profiles(dialog.result_profiles)
        except (OSError, ValueError) as exc:
            logger.exception("Не удалось сохранить профили спикеров")
            QMessageBox.critical(self, "Ошибка сохранения профилей", str(exc))
            return

        for old_name, new_name in dialog.renamed_profiles.items():
            for raw_name, display_name in list(self.speaker_names.items()):
                if display_name == old_name:
                    self.speaker_names[raw_name] = new_name
            self.speaker_names[old_name] = new_name
            self._updating_transcript = True
            try:
                self.txt_transcript.setPlainText(
                    self.txt_transcript.toPlainText().replace(
                        f"] {old_name}:", f"] {new_name}:"
                    )
                )
            finally:
                self._updating_transcript = False
            for item in self.full_transcript:
                if (
                    item["raw_speaker"] == old_name
                    or item["display_speaker"] == old_name
                ):
                    item["display_speaker"] = new_name
        self.speaker_profiles = dialog.result_profiles
        if self.asr_thread:
            self.asr_thread.embedder.replace_profiles(self.speaker_profiles)
        self.on_transcript_edited()
        self.save_recovery_snapshot()
        self.status_bar.showMessage(
            f"Профили спикеров сохранены: {len(self.speaker_profiles)}."
        )

    def generate_airtime_chart(self, output_img_path):
        if not self.speaker_airtime:
            return False

        labels = list(self.speaker_airtime.keys())
        sizes = list(self.speaker_airtime.values())
        if sum(sizes) == 0:
            return False

        fig, ax = plt.subplots(figsize=(6, 3.2), dpi=150)
        colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b']

        wedges, texts, autotexts = ax.pie(
            sizes,
            labels=labels,
            autopct='%1.1f%%',
            startangle=140,
            colors=colors[:len(labels)],
            textprops=dict(color="black", size=9)
        )
        plt.setp(autotexts, size=9, weight="bold", color="white")
        ax.set_title("Распределение эфирного времени спикеров", fontsize=11, fontweight='bold', pad=12)
        plt.tight_layout()
        plt.savefig(output_img_path, bbox_inches='tight', dpi=150)
        plt.close(fig)
        return True

    def export_pdf_report(self):
        protocol_content = self.txt_protocol.toHtml()
        if not protocol_content.strip() and not self.full_transcript:
            QMessageBox.warning(self, "Предупреждение", "Нет данных для генерации PDF-отчета.")
            return

        file_path, _ = QFileDialog.getSaveFileName(self, "Сохранить PDF-отчет", "protocol_report_cpu.pdf", "PDF Files (*.pdf)")
        if not file_path:
            return

        temp_dir = tempfile.gettempdir()
        chart_img_path = os.path.join(temp_dir, "meeting_airtime_chart.png")
        has_chart = self.generate_airtime_chart(chart_img_path)

        total_seconds = sum(self.speaker_airtime.values())
        stats_rows = []
        for spk, secs in self.speaker_airtime.items():
            pct = (secs / total_seconds * 100) if total_seconds > 0 else 0
            mins = int(secs // 60)
            s = int(secs % 60)
            stats_rows.append(f"<tr><td><b>{spk}</b></td><td>{mins} мин {s} сек</td><td>{pct:.1f}%</td></tr>")

        stats_table_html = f"""
        <table border="1" cellspacing="0" cellpadding="5" style="border-collapse: collapse; width: 100%;">
            <tr style="background-color: #f2f2f2;">
                <th>Спикер</th><th>Время речи</th><th>Доля эфира</th>
            </tr>
            {"".join(stats_rows)}
        </table>
        """

        chart_html = f'<div style="text-align: center; margin-top: 15px;"><img src="file:///{chart_img_path}" width="450"></div>' if has_chart else ""

        transcript_html_rows = [
            f"<p>{html.escape(line)}</p>"
            for line in self.txt_transcript.toPlainText().splitlines()
        ]

        html_document = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: sans-serif; font-size: 11pt; color: #333; margin: 20px; }}
                h1 {{ color: #1565c0; font-size: 18pt; text-align: center; border-bottom: 2px solid #1565c0; padding-bottom: 5px; }}
                h2 {{ color: #0d47a1; font-size: 14pt; margin-top: 20px; border-bottom: 1px solid #ccc; }}
                .protocol-box {{ background-color: #f9f9f9; border-left: 4px solid #1565c0; padding: 10px; margin-bottom: 15px; }}
            </style>
        </head>
        <body>
            <h1>Протокол и Аналитика Совещания (CPU Edition)</h1>
            <p><b>Дата формирования:</b> {time.strftime('%d.%m.%Y %H:%M')}</p>
            
            <h2>1. Итоговый протокол (Решения и задачи)</h2>
            <div class="protocol-box">
                {protocol_content}
            </div>

            <h2>2. Аналитика активности и эфирного времени</h2>
            {stats_table_html}
            {chart_html}

            <h2 style="page-break-before: always;">3. Полная стенограмма встречи</h2>
            {"".join(transcript_html_rows)}
        </body>
        </html>
        """

        try:
            doc = QTextDocument()
            doc.setHtml(html_document)

            pdf_writer = QPdfWriter(file_path)
            pdf_writer.setPageSize(QPageSize.A4)
            pdf_writer.setResolution(300)

            doc.print_(pdf_writer)
            QMessageBox.information(self, "Успех", f"PDF-отчет успешно сохранен:\n{file_path}")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка сохранения PDF", str(e))

    def closeEvent(self, event):
        try:
            self.save_recovery_snapshot()
            if self.audio_file_thread:
                self.audio_file_thread.stop()
            if self.recording_active:
                self.stop_meeting()
            if self.asr_thread:
                self.asr_thread.stop()
            if self.llm_thread:
                self.llm_thread.stop()
            logger.info("Приложение завершено штатно")
        except Exception:
            logger.exception("Ошибка при завершении приложения")
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = MeetingSecretaryMainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
