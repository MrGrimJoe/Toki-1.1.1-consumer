"""
voice_pipeline.py  --  Ctrl+K hotkey → VAD → faster-whisper transcription.

WHAT CHANGED FROM THE WAKE-WORD VERSION
----------------------------------------
Wake word (openWakeWord) is gone entirely.  The trigger is now Ctrl+K,
handled by toki_desktop_mark._run_listener() via pynput and forwarded
here as extend_listening() calls.

Everything else is the same pipeline:
  · Silero VAD (raw ONNX, no torch) decides when you've stopped talking
  · faster-whisper (tiny.en, int8, CPU) transcribes the utterance
  · speech_transcribed(text) feeds into orchestrator.process_request()
    exactly as a typed message would -- voice is not a separate code path
    past that point.

CTRL+K KEEP-ALIVE BEHAVIOUR
----------------------------
Each Ctrl+K press (while recording is active) calls extend_listening().
That resets a silence hangover timer, so the pipeline keeps listening
even if you paused between phrases.  Stop pressing Ctrl+K and stop
talking → silence timer runs out → transcription fires.

If you press Ctrl+K when the pipeline is idle it starts a fresh capture
session.  If it's already capturing, it extends.  No duplicate sessions.

INSTALL
-------
    pip install pynput faster-whisper sounddevice onnxruntime

openWakeWord is no longer needed.
"""

from __future__ import annotations

import queue
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

# ── audio / VAD constants ────────────────────────────────────────────────────

SAMPLE_RATE        = 16_000
VAD_FRAME_SAMPLES  = 512        # ~32 ms @ 16 kHz (Silero VAD contract)
VAD_THRESHOLD      = 0.45       # speech probability above this = "talking"
SILENCE_HANGOVER_S = 1.8        # seconds of silence before we transcribe
                                 # (reset on each Ctrl+K extend)
MAX_UTTERANCE_S    = 45.0       # hard cap on a single recording
NO_SPEECH_TIMEOUT_S = 6.0      # give up if VAD never sees speech at all

_SILERO_URL  = (
    "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
)
_SILERO_PATH = Path.home() / ".toki" / "silero_vad.onnx"
_WHISPER_MODEL = "tiny.en"


# ── extend_listening: called by the hotkey handler ───────────────────────────
# Module-level so toki_desktop_mark.py can import it without a circular dep.

_extend_event: threading.Event = threading.Event()

# ── hold-to-talk support ──────────────────────────────────────────────────
#
# Two distinct ways to use Ctrl+K, both handled by toki_desktop_mark.py's
# hotkey listener and reported here:
#   - a quick tap (press, release almost immediately): the pipeline keeps
#     doing exactly what it always did -- capture until SILENCE_HANGOVER_S
#     of real silence, no change to that path at all.
#   - a genuine press-and-hold (release comes noticeably later): the key
#     being physically down is itself the "keep listening" signal, so a
#     natural mid-sentence pause must NOT end the recording early the way
#     it would for a tap -- and releasing the key should end it
#     immediately, without waiting out the normal hangover.
#
# _key_held tracks "is Ctrl+K physically down right now" -- set/cleared on
# every press/release regardless of how long it turns out to have been
# held, since that's only knowable in hindsight at release time (see
# toki_desktop_mark.py's on_release, which decides tap-vs-hold from the
# elapsed duration and only calls force_stop_and_transcribe() for a real
# hold). While True, _record_and_transcribe()'s silence-hangover check is
# suspended -- a tap clears this again within well under
# SILENCE_HANGOVER_S in virtually every real press, so this never changes
# tap behavior in practice.
_key_held: threading.Event = threading.Event()
_force_stop_event: threading.Event = threading.Event()


def extend_listening() -> None:
    """
    Reset the silence hangover timer inside an active capture session.
    Callable from any thread.  No-op if the pipeline is not capturing.
    """
    _extend_event.set()


def set_key_held(held: bool) -> None:
    """Called by toki_desktop_mark's hotkey listener on every Ctrl+K
    press/release. While held, the silence-hangover auto-stop below is
    suspended so a deliberate hold-to-talk press isn't cut short by an
    ordinary pause between sentences. Callable from any thread."""
    if held:
        _key_held.set()
    else:
        _key_held.clear()


def force_stop_and_transcribe() -> None:
    """Called on a hold-to-talk RELEASE (toki_desktop_mark's on_release,
    only once the elapsed press duration crosses its hold-vs-tap
    threshold) to end the current recording right now instead of waiting
    out the normal silence hangover. No-op if nothing is currently
    capturing. Callable from any thread."""
    _force_stop_event.set()


# ── Silero VAD wrapper ───────────────────────────────────────────────────────

class _SileroVAD:
    """
    Thin wrapper around the raw Silero VAD ONNX model.
    Downloads the model once on first use (~2 MB).
    """

    def __init__(self):
        self._session = None
        self._state   = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(0, dtype=np.float32)

    def _ensure_loaded(self):
        if self._session is not None:
            return
        _SILERO_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not _SILERO_PATH.exists():
            print("[TOKI-VAD] Downloading Silero VAD model (~2 MB)…")
            try:
                urllib.request.urlretrieve(_SILERO_URL, _SILERO_PATH)
                print("[TOKI-VAD] Download complete.")
            except Exception as exc:
                raise RuntimeError(f"[TOKI-VAD] Could not download Silero VAD: {exc}")
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._session = ort.InferenceSession(str(_SILERO_PATH), sess_options=opts)

    def reset(self):
        self._state   = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(0, dtype=np.float32)

    def speech_prob(self, frame_f32: np.ndarray) -> float:
        """
        frame_f32: float32 array of VAD_FRAME_SAMPLES samples, range [-1, 1].
        Returns speech probability in [0, 1].
        """
        self._ensure_loaded()
        # Silero needs a leading context of 64 samples; carry between frames.
        ctx_len = 64
        x = np.concatenate([self._context, frame_f32])[-ctx_len - VAD_FRAME_SAMPLES:]
        inp = x[-VAD_FRAME_SAMPLES:][np.newaxis, :]   # (1, 512)
        sr  = np.array(SAMPLE_RATE, dtype=np.int64)
        out = self._session.run(None, {
            "input":       inp,
            "state":       self._state,
            "sr":          sr,
        })
        prob            = float(out[0].squeeze())
        self._state     = out[1]
        self._context   = x[-ctx_len:]
        return prob


# ── HotkeyVoicePipeline ──────────────────────────────────────────────────────

class HotkeyVoicePipeline(QThread):
    """
    Runs in a background QThread.  Waits for Ctrl+K (signalled via the
    module-level _extend_event / a separate start event), then captures
    audio until silence.

    Signals
    -------
    listening_started   -- first audio frame captured
    speech_transcribed  -- (str) text ready to dispatch
    no_speech_detected  -- Ctrl+K was pressed but no actual speech was heard
    unavailable         -- (str) setup failed, reason given
    """

    listening_started  = pyqtSignal()
    speech_transcribed = pyqtSignal(str)
    no_speech_detected = pyqtSignal()
    unavailable        = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        # NOTE: no setDaemon() here -- QThread has no such method (that's
        # threading.Thread-only API and calling it here raised
        # AttributeError unconditionally, confirmed directly). Clean
        # shutdown is already handled by main_widget.py's _on_quit calling
        # stop_pipeline() before the app quits, so nothing else is needed.

        self._stop_flag    = threading.Event()
        self._trigger      = threading.Event()   # set by on_hotkey_trigger()
        self._audio_q: queue.Queue[np.ndarray] = queue.Queue()
        self._capturing    = False
        self._stream       = None   # sounddevice InputStream
        self._vad          = _SileroVAD()
        self._whisper      = None

    # ── called by the hotkey handler in toki_desktop_mark ───────────────────

    def on_hotkey_trigger(self) -> None:
        """
        Called (from any thread) whenever Ctrl+K fires.
        · If not yet capturing → starts a new session.
        · If already capturing → extends the silence window (same as
          calling extend_listening()).
        """
        if self._capturing:
            extend_listening()
        else:
            self._trigger.set()

    # ── QThread.run ──────────────────────────────────────────────────────────

    def run(self) -> None:
        try:
            import sounddevice as sd
        except ImportError:
            self.unavailable.emit(
                "sounddevice not installed. Run: pip install sounddevice"
            )
            return

        try:
            self._whisper = self._load_whisper()
        except Exception as exc:
            self.unavailable.emit(f"faster-whisper load failed: {exc}")
            return

        def _audio_callback(indata, frames, t, status):
            if self._capturing:
                self._audio_q.put(indata[:, 0].copy())

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=VAD_FRAME_SAMPLES,
                callback=_audio_callback,
            )
            self._stream.start()
        except Exception as exc:
            self.unavailable.emit(f"Microphone not available: {exc}")
            return

        print("[TOKI-Voice] Ready.  Press Ctrl+K to speak.")

        try:
            while not self._stop_flag.is_set():
                triggered = self._trigger.wait(timeout=0.5)
                if not triggered:
                    continue
                self._trigger.clear()
                self._record_and_transcribe()
        finally:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()

    def stop_pipeline(self) -> None:
        self._stop_flag.set()
        self._trigger.set()   # unblock .wait()
        self.wait(3000)

    # ── capture → transcribe ─────────────────────────────────────────────────

    def _record_and_transcribe(self) -> None:
        """
        Capture audio until:
          · SILENCE_HANGOVER_S of silence (not reset by Ctrl+K or speech), OR
          · MAX_UTTERANCE_S hard cap, OR
          · NO_SPEECH_TIMEOUT_S without any detected speech.
        """
        # BETA 0.3.28 fix (restored 0.3.31 -- an intervening edit had moved
        # this back to running AFTER the drain/vad-reset/extend_event.clear()
        # below, i.e. the exact original bug ordering, with a comment
        # claiming otherwise. Verified live that this reopened the race:
        # hooking _vad.reset() and firing on_hotkey_trigger() at that point
        # showed self._capturing still False, extend_listening() NOT
        # called, and self._trigger.set() fired instead -- the original bug,
        # reproduced exactly. Moving this back to the top and confirming
        # again below.):
        #
        # on_hotkey_trigger() (called from the pynput listener thread,
        # entirely independent of this one) checks self._capturing to
        # decide "start a new session" vs "extend the current one". If a
        # Ctrl+K press landed in the window between run()'s
        # self._trigger.clear() and this line, on_hotkey_trigger() saw
        # False and called self._trigger.set() again instead of
        # extend_listening() -- but run()'s while loop won't check
        # self._trigger again until THIS session's
        # _record_and_transcribe() call returns, so that keypress didn't
        # extend anything -- it just sat there and, once this session
        # ended for an unrelated reason, immediately kicked off a brand
        # new recording session instead of the silence-extension the user
        # actually asked for.
        #
        # Fix: set self._capturing = True FIRST, before the drain/reset,
        # so on_hotkey_trigger() can never observe a stale False while a
        # session is genuinely already underway. Deliberately accepted
        # trade-off: the audio callback (_audio_callback) now also gates
        # on self._capturing, so it's technically possible for it to
        # enqueue one live ~tens-of-ms audio frame WHILE the drain loop
        # below is still clearing out the previous session's stale
        # frames, and for that one frame to get swept up in the drain
        # too. That's a single frame of audio at the very start of a
        # session, not a lost keypress/session -- a strictly smaller and
        # far less user-visible cost than the race being fixed here.
        self._capturing = True

        # drain stale audio from queue
        while not self._audio_q.empty():
            try:
                self._audio_q.get_nowait()
            except queue.Empty:
                break

        self._vad.reset()
        _extend_event.clear()
        _force_stop_event.clear()

        recorded           = np.zeros(0, dtype=np.int16)
        vad_buf            = np.zeros(0, dtype=np.int16)
        heard_speech       = False
        last_speech_t      = time.monotonic()
        session_start_t    = last_speech_t

        self.listening_started.emit()

        while not self._stop_flag.is_set():
            now = time.monotonic()

            # hold-to-talk release: end right now rather than waiting out
            # the normal silence hangover (see force_stop_and_transcribe()'s
            # docstring). A release before any speech was even heard is
            # treated the same as the ordinary no-speech timeout below,
            # not as an utterance to transcribe.
            if _force_stop_event.is_set():
                _force_stop_event.clear()
                if heard_speech:
                    break
                self._capturing = False
                self.no_speech_detected.emit()
                return

            # hard cap
            if now - session_start_t > MAX_UTTERANCE_S:
                break

            # no-speech timeout
            if not heard_speech and (now - session_start_t) > NO_SPEECH_TIMEOUT_S:
                self._capturing = False
                self.no_speech_detected.emit()
                return

            # silence hangover (only after we've heard something) -- held
            # off entirely while the hotkey is still physically down (a
            # deliberate hold-to-talk press), since a pause mid-sentence
            # while still holding must not end the recording; see
            # set_key_held()'s docstring for why this never changes a
            # plain tap's behavior in practice.
            if heard_speech and (now - last_speech_t) > SILENCE_HANGOVER_S and not _key_held.is_set():
                # Check if Ctrl+K extended us
                if _extend_event.is_set():
                    _extend_event.clear()
                    last_speech_t = time.monotonic()   # reset the clock
                    continue
                break   # genuine silence end → transcribe

            # collect audio
            try:
                chunk = self._audio_q.get(timeout=0.3)
            except queue.Empty:
                # while waiting, still check for extend
                if _extend_event.is_set() and heard_speech:
                    _extend_event.clear()
                    last_speech_t = time.monotonic()
                continue

            recorded = np.append(recorded, chunk)
            vad_buf  = np.append(vad_buf, chunk)

            # run VAD on complete frames
            while len(vad_buf) >= VAD_FRAME_SAMPLES:
                frame   = vad_buf[:VAD_FRAME_SAMPLES]
                vad_buf = vad_buf[VAD_FRAME_SAMPLES:]
                frame_f = frame.astype(np.float32) / 32768.0
                try:
                    prob = self._vad.speech_prob(frame_f)
                except Exception:
                    prob = 0.0
                if prob >= VAD_THRESHOLD:
                    heard_speech  = True
                    last_speech_t = time.monotonic()

        self._capturing = False

        if not heard_speech or len(recorded) == 0:
            self.no_speech_detected.emit()
            return

        audio_f32 = recorded.astype(np.float32) / 32768.0
        try:
            segments, _info = self._whisper.transcribe(
                audio_f32, language="en", beam_size=1
            )
            text = "".join(seg.text for seg in segments).strip()
        except Exception as exc:
            print(f"[TOKI-Voice] Transcription error: {exc}")
            self.no_speech_detected.emit()
            return

        if text:
            self.speech_transcribed.emit(text)
        else:
            self.no_speech_detected.emit()

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _load_whisper():
        return _load_whisper_model()


# ── continuous dictation ("start listening") ────────────────────────────────
#
# BETA 0.3.44 addition. HotkeyVoicePipeline above is single-utterance: one
# Ctrl+K press captures ONE utterance, transcribes it, and hands it to
# orchestrator.process_request() -- the normal classify/dispatch pipeline.
# DictationPipeline is a different shape entirely: it keeps capturing
# utterance after utterance in a loop, and each transcribed utterance is
# typed directly into a target field (see app_control.py's
# AppController.start_dictation()) -- it never goes through orchestrator's
# classify/dispatch at all, by design. "whatever you say immediately starts
# getting typed" only works if there's no per-utterance round-trip through
# intent classification in between.
#
# Shares _SileroVAD and the whisper-loading logic with HotkeyVoicePipeline
# (via the module-level _load_whisper_model() both now call) rather than
# duplicating that setup -- everything else about the loop is intentionally
# NOT shared, since "segment once and hand off" and "segment forever until
# told to stop" are different enough control flows that trying to force them
# through one shared method would have made both harder to follow than two
# short, separate ones.

def _load_whisper_model():
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise ImportError(
            "faster-whisper not installed. Run: pip install faster-whisper"
        )
    cache_dir = Path.home() / ".toki" / "whisper"
    cache_dir.mkdir(parents=True, exist_ok=True)
    print(f"[TOKI-Voice] Loading faster-whisper ({_WHISPER_MODEL}, int8, CPU)…")
    model = WhisperModel(
        _WHISPER_MODEL,
        device="cpu",
        compute_type="int8",
        download_root=str(cache_dir),
    )
    print("[TOKI-Voice] Whisper model ready.")
    return model


class DictationPipeline(QThread):
    """
    Runs in its own background QThread, entirely separate from any
    HotkeyVoicePipeline instance that may also be running (Ctrl+K keeps
    working normally while dictation is active -- they use independent
    sounddevice InputStreams, same as any two audio-consuming apps would).

    Started by AppController.start_dictation() once a typing target has
    been resolved; stopped by AppController.stop_dictation() (wired to the
    widget's stop button in main_widget.py -- see toki_desktop_mark.py's
    dictation panel). There is no silence-hangover auto-stop here on
    purpose: HotkeyVoicePipeline's whole point is "one utterance, then
    stop and process it"; dictation's whole point is "keep going until
    the user says to stop", so a pause between sentences must NOT end the
    session the way it would for a normal command.

    Signals
    -------
    dictation_started     -- audio stream opened, actively listening
    utterance_transcribed  -- (str) one finished utterance's text, emitted
                              as soon as it's ready -- the caller types it
                              immediately, no batching
    dictation_stopped     -- stream closed, thread about to exit cleanly
    unavailable           -- (str) setup failed, reason given
    """

    dictation_started    = pyqtSignal()
    utterance_transcribed = pyqtSignal(str)
    dictation_stopped     = pyqtSignal()
    unavailable           = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop_flag = threading.Event()
        self._audio_q: queue.Queue[np.ndarray] = queue.Queue()
        self._capturing = False
        self._stream = None
        self._vad = _SileroVAD()
        self._whisper = None

    def stop(self) -> None:
        """Callable from any thread (the Qt main thread, via the widget's
        stop button click handler). Signals the run() loop to finish its
        current segment check and exit -- does not hard-kill mid-utterance,
        so at most one in-flight utterance still gets transcribed and typed
        after stop() is called, never a torn/partial one."""
        self._stop_flag.set()

    def run(self) -> None:
        try:
            import sounddevice as sd
        except ImportError:
            self.unavailable.emit(
                "sounddevice not installed. Run: pip install sounddevice"
            )
            return

        try:
            self._whisper = _load_whisper_model()
        except Exception as exc:
            self.unavailable.emit(f"faster-whisper load failed: {exc}")
            return

        def _audio_callback(indata, frames, t, status):
            if self._capturing:
                self._audio_q.put(indata[:, 0].copy())

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=VAD_FRAME_SAMPLES,
                callback=_audio_callback,
            )
            self._stream.start()
        except Exception as exc:
            self.unavailable.emit(f"Microphone not available: {exc}")
            return

        self._capturing = True
        self.dictation_started.emit()
        print("[TOKI-Dictation] Listening -- speak, and it'll be typed. Tap stop when done.")

        try:
            while not self._stop_flag.is_set():
                text = self._capture_one_utterance()
                if text:
                    self.utterance_transcribed.emit(text)
                # No text (silence/timeout) just loops back and keeps
                # listening -- unlike HotkeyVoicePipeline, silence is not
                # a stop condition here, only self._stop_flag is.
        finally:
            self._capturing = False
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
            self.dictation_stopped.emit()

    def _capture_one_utterance(self) -> str:
        """
        Same VAD-segmentation shape as HotkeyVoicePipeline._record_and_
        transcribe() (silence hangover ends a segment, a hard cap prevents
        a runaway recording, an initial no-speech window returns early so
        the outer loop can re-check self._stop_flag promptly) -- returns
        "" instead of emitting a signal on a no-speech/empty result, so
        the caller's loop can just check truthiness and keep going either
        way. No Ctrl+K extend-listening concept here -- there's no
        separate "extend" trigger in continuous dictation, the whole
        point is that it never stops on its own.
        """
        while not self._audio_q.empty():
            try:
                self._audio_q.get_nowait()
            except queue.Empty:
                break
        self._vad.reset()

        recorded        = np.zeros(0, dtype=np.int16)
        vad_buf         = np.zeros(0, dtype=np.int16)
        heard_speech    = False
        last_speech_t   = time.monotonic()
        session_start_t = last_speech_t

        while not self._stop_flag.is_set():
            now = time.monotonic()

            if now - session_start_t > MAX_UTTERANCE_S:
                break
            if not heard_speech and (now - session_start_t) > NO_SPEECH_TIMEOUT_S:
                return ""
            if heard_speech and (now - last_speech_t) > SILENCE_HANGOVER_S:
                break

            try:
                chunk = self._audio_q.get(timeout=0.3)
            except queue.Empty:
                continue

            recorded = np.append(recorded, chunk)
            vad_buf = np.append(vad_buf, chunk)

            while len(vad_buf) >= VAD_FRAME_SAMPLES:
                frame = vad_buf[:VAD_FRAME_SAMPLES]
                vad_buf = vad_buf[VAD_FRAME_SAMPLES:]
                frame_f = frame.astype(np.float32) / 32768.0
                try:
                    prob = self._vad.speech_prob(frame_f)
                except Exception:
                    prob = 0.0
                if prob >= VAD_THRESHOLD:
                    heard_speech = True
                    last_speech_t = time.monotonic()

        if not heard_speech or len(recorded) == 0:
            return ""

        audio_f32 = recorded.astype(np.float32) / 32768.0
        try:
            segments, _info = self._whisper.transcribe(
                audio_f32, language="en", beam_size=1
            )
            return "".join(seg.text for seg in segments).strip()
        except Exception as exc:
            print(f"[TOKI-Dictation] Transcription error: {exc}")
            return ""
