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
from pathlib import Path

# Make sure we can import siblings when run from anywhere
sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from toki_desktop_mark import DesktopMark, _bridge
from voice_pipeline import HotkeyVoicePipeline


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
        import threading

        def _run_confirm():
            try:
                mark.working("energetic")
                result = orch.process_request(
                    "",
                    on_output=lambda _line: None,
                    on_done=lambda _exit_code: None,
                )
                response = (result or {}).get("response", "")
                _bridge.reply.emit(response)
            except Exception as exc:
                _bridge.reply.emit(f"Something went wrong: {exc}")
            finally:
                _bridge.done.emit()

        threading.Thread(target=_run_confirm, daemon=True, name="toki-permission-confirm").start()

    _bridge.permission_confirm.connect(_on_permission_confirm)

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
            mark.show_reply("(orchestrator not available)")
            QTimer.singleShot(800, mark.idle)
            return

        # Run orchestrator in a background thread so Qt stays responsive.
        # process_request is synchronous and can take several seconds
        # (Ollama + PowerShell round-trips).
        import threading

        def _run():
            try:
                # process_request()'s real signature (orchestrator.py) is
                # (user_prompt, on_output, on_done, on_thinking_token=None,
                # on_generate_token=None, on_generate_done=None) -- on_output
                # streams live PowerShell text, on_done(int) is an exit
                # code, NOT a completion signal carrying the result. The
                # actual per-turn result dict ({"response": ..., "kind": ...,
                # ...}) comes back as process_request's own return value, the
                # same way app.py's Worker.run() consumes it. There's no chat
                # UI here to stream tokens into, so on_output/on_done are
                # both no-ops -- but they must still be passed as on_output=/
                # on_done= (positional-or-keyword), matching the real
                # parameter names, or this raises TypeError on every
                # command and gets silently swallowed by the except below.
                result = orch.process_request(
                    text,
                    on_output=lambda _line: None,
                    on_done=lambda _exit_code: None,
                )
                response = (result or {}).get("response", "")
                # Reply appears as floating text next to the mark (fades
                # after 3s) rather than only printing -- this IS the reply
                # surface now that there's no chat window. Emitted via
                # _bridge.reply (a real Qt signal) rather than
                # QTimer.singleShot, because this runs on a background
                # thread -- singleShot scheduled from a non-Qt thread is
                # not guaranteed to fire (confirmed directly: it silently
                # never did in testing), whereas Qt signals are safe to
                # emit cross-thread by design.
                _bridge.reply.emit(response)
            except Exception as exc:
                print(f"[TOKI] Dispatch error: {exc}")
                _bridge.reply.emit(f"Something went wrong: {exc}")
            finally:
                # Return to idle on the Qt thread
                _bridge.done.emit()

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
