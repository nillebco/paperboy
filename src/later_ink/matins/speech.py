"""Reading aloud on the server: Kokoro-82M, one MP3 per paragraph, cached.

A phone is too slow to run Kokoro faster than it speaks; the server's two ARM
cores make speech about twice as fast as it is spoken (8-bit model, measured on
the OCI VM). The Reader posts an article's paragraphs, gets one key per
paragraph back, and plays /read/speech/<key>.mp3 one after the other while the
next ones are made. The paragraph a reader is waiting for goes first.

Keys are content addresses (model, voice, text), so a paragraph is made once
whoever listens to it. The files live on the data volume (oldest dropped past
SPEECH_CACHE_MAX_BYTES) and, when SPEECH_STORE_URL is set, in object storage
too: an OCI pre-authenticated request URL ending in /o/, which takes a plain
GET and PUT per object, no signing.

The model and voices are the ones the browser player downloads (tts-assets);
kokoro-onnx phonemizes with eSpeak NG, as kokoro-js does. Without kokoro-onnx or
the assets, available() is False and the setting does not offer the server.
"""

import asyncio
import hashlib
import heapq
import itertools
import logging
import os
import threading
import time
from pathlib import Path

import httpx
import numpy as np

from . import config

logger = logging.getLogger(__name__)

MODEL = "onnx/model_quantized.onnx"
# Part of every key: a new model or encoding makes new files.
VERSION = "kokoro-82m-v1.0-q8/mp3-48k/1"
ESPEAK = {"a": "en-us", "b": "en-gb", "f": "fr-fr", "i": "it", "e": "es", "p": "pt-br"}
SAMPLE_RATE = 24000
BITRATE = 48
# After each paragraph, so two in a row do not run together.
TAIL_SECONDS = 0.25
MAX_BLOCKS = 400
MAX_CHARS = 6000

# Interactive requests first, newest listen first; prefetches after.
NOW, LATER = 0, 1


def key_for(voice: str, text: str) -> str:
    return hashlib.sha256(f"{VERSION}\0{voice}\0{text}".encode()).hexdigest()[:32]


def available() -> bool:
    if os.environ.get("SPEECH_DISABLED"):
        return False
    assets = config.get_tts_assets_dir()
    if assets is None or not (assets / "model" / MODEL).is_file():
        return False
    try:
        import kokoro_onnx  # noqa: F401
        import lameenc  # noqa: F401
    except ImportError:
        return False
    return True


class Kokoro:
    """kokoro-onnx over the vendored browser model, loaded on first use."""

    def __init__(self, assets: Path, workdir: Path) -> None:
        self._assets = assets
        self._workdir = workdir
        self._engine = None

    def _load(self):
        import onnxruntime as rt
        from kokoro_onnx import Kokoro as Engine

        voices = self._workdir / "voices.npz"
        if not voices.is_file():
            arrays = {
                path.stem: np.fromfile(path, dtype=np.float32).reshape(-1, 1, 256)
                for path in sorted((self._assets / "model" / "voices").glob("*.bin"))
            }
            partial = self._workdir / "voices.tmp.npz"
            np.savez(partial, **arrays)
            partial.replace(voices)
        options = rt.SessionOptions()
        options.intra_op_num_threads = os.cpu_count() or 1
        session = rt.InferenceSession(
            str(self._assets / "model" / MODEL), options, providers=["CPUExecutionProvider"]
        )
        return Engine.from_session(session, str(voices))

    def mp3(self, voice: str, text: str) -> bytes:
        import lameenc

        if self._engine is None:
            self._engine = self._load()
        audio, rate = self._engine.create(
            text, voice=voice, speed=1.0, lang=ESPEAK.get(voice[:1], "en-us")
        )
        audio = np.concatenate([audio, np.zeros(int(TAIL_SECONDS * rate), dtype=audio.dtype)])
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(BITRATE)
        encoder.set_in_sample_rate(rate)
        encoder.set_channels(1)
        encoder.set_quality(2)
        return bytes(encoder.encode(pcm) + encoder.flush())


class Remote:
    """Object storage behind a pre-authenticated request URL (…/o/)."""

    def __init__(self, base: str) -> None:
        self._base = base if base.endswith("/") else base + "/"

    def get(self, key: str) -> bytes | None:
        try:
            response = httpx.get(f"{self._base}speech/{key}.mp3", timeout=20.0)
        except httpx.HTTPError as e:
            logger.warning("speech store unreachable: %s", type(e).__name__)
            return None
        return response.content if response.status_code == 200 else None

    def put(self, key: str, data: bytes) -> None:
        try:
            response = httpx.put(
                f"{self._base}speech/{key}.mp3",
                content=data,
                headers={"Content-Type": "audio/mpeg"},
                timeout=30.0,
            )
            if response.status_code >= 300:
                logger.warning("speech store refused %s: %s", key, response.status_code)
        except httpx.HTTPError as e:
            logger.warning("speech store unreachable: %s", type(e).__name__)


class Speech:
    def __init__(self, cache_dir: Path, max_bytes: int, synth, remote: Remote | None = None):
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max = max_bytes
        self._synth = synth
        self._remote = remote
        self._lock = threading.Condition()
        self._heap: list[tuple] = []
        self._order = itertools.count()
        self._texts: dict[str, tuple[str, str]] = {}
        self._waiters: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Future]]] = {}
        self._failed: set[str] = set()
        self._size = sum(p.stat().st_size for p in self._dir.glob("*.mp3"))
        self._thread = threading.Thread(target=self._run, name="speech", daemon=True)
        self._thread.start()

    def path(self, key: str) -> Path:
        return self._dir / f"{key}.mp3"

    def submit(self, voice: str, blocks: list[str], priority: int = NOW) -> list[str]:
        """Queue the paragraphs not made yet; their keys, in order."""
        keys = [key_for(voice, text) for text in blocks]
        # Newer listens go before older ones still in the queue.
        batch = -time.monotonic() if priority == NOW else time.monotonic()
        with self._lock:
            for index, (key, text) in enumerate(zip(keys, blocks)):
                if self.path(key).is_file():
                    continue
                self._failed.discard(key)
                self._texts[key] = (voice, text)
                heapq.heappush(self._heap, (priority, batch, index, next(self._order), key))
            self._lock.notify()
        return keys

    async def audio(self, key: str, timeout: float = 120.0) -> Path | None:
        """The paragraph's file, waiting while it is being made; None if unknown."""
        path = self.path(key)
        if path.is_file():
            return path
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        with self._lock:
            if path.is_file():
                return path
            if key not in self._texts:
                return None
            self._waiters.setdefault(key, []).append((loop, future))
        try:
            await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return None
        return path if path.is_file() else None

    def _run(self) -> None:
        # Below the web server, which has to keep answering while this works.
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
        except (AttributeError, OSError):
            pass
        while True:
            with self._lock:
                while not self._heap:
                    self._lock.wait()
                *_, key = heapq.heappop(self._heap)
                job = self._texts.get(key)
            if job is None or self.path(key).is_file():
                self._done(key)
                continue
            try:
                data = self._remote.get(key) if self._remote else None
                if data is None:
                    started = time.monotonic()
                    data = self._synth(*job)
                    logger.info(
                        "speech: %s chars in %.1fs (%s)", len(job[1]), time.monotonic() - started, job[0]
                    )
                    if self._remote:
                        self._remote.put(key, data)
                self._store(key, data)
            except Exception:
                logger.exception("speech failed for a paragraph (%s chars)", len(job[1]))
                with self._lock:
                    self._failed.add(key)
            self._done(key)

    def _store(self, key: str, data: bytes) -> None:
        partial = self._dir / f"{key}.part"
        partial.write_bytes(data)
        partial.replace(self.path(key))
        self._size += len(data)
        if self._size > self._max:
            self._evict()

    def _evict(self) -> None:
        files = sorted(self._dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
        for path in files:
            if self._size <= self._max * 0.9:
                break
            try:
                size = path.stat().st_size
                path.unlink()
                self._size -= size
            except OSError:
                pass

    def _done(self, key: str) -> None:
        with self._lock:
            self._texts.pop(key, None)
            waiters = self._waiters.pop(key, [])
        for loop, future in waiters:
            loop.call_soon_threadsafe(_resolve, future)


def _resolve(future: asyncio.Future) -> None:
    if not future.done():
        future.set_result(None)


_shared: Speech | None = None
_shared_lock = threading.Lock()


def service() -> Speech | None:
    """The process's speech service, or None when the server cannot speak."""
    global _shared
    if not available():
        return None
    with _shared_lock:
        if _shared is None:
            cache = config.get_speech_cache_dir()
            store = os.environ.get("SPEECH_STORE_URL", "").strip()
            _shared = Speech(
                cache,
                config.get_speech_cache_max_bytes(),
                Kokoro(config.get_tts_assets_dir(), cache).mp3,
                Remote(store) if store else None,
            )
        return _shared
