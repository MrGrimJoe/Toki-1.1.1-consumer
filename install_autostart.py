"""
install_autostart.py -- opt-in: makes toki_desktop_mark.py launch when
Windows starts, so the desktop icon is just always there without having
to manually run anything first.

Not run automatically by anything else in this project. You run this
once, yourself:

    python install_autostart.py            # installs
    python install_autostart.py --remove   # uninstalls

WHAT IT ACTUALLY DOES: adds one value under
HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run,
named "TOKIDesktopMark", pointing at:

    <this venv's pythonw.exe> <full path to toki_desktop_mark.py>

pythonw.exe (not python.exe) specifically, so no console window flashes
up at every login -- just the icon itself. Per-user (HKCU), not
per-machine (HKLM), so it doesn't need Administrator and only affects
the account that runs this script.

This launches ONLY the desktop mark (toki_desktop_mark.standalone_main),
not the full chat window/Ollama connection -- deliberately, since there's
no real speech-to-text wired in yet to justify keeping the full backend
warm at every boot (see toki_desktop_mark.py's own docstring for exactly
where that hook goes once one exists). Once real STT does exist, this is
the file to point at the fuller startup sequence instead.

Windows only -- checked at the top, refuses to touch anything on any
other platform.
"""

import sys
import os


def _require_windows():
    if sys.platform != "win32":
        print("install_autostart.py only does anything on Windows. "
              "Nothing was changed.")
        sys.exit(1)


def install():
    _require_windows()
    import winreg

    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "toki_desktop_mark.py")
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        # Fallback for environments without a pythonw.exe alongside
        # python.exe (rare, but don't just silently fail).
        pythonw = sys.executable

    command = f'"{pythonw}" "{script}"'

    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0, winreg.KEY_SET_VALUE,
    )
    try:
        winreg.SetValueEx(key, "TOKIDesktopMark", 0, winreg.REG_SZ, command)
    finally:
        winreg.CloseKey(key)

    print("Installed. TOKI's desktop mark will now launch automatically")
    print(f"at login, running:\n    {command}")
    print("\nRun this script with --remove to undo.")


def remove():
    _require_windows()
    import winreg

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        try:
            winreg.DeleteValue(key, "TOKIDesktopMark")
        finally:
            winreg.CloseKey(key)
        print("Removed. The desktop mark will no longer launch at login.")
    except FileNotFoundError:
        print("Nothing to remove -- it wasn't installed.")


if __name__ == "__main__":
    if "--remove" in sys.argv:
        remove()
    else:
        install()
