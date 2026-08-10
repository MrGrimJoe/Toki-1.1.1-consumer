"""
intents.py — the closed vocabulary the model classifies into.

This is the core fix over every previous design: the model never writes a
command or a JSON shape for variables. It picks exactly ONE word from this
list. Everything after that (which variables, what values) is filled in by
plain Python — regex/heuristic extraction straight from the user's own
message — never by asking the model to improvise a shape.

Each intent has:
  - description: shown to the model in the system prompt (keep to one line)
  - kind: "powershell" | "api" | "chat"
  - template: PowerShell command with {slot} placeholders (kind=powershell)
  - api / action: which ToolAPI method to call (kind=api)
  - slots: which variable names extract_slots() must fill
  - reversible: whether the undo-style safety net applies (mostly unused now
    that we're going stop-button-first, but kept so destructive ops are at
    least flagged in the UI)
"""

from typing import Dict, Any

INTENTS: Dict[str, Dict[str, Any]] = {

    # ── Filesystem — sandboxed to D:\ and Desktop only ─────────────────────
    "MAKE_FOLDER": {
        "description": "Create a new folder/directory",
        "kind": "powershell",
        "template": "New-Item -Path '{path}' -ItemType Directory -Force",
        "slots": ["path"],
        "reversible": True,
    },
    "MAKE_FILE": {
        "description": "Create a new empty file",
        "kind": "powershell",
        "template": "New-Item -Path '{path}' -ItemType File -Force",
        "slots": ["path"],
        "reversible": True,
    },
    "DELETE_ITEM": {
        "description": "Delete a file or folder (sends to Recycle Bin, not permanent)",
        "kind": "powershell",
        "template": "$shell = New-Object -ComObject Shell.Application; "
                    "$item = Get-Item -LiteralPath '{path}'; "
                    "$folder = $shell.Namespace($item.DirectoryName); "
                    "$fileObj = $folder.ParseName($item.Name); "
                    "$fileObj.InvokeVerb('delete')",
        "slots": ["path"],
        "reversible": False,   # goes to Recycle Bin, so recoverable manually, but no in-app undo
    },
    "RENAME_ITEM": {
        "description": "Rename a file or folder",
        "kind": "powershell",
        "template": "Rename-Item -LiteralPath '{path}' -NewName '{new_name}'",
        "slots": ["path", "new_name"],
        "reversible": True,
    },
    "MOVE_ITEM": {
        "description": "Move a file or folder to another location",
        "kind": "powershell",
        "template": "Move-Item -LiteralPath '{path}' -Destination '{dest}'",
        "slots": ["path", "dest"],
        "reversible": True,
    },
    "COPY_ITEM": {
        "description": "Copy a file or folder",
        "kind": "powershell",
        "template": "Copy-Item -LiteralPath '{path}' -Destination '{dest}' -Recurse -Force",
        "slots": ["path", "dest"],
        "reversible": True,
    },
    "LIST_FILES": {
        "description": "List files/folders in a directory",
        "kind": "powershell",
        "template": "Get-ChildItem -Path '{path}' | Select-Object Name,Length,LastWriteTime",
        "slots": ["path"],
        "reversible": False,
    },
    "SORT_FOLDER_BY_TYPE": {
        "description": "Organize the top-level files in a folder into type subfolders (Images/Documents/Archives/Audio/Video/Other)",
        "kind": "powershell",
        # Only top-level files (-File, non-recursive) are touched, and .lnk
        # shortcuts are skipped so desktop icons aren't disturbed. The
        # destination is always a subfolder freshly created UNDER the
        # already-sandbox-checked {path} itself, so it can't escape the
        # sandbox even though it's computed inside PowerShell rather than
        # passed as its own extracted slot. Every literal PowerShell brace
        # below (hashtable literal, script blocks) is doubled so Python's
        # str.format() doesn't choke on it -- see BETA 0.3.37 checkpoint 1's
        # brace-escaping fix for the exact failure mode this avoids.
        "template": (
            "$map=@{{'.jpg'='Images';'.jpeg'='Images';'.png'='Images';'.gif'='Images';'.bmp'='Images';"
            "'.pdf'='Documents';'.doc'='Documents';'.docx'='Documents';'.txt'='Documents';'.xls'='Documents';"
            "'.xlsx'='Documents';'.ppt'='Documents';'.pptx'='Documents';'.zip'='Archives';'.rar'='Archives';"
            "'.7z'='Archives';'.mp3'='Audio';'.wav'='Audio';'.mp4'='Video';'.mov'='Video';'.avi'='Video'}}; "
            "Get-ChildItem -Path '{path}' -File | Where-Object {{ $_.Extension -ne '.lnk' }} | ForEach-Object {{ "
            "$cat = $map[$_.Extension.ToLower()]; if (-not $cat) {{ $cat = 'Other' }}; "
            "$dest = Join-Path '{path}' $cat; if (-not (Test-Path $dest)) {{ New-Item -Path $dest -ItemType Directory -Force | Out-Null }}; "
            "Move-Item -LiteralPath $_.FullName -Destination $dest -Force }}"
        ),
        "slots": ["path"],
        "reversible": True,
    },
    "FIND_FILES": {
        "description": "Search for files by name within the sandbox",
        "kind": "powershell",
        "template": "Get-ChildItem -Path '{root}' -Recurse -Filter '*{query}*' "
                    "-ErrorAction SilentlyContinue | Select-Object FullName",
        "slots": ["root", "query"],
        "reversible": False,
    },
    "READ_FILE": {
        "description": "Show the contents of a text file",
        "kind": "powershell",
        "template": "Get-Content -LiteralPath '{path}' -TotalCount 200",
        "slots": ["path"],
        "reversible": False,
    },
    "OPEN_ITEM": {
        "description": "Open a file or folder with its default program",
        "kind": "powershell",
        # -FilePath, not -LiteralPath -- Start-Process has no -LiteralPath
        # parameter at all (that's a filesystem-provider cmdlet parameter,
        # e.g. Get-Item/Remove-Item/Copy-Item; likely copy-pasted from one
        # of those into this template originally). Confirmed live: this
        # errored on every single invocation with "A parameter cannot be
        # found that matches parameter name 'LiteralPath'" -- OPEN_ITEM
        # could never have worked, not even once, regardless of routing.
        "template": "Start-Process -FilePath '{path}'",
        "slots": ["path"],
        "reversible": False,
    },
    "DISK_USAGE": {
        "description": "Show free/used space on available drives",
        "kind": "powershell",
        "template": "Get-PSDrive -PSProvider FileSystem | "
                    "Select-Object Name,@{{N='UsedGB';E={{[math]::Round($_.Used/1GB,1)}}}},"
                    "@{{N='FreeGB';E={{[math]::Round($_.Free/1GB,1)}}}}",
        "slots": [],
        "reversible": False,
    },

    # ── Processes / system ──────────────────────────────────────────────────
    "PROCESS_LIST": {
        "description": "List currently running processes",
        "kind": "powershell",
        "template": "Get-Process | Select-Object Name,Id,CPU | Sort-Object CPU -Descending "
                    "| Select-Object -First 20",
        "slots": [],
        "reversible": False,
    },
    "KILL_PROCESS": {
        "description": "Stop/close a running process by name",
        "kind": "powershell",
        "template": "Stop-Process -Name '{process}' -Force -ErrorAction SilentlyContinue",
        "slots": ["process"],
        "reversible": False,
    },
    "SYSTEM_INFO": {
        "description": "Show basic system info (OS version, memory)",
        "kind": "powershell",
        "template": "Get-ComputerInfo | Select-Object WindowsVersion,OsTotalVisibleMemorySize,OsFreePhysicalMemory",
        "slots": [],
        "reversible": False,
    },
    "NETWORK_INFO": {
        "description": "Show local network/IP info",
        "kind": "powershell",
        "template": "Get-NetIPAddress | Where-Object {{$_.AddressFamily -eq 'IPv4'}} "
                    "| Select-Object IPAddress,InterfaceAlias",
        "slots": [],
        "reversible": False,
    },
    "CURRENT_USER": {
        "description": "Show the current Windows username",
        "kind": "powershell",
        "template": "$env:USERNAME",
        "slots": [],
        "reversible": False,
    },

    # ── Info APIs (no PowerShell involved) ──────────────────────────────────
    "GET_TIME": {
        "description": "What time is it right now",
        "kind": "api", "api": "time", "action": "get_time",
        "slots": [],
        "reversible": False,
    },
    "GET_DATE": {
        "description": "What's today's date",
        "kind": "api", "api": "time", "action": "get_date",
        "slots": [],
        "reversible": False,
    },
    "GET_WEATHER": {
        "description": "Current weather (uses the user's cached location unless a city is named)",
        "kind": "api", "api": "weather", "action": "get_weather",
        "slots": ["city"],
        "reversible": False,
    },
    "GET_FORECAST": {
        "description": "Multi-day weather forecast",
        "kind": "api", "api": "weather", "action": "get_forecast",
        "slots": ["city", "days"],
        "reversible": False,
    },
    "SEARCH_WEB": {
        "description": "Search the web for something",
        "kind": "api", "api": "websearch", "action": "search",
        "slots": ["query"],
        "reversible": False,
    },
    "GET_LOCATION": {
        "description": "Where the user currently is (cached, IP-based)",
        "kind": "api", "api": "location", "action": "get_location",
        "slots": [],
        "reversible": False,
    },

    # ── Scheduling & conditionals ────────────────────────────────────────────
    # These two intents are deliberately reached BEFORE graph classification,
    # not after a graph miss -- see extractor.py's find_time_expression()/
    # looks_conditional() module docstrings for the real bug this fixes
    # (the graph used to silently content-word-match "open notepad at 3pm"
    # to plain OPEN_ITEM, dropping "at 3pm" entirely). orchestrator.py's
    # _process_single_request() checks for these BEFORE calling
    # self.graph_router.classify() at all.
    "SCHEDULE_COMMAND": {
        "description": "Run a command after a delay, or at a specific time",
        "kind": "schedule",
        "slots": ["command_text", "delay_seconds"],
        "reversible": True,  # cancellable before it fires -- see scheduler.py
    },
    "CANCEL_SCHEDULED": {
        "description": "Cancel a previously scheduled command or watched condition",
        "kind": "cancel_scheduled",
        "slots": ["ref"],
        "reversible": False,
    },
    "CONDITIONAL_COMMAND": {
        "description": "Do something only if a condition becomes true (\"if X, do Y\")",
        "kind": "conditional",
        "slots": [],  # deliberately empty here -- the real condition/action
                      # pair only exists after the MISSING_SLOT_QUESTIONS
                      # follow-up; see extractor.py's CONDITIONAL_COMMAND
                      # branches and condition_checker.py's scope note on
                      # which conditions can actually be monitored today.
        "reversible": True,  # cancellable while still watching -- see condition_checker.py
    },

    # ── Selected-file conversion — acts on selection_context.py's current
    #    selection, never on a path the model made up. See extractor.py's
    #    resolve_selected_file_target() and apis.py's FileConvertAPI. ─────────
    "CONVERT_SELECTED_FILE": {
        "description": "Convert the currently selected file to a different format (e.g. json to text, png to jpg)",
        "kind": "api",
        "api": "fileconvert",
        "action": "convert_selected",
        "slots": ["target_format"],
        "reversible": False,  # writes a new file alongside the original by default, never overwrites
    },
    "RESIZE_SELECTED_FILE": {
        "description": "Resize/shrink/enlarge the currently selected image",
        "kind": "api",
        "api": "fileconvert",
        "action": "resize_selected",
        "slots": ["width", "height", "scale"],
        "reversible": False,
    },
    "COMPRESS_SELECTED_FILE": {
        "description": "Compress the currently selected file or folder to reduce its size",
        "kind": "api",
        "api": "fileconvert",
        "action": "compress_selected",
        "slots": ["quality"],
        "reversible": False,
    },
    "EXTRACT_SELECTED_FILE": {
        "description": "Extract/unzip the currently selected archive",
        "kind": "api",
        "api": "fileconvert",
        "action": "extract_selected",
        "slots": [],
        "reversible": False,
    },

    # ── Fallback ─────────────────────────────────────────────────────────────
    "CHAT": {
        "description": "Anything else — just answer conversationally, no action needed",
        "kind": "chat",
        "slots": [],
        "reversible": False,
    },
    "ASK_CONTEXT": {
        "description": (
            "The user clearly wants something done or looked up, but left out "
            "the detail needed to know what — ask a short clarifying question "
            "instead of guessing or just chatting"
        ),
        "kind": "ask_context",
        "slots": [],
        "reversible": False,
    },
}

# Handy for building the JSON schema / prompt list once, elsewhere.
INTENT_NAMES = list(INTENTS.keys())
