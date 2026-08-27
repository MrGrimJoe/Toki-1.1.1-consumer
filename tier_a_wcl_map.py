"""
tier_a_wcl_map.py -- Tier A intent -> WCL cmdlet equivalence map.

Groundwork for priority.md #11 ("Destructive Tier B commands get silently
shadowed by harmless Tier A ones"). Confirmed live and in sandbox:
"wipe disk 2" -> DISK_USAGE (should be Clear-Disk, destructive),
"bitlocker lock mount point D" -> LOCK_WORKSTATION (should be
Lock-BitLocker, destructive), "disable dedup volume on E" -> VOLUME_UP
(should be Disable-DedupVolume, destructive). Root cause: graph_router.py
(Tier A) and wcl_resolver.py (Tier B/WCL) are two separate, purpose-built
graphs (see wcl_resolver.py's module docstring), and orchestrator.py tries
GraphRouter FIRST -- a confident Tier A word-overlap hit wins and
WCLResolver never even gets consulted, regardless of whether the WCL
answer would have been the real, destructive one.

This module answers a narrower question than "is Tier A's answer wrong":
given a Tier A intent that graph_router.py already confidently picked, and
a competing WCL cmdlet that wcl_resolver.py separately resolves for the
SAME query, are these the same real-world action (just catalogued/
implemented differently), or genuinely different actions where Tier A's
confident pick would be silently wrong?

Cmdlet-identity is the right equivalence signal, not danger_level alone --
found while cross-referencing: 13 of Tier A's 21 originally-checked
intents share their exact executing cmdlet with a WCL entry, including
KILL_PROCESS -> Stop-Process (WCL marks this "destructive") and
COPY_ITEM/MOVE_ITEM/RENAME_ITEM (WCL marks these "caution"). None of
those are shadowing bugs -- they're the SAME action, cross-listed in two
data sources that independently rate risk. An earlier "defer whenever WCL
says caution/destructive" rule would have nulled out huge swaths of core,
well-tested Tier A functionality on that basis alone.

TIER_A_TO_WCL_CMDLETS maps each Tier A intent name to the set of WCL
cmdlet names considered equivalent (case-insensitive comparison expected
by callers) -- i.e. "if the WCL side resolves to one of these, trust
Tier A's answer, don't flag it as shadowing." An empty set means "no
known equivalent -- ANY resolved WCL match for this query is worth
flagging."

How each entry was derived:
  1. Primary cmdlet extracted via regex from the intent's own
     orchestrator.py `template` string (intents.py / intents_extended.py /
     intents_app_control.py), skipping known incidental/helper cmdlets
     (New-Object, Add-Type, Select-Object, Sort-Object, Where-Object,
     Group-Object, ForEach-Object, Measure-Object, Write-Output,
     Add-Member -- these appear in templates as scaffolding, never as the
     action the intent is actually named for).
  2. Manually corrected where the regex's first non-helper match still
     wasn't the real action:
       - DELETE_ITEM's template calls Get-Item then an InvokeVerb('delete')
         COM call (send to Recycle Bin) -- Get-Item is just a lookup, not
         the delete itself. Real-world equivalent action is Remove-Item
         (and its aliases ri/rm/del/erase) -- DELETE_ITEM is deliberately
         the SAFER (recoverable, Recycle-Bin) version of that same
         real-world action, so it's treated as equivalent rather than
         flagged, to avoid nagging on the single most common delete
         phrasing. A user who explicitly wants the permanent/bypass-
         Recycle-Bin behavior isn't served by either path today -- that's
         a separate, smaller gap (no PERMANENT_DELETE intent exists),
         not this one.
       - TAKE_SCREENSHOT's regex first-match is Join-Path (used only to
         build the output file's path string) -- not a real equivalence
         signal, corrected to no-cmdlet.
  3. Intents with no real cmdlet at all (COM SendKeys calls, bare shell
     commands, env vars, API/chat/schedule kinds) map to an empty set:
     CURRENT_USER, HOSTNAME, LOCK_WORKSTATION, TOGGLE_MUTE, VOLUME_UP,
     VOLUME_DOWN, GET_TIME, GET_DATE, GET_WEATHER, GET_FORECAST,
     SEARCH_WEB, GET_LOCATION, SCHEDULE_COMMAND, CANCEL_SCHEDULED,
     CONDITIONAL_COMMAND, CHAT, ASK_CONTEXT, SET_TIMER.

Verified against the real graph DB in test_tier_a_wcl_map.py -- run that
before trusting any future edit to this file.
"""
from typing import Dict, FrozenSet

# name -> set of equivalent WCL cmdlet names (case-insensitive compare
# left to callers). Empty set == no known equivalent.
TIER_A_TO_WCL_CMDLETS: Dict[str, FrozenSet[str]] = {
    "MAKE_FOLDER": frozenset({"New-Item"}),
    "MAKE_FILE": frozenset({"New-Item"}),
    "DELETE_ITEM": frozenset({"Remove-Item", "ri", "rm", "del", "erase"}),
    "RENAME_ITEM": frozenset({"Rename-Item"}),
    "MOVE_ITEM": frozenset({"Move-Item", "move", "mi"}),
    "COPY_ITEM": frozenset({"Copy-Item"}),
    "LIST_FILES": frozenset({"Get-ChildItem"}),
    "SORT_FOLDER_BY_TYPE": frozenset({"Get-ChildItem", "Move-Item", "New-Item"}),
    "FIND_FILES": frozenset({"Get-ChildItem"}),
    "READ_FILE": frozenset({"Get-Content"}),
    "OPEN_ITEM": frozenset({"Start-Process", "Invoke-Item"}),
    "DISK_USAGE": frozenset({"Get-PSDrive"}),
    "PROCESS_LIST": frozenset({"Get-Process"}),
    "KILL_PROCESS": frozenset({"Stop-Process"}),
    "SYSTEM_INFO": frozenset({"Get-ComputerInfo"}),
    "NETWORK_INFO": frozenset({"Get-NetIPAddress"}),
    "CURRENT_USER": frozenset(),
    "GET_TIME": frozenset(),
    "GET_DATE": frozenset(),
    "GET_WEATHER": frozenset(),
    "GET_FORECAST": frozenset(),
    "SEARCH_WEB": frozenset(),
    "GET_LOCATION": frozenset(),
    # BETA 0.3.41 -- kind="api" like GET_WEATHER/SEARCH_WEB above, not
    # "powershell", so there's no cmdlet at all to compare against. These
    # act on selection_context.py's current selection through
    # conversion_engine/, entirely in-process (Pillow/stdlib/pandoc), never
    # by shelling out to a PowerShell cmdlet -- so an empty set is exactly
    # right, not a placeholder.
    "CONVERT_SELECTED_FILE": frozenset(),
    "RESIZE_SELECTED_FILE": frozenset(),
    "COMPRESS_SELECTED_FILE": frozenset(),
    "EXTRACT_SELECTED_FILE": frozenset(),
    "DOWNLOAD_PLAYING_VIDEO": frozenset(),
    "DOWNLOAD_VIDEO_URL": frozenset(),
    # BETA 0.3.44 checkpoint 4 -- kind="api" like the conversion_engine/
    # video-download intents just above, not "powershell". ORGANIZE_FILES_
    # BY_TOPIC's actual move happens via Python's own shutil.move() inside
    # file_graph/organizer.py (not a PowerShell Move-Item template) so its
    # scoring engine can decide per-file, per-candidate-folder confidence
    # first; GROUP_FILES_BY_EXTENSION similarly moves via shutil.move()
    # inside file_grouping.py. Neither shells out to a cmdlet, so an empty
    # set is exactly right, not a placeholder.
    "ORGANIZE_FILES_BY_TOPIC": frozenset(),
    "GROUP_FILES_BY_EXTENSION": frozenset(),
    "SCHEDULE_COMMAND": frozenset(),
    "CANCEL_SCHEDULED": frozenset(),
    "SET_TIMER": frozenset(),
    "CONDITIONAL_COMMAND": frozenset(),
    "CHAT": frozenset(),
    "ASK_CONTEXT": frozenset(),
    "PATH_EXISTS": frozenset({"Test-Path"}),
    "ITEM_PROPERTIES": frozenset({"Get-ItemProperty"}),
    "CURRENT_LOCATION": frozenset({"Get-Location"}),
    "RESOLVE_PATH": frozenset({"Resolve-Path"}),
    "SPLIT_PATH": frozenset({"Split-Path"}),
    "EXPORT_FOLDER_LISTING_CSV": frozenset({"Get-ChildItem", "Export-Csv"}),
    "COUNT_FILES": frozenset({"Get-ChildItem"}),
    "COUNT_FOLDERS": frozenset({"Get-ChildItem"}),
    "FILE_TYPE_BREAKDOWN": frozenset({"Get-ChildItem"}),
    # Get-FileHash checked against windows_command_library.json directly:
    # not present there (confirmed by test_tier_a_wcl_map.py), so it's
    # left out of the equivalence set on purpose -- only Get-ChildItem is
    # a name wcl_resolver.py could ever actually return.
    "FIND_DUPLICATE_FILES": frozenset({"Get-ChildItem"}),
    # Select-String checked directly against windows_command_library.json:
    # not present there either -- same reasoning as Get-FileHash above.
    "FIND_FILES_BY_CONTENT": frozenset({"Get-ChildItem"}),
    "GET_CLIPBOARD": frozenset({"Get-Clipboard"}),
    "SET_CLIPBOARD": frozenset({"Set-Clipboard"}),
    # kind="api" like GET_WEATHER/CONVERT_SELECTED_FILE above -- plain
    # Python (clip_qr.py), no PowerShell cmdlet involved at all.
    "SAVE_CLIPBOARD_TO_FILE": frozenset(),
    "GENERATE_QR_CODE": frozenset(),
    "SCAN_QR_CODE": frozenset(),
    "WAIT_FOR_PROCESS": frozenset({"Wait-Process"}),
    "FIND_PROCESS": frozenset({"Get-Process"}),
    "TOP_PROCESSES_BY_CPU": frozenset({"Get-Process"}),
    "OPEN_TASK_MANAGER": frozenset({"Start-Process"}),
    "LIST_SCHEDULED_TASKS": frozenset({"Get-ScheduledTask", "Get-ScheduledTaskInfo"}),
    "SYSTEM_UPTIME": frozenset({"Get-Uptime"}),
    "HOSTNAME": frozenset(),
    "FIND_SERVICE": frozenset({"Get-Service"}),
    # Get-Printer and Get-WinSystemLocale: both directly re-verified
    # absent from windows_command_library.json (confirmed twice, after
    # an initial false "confirmed absent" claim turned out to be a bash
    # one-liner's own bug, not a real contradiction -- re-checked
    # carefully with a repr() dump before concluding either way). Left
    # as empty sets; safe by construction since test_tier_a_wcl_map.py's
    # test_wcl_json_actually_contains_every_mapped_cmdlet would fail loudly
    # if either name were ever added to the WCL data under a different
    # spelling this map still pointed at.
    "LIST_PRINTERS": frozenset(),
    "SYSTEM_LOCALE": frozenset(),
    "LIST_USB_DEVICES": frozenset({"Get-WmiObject"}),
    "TEMPERATURE_SENSORS": frozenset({"Get-WmiObject"}),
    "TOGGLE_MUTE": frozenset(),
    "VOLUME_UP": frozenset(),
    "VOLUME_DOWN": frozenset(),
    "TAKE_SCREENSHOT": frozenset(),
    "LOCK_WORKSTATION": frozenset(),
    "BATTERY_STATUS": frozenset({"Get-CimInstance"}),
    "EMPTY_RECYCLE_BIN": frozenset({"Clear-RecycleBin"}),
}


def is_equivalent(intent: str, wcl_cmdlet: str) -> bool:
    """True if wcl_cmdlet is a known equivalent real-world action for
    this Tier A intent (case-insensitive) -- i.e. NOT a shadowing case.
    Unknown intents (e.g. WCL_-prefixed dynamic ones, which never come
    from Tier A anyway) conservatively return False."""
    if not wcl_cmdlet:
        return False
    equivalents = TIER_A_TO_WCL_CMDLETS.get(intent)
    if not equivalents:
        return False
    wcl_lower = wcl_cmdlet.lower()
    return any(wcl_lower == e.lower() for e in equivalents)
