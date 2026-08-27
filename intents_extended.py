"""
intents_extended.py — batch-1 additions sourced from windows_command_library_core.json.

Same shape as INTENTS in intents.py, on purpose — this is meant to be merged
into the same enum/dispatch, not a parallel system. Kept in its own file so
this batch can be reviewed, tested, and reverted independently of the
hand-written core set while the intent list is still growing.

── Why only 27 of the ~1000+ entries in the source databases ─────────────────
The model picks an intent by schema-constrained decoding over
`INTENT_NAMES` — that enum IS the grammar Ollama constrains against on every
single call. Jumping straight from ~20 to 1000+ enum values is almost
certainly what caused the crashing/hanging last time: it's a much bigger
grammar for a 4B model (Phi-4-mini, 7GB VRAM budget) to compile and sample
against per turn, not just "more data".

So this batch was hand-curated, not dumped wholesale:
  - Only entries the source data marked danger_level=safe, requires_admin=
    False, requires_confirmation=False.
  - Only filesystem / process / system_info / disk_storage categories — a
    direct extension of what TOKI already does, not new territory.
  - Dropped entries whose `variables` had vague "meaning depends on the
    command" descriptions or placeholder examples ("<item_type>") — those
    can't be extracted deterministically by regex, which is the exact
    ambiguity that broke earlier versions.
  - Dropped shell-spawning aliases (cmd, powershell, pwsh, bash, exit),
    variable-mutation (set, sv), raw network calls (iwr, ftp, tftp),
    arbitrary-argument process launch (Open-Application) — that last one
    is explicitly deferred to the planned cursor-control pass, not folded
    in here as a side effect.
  - Dropped duplicate aliases of intents that already exist (ls/gci/cp/cd/
    etc.) rather than bloating the enum with synonyms — synonyms are
    handled by extractor regex matching multiple phrasings into the SAME
    intent, the way GET_WEATHER etc. already work.
  - Dropped a few whose "safe" flag looked wrong for this app's sandbox
    model (Get-WindowsDriver -Online effectively needs admin in practice;
    Get-ScheduledTask touches the task-scheduler subsystem).

Every filesystem-touching template below still routes its {path}/{file_path}
through extractor.resolve_path(), so the D:\\ + Desktop sandbox check in
extractor.is_within_sandbox() applies exactly the same as the original
intents — nothing here bypasses it.
"""

from typing import Dict, Any

INTENTS_EXTENDED: Dict[str, Dict[str, Any]] = {

    # ── Filesystem — path/info utilities ────────────────────────────────────
    "PATH_EXISTS": {
        "description": "Check whether a file or folder exists",
        "kind": "powershell",
        "template": "Test-Path -Path '{path}'",
        "slots": ["path"],
        "reversible": False,
    },
    "ITEM_PROPERTIES": {
        "description": "Show detailed properties of a file or folder",
        "kind": "powershell",
        "template": "Get-ItemProperty -Path '{path}'",
        "slots": ["path"],
        "reversible": False,
    },
    "CURRENT_LOCATION": {
        "description": "Show the current working directory",
        "kind": "powershell",
        "template": "Get-Location",
        "slots": [],
        "reversible": False,
    },
    "RESOLVE_PATH": {
        "description": "Resolve a wildcard or relative path to its full path",
        "kind": "powershell",
        "template": "Resolve-Path -Path '{path}'",
        "slots": ["path"],
        "reversible": False,
    },
    "SPLIT_PATH": {
        "description": "Break a path into its components (parent, leaf, etc.)",
        "kind": "powershell",
        "template": "Split-Path -Path '{path}'",
        "slots": ["path"],
        "reversible": False,
    },
    "EXPORT_FOLDER_LISTING_CSV": {
        "description": "Export a folder's file listing to a CSV file in the same folder",
        "kind": "powershell",
        "template": "Get-ChildItem -Path '{path}' | Export-Csv -Path '{path}\\listing.csv' -NoTypeInformation",
        "slots": ["path"],
        "reversible": True,
    },
    "COUNT_FILES": {
        "description": "Count how many files are in a folder (recursively)",
        "kind": "powershell",
        "template": "Get-ChildItem -Path '{path}' -Recurse -File | Measure-Object | Select-Object Count",
        "slots": ["path"],
        "reversible": False,
    },
    "COUNT_FOLDERS": {
        "description": "Count how many subfolders are in a folder (recursively)",
        "kind": "powershell",
        "template": "Get-ChildItem -Path '{path}' -Recurse -Directory | Measure-Object | Select-Object Count",
        "slots": ["path"],
        "reversible": False,
    },
    "FILE_TYPE_BREAKDOWN": {
        "description": "Show a breakdown of file types/extensions in a folder",
        "kind": "powershell",
        "template": "Get-ChildItem -Path '{path}' -Recurse -File | Group-Object Extension -NoElement | Sort-Object Count -Descending",
        "slots": ["path"],
        "reversible": False,
    },
    "FIND_DUPLICATE_FILES": {
        "description": "Find duplicate files in a folder by comparing size and hash",
        "kind": "powershell",
        "template": (
            "Get-ChildItem -Path '{path}' -Recurse -File | Group-Object -Property Length | "
            "Where-Object {{$_.Count -gt 1}} | ForEach-Object {{$_.Group | ForEach-Object "
            "{{$_ | Add-Member -NotePropertyName Hash -NotePropertyValue "
            "(Get-FileHash $_.FullName -Algorithm MD5).Hash -PassThru}}}} | "
            "Group-Object -Property Hash | Where-Object {{$_.Count -gt 1}} | "
            "ForEach-Object {{$_.Group | Select-Object FullName, Length, Hash}}"
        ),
        "slots": ["path"],
        "reversible": False,
    },
    "FIND_FILES_BY_CONTENT": {
        "description": "Search inside files under a folder for a text pattern (like grep)",
        "kind": "powershell",
        "template": "Get-ChildItem -Path '{path}' -Recurse -File | Select-String -Pattern '{pattern}'",
        "slots": ["path", "pattern"],
        "reversible": False,
    },
    "GET_CLIPBOARD": {
        "description": "Show the current text on the clipboard",
        "kind": "powershell",
        "template": "Get-Clipboard",
        "slots": [],
        "reversible": False,
    },
    "SET_CLIPBOARD": {
        "description": "Copy text onto the clipboard",
        "kind": "powershell",
        "template": "Set-Clipboard -Value '{value}'",
        "slots": ["value"],
        "reversible": False,
    },
    "SAVE_CLIPBOARD_TO_FILE": {
        "description": "Save the current clipboard text to a file (defaults to a timestamped .md file on the Desktop)",
        "kind": "api",
        "api": "clipboardfile",
        "action": "save_clipboard_to_file",
        "slots": ["filename", "extension"],
        "reversible": False,
    },
    "GENERATE_QR_CODE": {
        "description": "Generate a QR code image for some text/link (falls back to the current clipboard contents if nothing else is given)",
        "kind": "api",
        "api": "qrcode",
        "action": "generate_qr_code",
        "slots": ["content", "filename"],
        "reversible": False,
    },
    "SCAN_QR_CODE": {
        "description": "Read/decode the QR code in the currently selected image file",
        "kind": "api",
        "api": "qrcode",
        "action": "scan_qr_code",
        "slots": [],
        "reversible": False,
    },

    # ── Process ──────────────────────────────────────────────────────────────
    "WAIT_FOR_PROCESS": {
        "description": "Wait until a named process stops running",
        "kind": "powershell",
        "template": "Wait-Process -Name '{process_name}'",
        "slots": ["process_name"],
        "reversible": False,
    },
    "FIND_PROCESS": {
        "description": "Show info about one specific running process by name",
        "kind": "powershell",
        "template": "Get-Process -Name '{process_name}'",
        "slots": ["process_name"],
        "reversible": False,
    },
    "TOP_PROCESSES_BY_CPU": {
        "description": "Show the top N processes sorted by CPU usage",
        "kind": "powershell",
        "template": "Get-Process | Sort-Object CPU -Descending | Select-Object -First {count} Name, CPU, WorkingSet, Id",
        "slots": ["count"],
        "reversible": False,
    },
    "OPEN_TASK_MANAGER": {
        "description": "Open the Windows Task Manager window",
        "kind": "powershell",
        "template": "Start-Process taskmgr",
        "slots": [],
        "reversible": False,
    },
    "LIST_SCHEDULED_TASKS": {
        "description": "List all scheduled tasks with their last run time and result",
        "kind": "powershell",
        "template": "Get-ScheduledTask | Get-ScheduledTaskInfo | Select-Object TaskName, LastRunTime, LastTaskResult",
        "slots": [],
        "reversible": False,
    },

    # ── System info ──────────────────────────────────────────────────────────
    "SYSTEM_UPTIME": {
        "description": "Show how long the system has been running since last boot",
        "kind": "powershell",
        "template": "Get-Uptime",
        "slots": [],
        "reversible": False,
    },
    "HOSTNAME": {
        "description": "Show the computer's hostname",
        "kind": "powershell",
        "template": "hostname",
        "slots": [],
        "reversible": False,
    },
    "FIND_SERVICE": {
        "description": "Show the status of a named Windows service",
        "kind": "powershell",
        "template": "Get-Service -Name '{service_name}'",
        "slots": ["service_name"],
        "reversible": False,
    },
    "LIST_PRINTERS": {
        "description": "List installed printers",
        "kind": "powershell",
        "template": "Get-Printer | Select-Object Name, DriverName, PortName, Shared",
        "slots": [],
        "reversible": False,
    },
    "SYSTEM_LOCALE": {
        "description": "Show the system's locale/language/region settings",
        "kind": "powershell",
        "template": "Get-WinSystemLocale | Select-Object Name, DisplayName, LCID",
        "slots": [],
        "reversible": False,
    },
    "LIST_USB_DEVICES": {
        "description": "List currently connected USB devices",
        "kind": "powershell",
        "template": "Get-WmiObject Win32_USBControllerDevice | ForEach-Object {{[WMI]$_.Dependent}} | Select-Object Name, DeviceID",
        "slots": [],
        "reversible": False,
    },
    "TEMPERATURE_SENSORS": {
        "description": "Read available temperature sensor data",
        "kind": "powershell",
        "template": "Get-WmiObject MSAcpi_ThermalZoneTemperature -Namespace root/wmi | Select-Object CurrentTemperature",
        "slots": [],
        "reversible": False,
    },

    # ── System — media/power utilities (added this session) ────────────────
    # Every command below was checked against real PowerShell/.NET
    # documentation before being added, not assumed from memory -- see this
    # session's research. All are zero-dependency: no extra PowerShell
    # module, no downloaded utility, nothing beyond what ships with Windows
    # + .NET Framework (which is preinstalled on every realistic demo
    # machine). That constraint ruled out volume SET-TO-A-SPECIFIC-PERCENT
    # (Windows/PowerShell genuinely has no native API for that -- every real
    # solution needs either a third-party module like AudioDeviceCmdlets or
    # a separate download like NirCmd) and it's deliberately left out rather
    # than shipped as something that would silently fail on a machine
    # without that extra install. Toggle-style mute/volume-step, by
    # contrast, works natively via a simulated media key press
    # (WScript.Shell SendKeys with the volume virtual-key char codes), so
    # those made the cut.
    #
    # Also deliberately NOT added: sleep/shutdown/hibernate. Those are one
    # keystroke from disrupting a live demo or losing unsaved work with no
    # undo, which cuts against this app's own "reversible where possible,
    # sandboxed, no destructive surprises" design -- LOCK_WORKSTATION gives
    # the same "look, TOKI can control the OS" demo moment without that risk.
    "TOGGLE_MUTE": {
        "description": "Mute or unmute the system volume",
        "kind": "powershell",
        "template": "(New-Object -ComObject WScript.Shell).SendKeys([char]173)",
        "slots": [],
        "reversible": True,  # running it again toggles back
    },
    "VOLUME_UP": {
        "description": "Increase the system volume by one step",
        "kind": "powershell",
        "template": "(New-Object -ComObject WScript.Shell).SendKeys([char]175)",
        "slots": [],
        "reversible": True,
    },
    "VOLUME_DOWN": {
        "description": "Decrease the system volume by one step",
        "kind": "powershell",
        "template": "(New-Object -ComObject WScript.Shell).SendKeys([char]174)",
        "slots": [],
        "reversible": True,
    },
    "TAKE_SCREENSHOT": {
        "description": "Capture the screen and save it as an image file to the Desktop",
        "kind": "powershell",
        "template": (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
            "$s = [Windows.Forms.SystemInformation]::VirtualScreen; "
            "$bmp = New-Object System.Drawing.Bitmap $s.Width, $s.Height; "
            "$g = [System.Drawing.Graphics]::FromImage($bmp); "
            "$g.CopyFromScreen($s.Left, $s.Top, 0, 0, $bmp.Size); "
            "$path = Join-Path ([Environment]::GetFolderPath('Desktop')) "
            "\"screenshot_$(Get-Date -Format yyyyMMdd_HHmmss).png\"; "
            "$bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png); "
            "$g.Dispose(); $bmp.Dispose(); Write-Output $path"
        ),
        "slots": [],
        "reversible": False,  # file lands in the sandboxed Desktop, but the capture itself can't be "undone"
    },
    "LOCK_WORKSTATION": {
        "description": "Lock the computer (goes to the Windows lock screen)",
        "kind": "powershell",
        "template": "rundll32.exe user32.dll,LockWorkStation",
        "slots": [],
        "reversible": False,  # unlocking isn't something TOKI does -- the user unlocks their own PC
    },
    "BATTERY_STATUS": {
        "description": "Show battery charge percentage and estimated time remaining, if a battery is present",
        "kind": "powershell",
        "template": (
            "$b = Get-CimInstance -ClassName Win32_Battery; "
            "if ($b) {{ $b | Select-Object EstimatedChargeRemaining, BatteryStatus, EstimatedRunTime }} "
            "else {{ Write-Output 'No battery detected (desktop or VM).' }}"
        ),
        "slots": [],
        "reversible": False,
    },
    "EMPTY_RECYCLE_BIN": {
        "description": "Empty the Recycle Bin",
        "kind": "powershell",
        # -Force matches this app's existing no-confirmation-dialog design
        # (see README/orchestrator notes: safety comes from the sandbox +
        # Stop button, not per-action prompts) rather than leaving the
        # cmdlet's default interactive Y/N prompt, which would hang waiting
        # for input RunningCommand never sends.
        "template": "Clear-RecycleBin -Force",
        "slots": [],
        "reversible": False,
    },
}

INTENTS_EXTENDED_NAMES = list(INTENTS_EXTENDED.keys())
