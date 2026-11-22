"""Offline, pre-rendered WT9 antenna announcements.

No speech synthesis or network calls are performed at runtime. The current
Kokoro bf_emma voice pack can be generated using scripts/build_kokoro_voice.py.
The output directory keeps its historical en_AU_female name for compatibility.
"""
from array import array
import io
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import wave

VOICE_DIR = Path(__file__).resolve().parent / "voice" / "en_AU_female"
PHRASES = {
    "connecting": "connecting",
    "connected": "connected",
    "disconnected": "disconnected",
    "tracking": "tracking",
    "parking": "parking",
    "parked": "parked",
    "servicing": "servicing",
    "service": "service",
    "stopped": "stopped",
    "slewing": "slewing",
    "manual": "manual control",
    "manual az+": "manual azimuth plus",
    "manual az-": "manual azimuth minus",
    "manual el+": "manual elevation plus",
    "manual el-": "manual elevation minus",
    "manual az+ + el+": "manual azimuth plus and elevation plus",
    "manual az+ + el-": "manual azimuth plus and elevation minus",
    "manual az- + el+": "manual azimuth minus and elevation plus",
    "manual az- + el-": "manual azimuth minus and elevation minus",
    "fault": "fault",
    "slew timeout": "slew timeout",
    "yfactor": "Y factor",
    "scan": "scan",
    "scanning": "scanning",
    "idle": "idle",
    "test": "voice annunciation test",
}


def canonical_state(state):
    state = str(state).strip().lower()
    if state.startswith("manual"):
        directions = state[6:].strip().split(" + ")
        directions = [direction.strip() for direction in directions]
        if directions and all(direction in ("az+", "az-", "el+", "el-") for direction in directions):
            az = next((direction for direction in directions if direction.startswith("az")), None)
            el = next((direction for direction in directions if direction.startswith("el")), None)
            if len(directions) == len(set(directions)) and len(directions) <= 2 and (len(directions) == 1 or (az and el)):
                return "manual " + " + ".join(direction for direction in (az, el) if direction)
        return "manual"
    return state if state in PHRASES else None


def phrase_path(name, state, voice_dir=VOICE_DIR):
    key = canonical_state(state)
    name = str(name).strip().lower()
    if name not in ("east", "west") or key is None:
        return None
    return Path(voice_dir) / (name + "_" + key.replace(" + ", "_and_").replace(" ", "_").replace("+", "plus").replace("-", "minus") + ".wav")


class VoiceAnnunciator:
    def __init__(self, voice_dir=VOICE_DIR):
        self.voice_dir = Path(voice_dir)
        self.engine = shutil.which("aplay")
        self.ready = bool(self.engine and (self.voice_dir / "east_test.wav").is_file())
        self.muted = True
        self.volume = 70
        self._lock = threading.Lock()
        self._queue = queue.Queue(maxsize=12)
        self._closed = False
        self._latest_announcements = {}
        self._process = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="WT9Voice")
        self._thread.start()

    def set_muted(self, muted):
        with self._lock:
            self.muted = bool(muted)
            if self.muted:
                self._clear_queue()
                if self._process is not None and self._process.poll() is None:
                    self._process.terminate()

    def set_volume(self, volume):
        with self._lock:
            self.volume = max(0, min(100, int(volume)))

    def announce(self, name, state):
        path = phrase_path(name, state, self.voice_dir)
        if path is None or not path.is_file():
            return False
        return self._enqueue(path, str(name).strip().lower())

    def say(self, message, force=False):
        # Compatibility with older GUI callers. No dynamic speech synthesis.
        words = str(message).strip().lower().split()
        if len(words) < 2:
            return False
        name = words[0]
        state = "test" if "test" in words else " ".join(words[1:])
        return self.announce(name, state)

    def _enqueue(self, path, name):
        if not self.ready:
            return False
        with self._lock:
            if self._closed or self.muted:
                return False
            try:
                serial = self._latest_announcements.get(name, 0) + 1
                self._latest_announcements[name] = serial
                self._queue.put_nowait((name, serial, path))
                return True
            except queue.Full:
                return False

    def _clear_queue(self):
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    @staticmethod
    def _volume_adjusted_wav(path, volume):
        with wave.open(str(path), "rb") as reader:
            params = reader.getparams()
            frames = reader.readframes(reader.getnframes())
        if params.sampwidth != 2 or params.comptype != "NONE":
            raise ValueError("Voice pack must contain uncompressed 16-bit PCM WAV")
        samples = array("h")
        samples.frombytes(frames)
        if sys.byteorder != "little":
            samples.byteswap()
        gain = max(0.0, min(1.0, volume / 100.0))
        for index, sample in enumerate(samples):
            samples[index] = int(sample * gain)
        if sys.byteorder != "little":
            samples.byteswap()
        output = io.BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setparams(params)
            writer.writeframes(samples.tobytes())
        return output.getvalue()

    def _run(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            name, serial, path = item
            with self._lock:
                if (self._closed or self.muted
                        or self._latest_announcements.get(name) != serial):
                    continue
                volume = self.volume
            try:
                wav = self._volume_adjusted_wav(path, volume)
                with self._lock:
                    if (self._closed or self.muted
                            or self._latest_announcements.get(name) != serial):
                        continue
                    self._process = subprocess.Popen(
                        [self.engine, "-q"], stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    process = self._process
                try:
                    process.communicate(input=wav, timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                except (BrokenPipeError, OSError):
                    pass
            except (OSError, ValueError, wave.Error):
                pass
            finally:
                with self._lock:
                    if self._process is locals().get("process"):
                        self._process = None

    def close(self):
        with self._lock:
            self._closed = True
            self._clear_queue()
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
