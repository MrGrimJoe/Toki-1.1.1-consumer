"""
main_widget.py  --  TOKI widget-only entry point.

Replaces app.py for the voice + hotkey desktop widget.
No chat window.  No text field.  Just the mark + voice pipeline.

Run from inside your toki_v22/ folder:
    python main_widget.py

WHAT IT DOES
------------
1. Starts the Qt application.
2. Creates DesktopMark (the top-centre animated icon).
3. Creates HotkeyVoicePipeline (Ctrl+K → mic → whisper).
4. Tries to import the orchestrator; if present, wires speech_transcribed
   into orchestrator.process_request so every transcribed utterance goes
   through the same two-tier classify → dispatch path a typed message uses.
   If the orchestrator isn't importable (e.g. Ollama/kuzu not set up yet),
   the widget still works -- it just prints transcriptions to stdout.
5. Hooks pipeline signals back into the mark so the animation reflects
   the actual state of the voice pipeline.

WIRING DIAGRAM
--------------
    Ctrl+K (pynput)
        │
        ▼
    HotkeyVoicePipeline.on_hotkey_trigger()
        │  listening_started ──► mark.listening()  → mysterious mood, expands
        │  speech_transcribed ─► _on_speech(text)  → mark.working("energetic")
        │                                              orchestrator.process_request(text)
        │                                              mark.idle()  (when done)
        │  no_speech_detected ─► mark.idle()        → calm mood, shrinks back
        │  unavailable ────────► print + mark.idle()
        │
    HotkeyVoicePipeline (QThread, background)

    Ctrl+K hotkey (pynput, daemon thread)
        │
        ├──► _bridge.hotkey (Qt signal)
        │       ├──► DesktopMark._on_hotkey()        → expand + mysterious
        │       └──► HotkeyVoicePipeline.on_hotkey_trigger()
        │
        └──► extend_listening()  (if already capturing → reset hangover timer)
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import List, Tuple

# Make sure we can import siblings when run from anywhere
sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from toki_desktop_mark import DesktopMark, _bridge
from voice_pipeline import HotkeyVoicePipeline
from display_strategy import DisplayStrategy, classify_display

# How long to wait, on the background dispatch thread, for a powershell-kind
# command's real completion (executor.RunningCommand's on_done callback)
# before giving up and showing whatever output was collected so far. See
# _await_powershell_result() below for why this wait exists at all.
_POWERSHELL_RESULT_TIMEOUT_S = 30


def _run_and_classify(orch, text: str) -> Tuple[str, str]:
    """
    Runs one turn through orch.process_request() and returns
    (strategy_value, display_text) -- the pair display_strategy.classify_
    display() decided on, ready to hand straight to
    _bridge.result_ready.emit().

    WHY THIS EXISTS AS ITS OWN FUNCTION
    ------------------------------------
    process_request() returns almost immediately for kind=="powershell":
    it starts the actual PowerShell process on its own background thread
    (executor.RunningCommand) and hands back a synchronous placeholder
    response of literally "Done." -- the real stdout and exit code only
    arrive afterward, streamed line-by-line into on_output and finished
    via on_done. Every OTHER kind (api/chat/schedule/timer/app_control/
    generate) is already fully resolved by the time process_request()
    itself returns.

    So: pass real on_output/on_done callbacks (not the no-ops main_widget
    used before this fix -- which meant EVERY powershell-kind INFO
    command, e.g. LIST_FILES, DISK_USAGE, READ_FILE, silently discarded
    its actual output and showed nothing but "Done."), collect the
    output, and -- only for kind=="powershell" -- block this background
    thread (never the Qt thread; this always runs inside the caller's
    own background dispatch thread) on a threading.Event that on_done
    sets, bounded by _POWERSHELL_RESULT_TIMEOUT_S so a hung/slow command
    can't wedge the turn forever.
    """
    output_lines: List[str] = []
    done_event = threading.Event()
    exit_code_holder: dict = {}

    def _on_output(line: str) -> None:
        output_lines.append(line)

    def _on_done(exit_code: int) -> None:
        exit_code_holder["code"] = exit_code
        done_event.set()

    result = orch.process_request(text, on_output=_on_output, on_done=_on_done)

    timed_out = False
    if (result or {}).get("kind") == "powershell":
        completed = done_event.wait(timeout=_POWERSHELL_RESULT_TIMEOUT_S)
        timed_out = not completed

    strategy, display_text = classify_display(
        result,
        collected_output="\n".join(output_lines),
        exit_code=exit_code_holder.get("code"),
        timed_out=timed_out,
    )
    return strategy.value, display_text


def _try_load_orchestrator():
    """
    Returns a ready WindowsAIAssistant (orchestrator) or None if the
    environment isn't set up yet.  Fails soft so the widget still works.
    """
    try:
        from orchestrator import WindowsAIAssistant
        orch = WindowsAIAssistant()
        return orch
    except Exception as exc:
        print(f"[TOKI] Orchestrator unavailable ({exc}); voice will print to stdout.")
        return None


def main() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # ── mark (the entire visible UI) ─────────────────────────────────────────
    mark = DesktopMark()
    mark.show()
    QTimer.singleShot(300, mark.play_startup)

    # ── orchestrator (optional) ──────────────────────────────────────────────
    orch = _try_load_orchestrator()
    if orch is not None:
        mark.set_scheduler(orch.scheduler)
        mark.set_assistant(orch)   # lets avatar single-click confirm permission gate

    # ── permission_confirm signal: avatar click → orchestrator ───────────────
    # When a caution/destructive command is pending (BETA 0.3.38's
    # self._pending_confirmation, replacing the older self._pending_permission
    # this file used to call directly), a single click on the avatar submits
    # an empty message through the SAME process_request() path a normal typed
    # Enter would take -- "" is one of orchestrator.py's own _CONFIRMATION_WORDS,
    # so this confirms and dispatches without any special-cased confirm method;
    # anything that isn't a confirm word falls through to _resume_pending_confirmation()'s
    # own cancel-and-reprocess behavior, unchanged either way.
    def _on_permission_confirm() -> None:
        if orch is None or getattr(orch, "_pending_confirmation", None) is None:
            return

        def _run_confirm():
            try:
                mark.working("energetic")
                strategy_value, display_text = _run_and_classify(orch, "")
                _bridge.result_ready.emit(strategy_value, display_text)
            except Exception as exc:
                _bridge.result_ready.emit(
                    DisplayStrategy.ERROR.value, f"Something went wrong: {exc}",
                )
            finally:
                _bridge.done.emit()

        threading.Thread(target=_run_confirm, daemon=True, name="toki-permission-confirm").start()

    _bridge.permission_confirm.connect(_on_permission_confirm)

    # ── dictation ("start listening") stop button ───────────────────────────
    # AppController.start_dictation()/stop_dictation() run on the same
    # background dispatch thread as every other app_control-kind intent
    # (see _run() below) -- there's no separate signal fired the moment a
    # session actually starts/stops, so _dispatch_text's finally-block just
    # checks orch.app_controller._active_dictation right after each turn
    # and emits _bridge.dictation_active accordingly. Good enough here
    # specifically because dictation sessions only ever start/stop as the
    # direct result of a START_LISTENING/STOP_LISTENING turn -- there's no
    # other code path that could change _active_dictation between one
    # turn's dispatch and the next.
    def _on_dictation_stop_clicked() -> None:
        if orch is None:
            return

        def _run_stop():
            try:
                result = orch.app_controller.stop_dictation()
                # stop_dictation() isn't a process_request() turn -- it's
                # a direct app_controller call with a plain string result
                # -- so it doesn't go through classify_display(). It's
                # unambiguously an action (stop the session), not
                # information to read: DONE.
                _bridge.result_ready.emit(DisplayStrategy.DONE.value, result)
            except Exception as exc:
                _bridge.result_ready.emit(
                    DisplayStrategy.ERROR.value,
                    f"Something went wrong stopping dictation: {exc}",
                )
            finally:
                _bridge.dictation_active.emit(False)

        threading.Thread(target=_run_stop, daemon=True, name="toki-dictation-stop").start()

    _bridge.dictation_stop_clicked.connect(_on_dictation_stop_clicked)

    # ── voice pipeline ───────────────────────────────────────────────────────
    pipeline = HotkeyVoicePipeline()

    # hotkey → pipeline (extend or start)
    _bridge.hotkey.connect(pipeline.on_hotkey_trigger)

    # pipeline → mark
    pipeline.listening_started.connect(mark.listening)
    pipeline.no_speech_detected.connect(mark.idle)
    pipeline.unavailable.connect(lambda reason: (
        print(f"[TOKI-Voice] {reason}"),
        mark.idle(),
    ))

    # ── shared dispatch path: voice AND typed (double-click) prompts both
    # go through this, exactly the same way app.py's Worker.run() is the
    # single dispatch path for typed messages today. ─────────────────────────
    def _dispatch_text(text: str) -> None:
        print(f"[TOKI] Heard/typed: {text}")
        mark.working("energetic")

        if orch is None:
            print(f"[TOKI] (no orchestrator) → {text}")
            # Genuinely an error state (nothing can be dispatched at
            # all), so this goes through the same result_ready/"engaged"
            # path as any other ERROR turn -- consistent persistent
            # card + click-elsewhere-or-Ctrl+K dismissal -- rather than
            # the old transient show_reply() + a hardcoded 800ms timer
            # that didn't sync with anything else in this file anymore.
            _bridge.result_ready.emit(
                DisplayStrategy.ERROR.value, "TOKI's orchestrator isn't available right now.",
            )
            _bridge.done.emit()
            return

        # Run orchestrator in a background thread so Qt stays responsive.
        # process_request is synchronous and can take several seconds
        # (Ollama + PowerShell round-trips).
        def _run():
            try:
                # See _run_and_classify()'s own docstring above for why
                # this isn't just a bare process_request() call anymore:
                # on_output/on_done used to be no-ops here, which meant
                # every powershell-kind INFO command (LIST_FILES,
                # DISK_USAGE, READ_FILE, ...) silently discarded its real
                # output and only ever showed the generic "Done."
                # placeholder. _run_and_classify() collects that output
                # and (only for kind=="powershell") waits for the async
                # on_done before deciding what to show.
                strategy_value, display_text = _run_and_classify(orch, text)
                # Reply appears next to the mark -- a quick fading note
                # for "done", a persistent island card for "info"/"error"
                # (see display_strategy.py / DesktopMark.show_result()).
                # Emitted via a real Qt signal rather than
                # QTimer.singleShot, because this runs on a background
                # thread -- singleShot scheduled from a non-Qt thread is
                # not guaranteed to fire (confirmed directly: it silently
                # never did in testing), whereas Qt signals are safe to
                # emit cross-thread by design.
                _bridge.result_ready.emit(strategy_value, display_text)
            except Exception as exc:
                print(f"[TOKI] Dispatch error: {exc}")
                _bridge.result_ready.emit(
                    DisplayStrategy.ERROR.value, f"Something went wrong: {exc}",
                )
            finally:
                # Return to idle on the Qt thread
                _bridge.done.emit()
                # See the comment on _on_dictation_stop_clicked above for
                # why a plain post-turn check is enough here.
                if orch is not None:
                    is_active = getattr(orch.app_controller, "_active_dictation", None) is not None
                    _bridge.dictation_active.emit(is_active)

        t = threading.Thread(target=_run, daemon=True, name="toki-dispatch")
        t.start()

    # typed input (double-click the mark) goes through the same path
    mark.set_dispatch(_dispatch_text)

    # voice input goes through the same shared dispatch path
    pipeline.speech_transcribed.connect(_dispatch_text)

    pipeline.start()

    # ── clean shutdown ───────────────────────────────────────────────────────
    def _on_quit():
        pipeline.stop_pipeline()
        if orch is not None:
            try:
                orch.scheduler.shutdown()
            except Exception:
                pass

    app.aboutToQuit.connect(_on_quit)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
