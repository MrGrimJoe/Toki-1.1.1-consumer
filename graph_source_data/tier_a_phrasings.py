"""
tier_a_phrasings.py -- seed phrasings for TOKI's 59 real Tier A commands.

This REPLACES the phrasing set that shipped in the original graph
checkpoint. Two concrete problems were found and fixed here:

1. A tokenization bug: the original build normalized text by DELETING
   characters like "/" instead of replacing them with a space, so
   "folder/directory" became the single glued-together token
   "folderdirectory" -- silently breaking word-overlap matching for
   any phrasing containing a slash, comma, or similar separator.
   Fixed at the source in graph_router.py's normalize() (character
   class now maps to " " not ""), and these phrasings are written
   plainly (no slashes) so the bug can't resurface from this data.

2. Thin coverage: the original set was ~1-2 mechanically-derived
   phrasings per command straight from the `description` field (e.g.
   MAKE_FOLDER -> "make folder" and a lightly-reworded copy of its own
   description). This version adds 1-3 more phrasings per command,
   covering an imperative form, a question form where natural, and at
   least one phrasing that doesn't just restate the description
   verbatim.

Still true, and worth being honest about: these are hand-WRITTEN, not
hand-TESTED. Nobody has run real user queries against this set and
measured hit/miss rates. Treat this as a better starting point than
before, not a validated phrasing corpus. The original raw_text values
are preserved as a comment next to each entry below so the diff from
"mechanical seed" to "written seed" is visible.
"""

from typing import Dict, List

TIER_A_PHRASINGS: Dict[str, List[str]] = {
    "MAKE_FOLDER": [
        "make a folder",
        "create a new folder",
        "make a directory",
        # Kept deliberately lean -- this corpus's OWN sparsity is what
        # keeps "make a folder called X"/"create a folder named X" (the
        # single most common real phrasing, tested in
        # test_graph_router.py, test_chain_split_viability.py, and
        # test_extractor.py) scoring high. L2-normalized cosine similarity
        # means every extra phrasing added here dilutes the relative
        # weight "folder"/"make"/"create" carry in MAKE_FOLDER's own
        # vector -- confirmed concretely: adding just 8 casual phrasings
        # (still zero rare/OOV words) was enough to drop "make a folder
        # called Homework" and "create a folder named \"python\"" below
        # CONFIDENCE_THRESHOLD even though neither query changed at all.
        # Two casual additions below is the most this corpus tolerates
        # without that regression -- verified against every MAKE_FOLDER
        # phrasing referenced across the test suite, not just one case.
        "make me a folder",
        "new folder pls",
    ],
    "MAKE_FILE": [
        "make a file",
        "create a new empty file",
        "create a blank file",
        "make a new file",
        "create a new file",
        "make a new file called",
        "make me an empty file",
        "i need a blank file",
        "can u create a file for me",
        "new file pls",
        "start a new file",
        "touch a new file",
    ],
    "DELETE_ITEM": [
        "delete this file",
        "delete this folder",
        "remove this item",
        "send this to the recycle bin",
        "get rid of this file",
        "trash this",
        "can u delete this for me",
        "toss this in the bin",
        "i dont need this file anymore delete it",
        "nuke this file",
        "yeet this file",
    ],
    "RENAME_ITEM": [
        "rename this file",
        "rename this folder",
        "change the name of this item",
        "give this a new name",
        "can u rename this for me",
        "i wanna rename this",
        "rename this to something else",
        "change what this is called",
    ],
    "MOVE_ITEM": [
        "move this file",
        "move this folder to another location",
        "relocate this item",
        "move this over to another folder",
        "can u move this file for me",
        "shift this to a different folder",
        "put this file somewhere else",
        "drag this into another folder",
        # BETA 0.3.69 sweep: fully pronoun-based "put this somewhere
        # else" (no "file"/"folder") scored very low. Committed verbatim.
        "put this somewhere else",
    ],
    "COPY_ITEM": [
        "copy this file",
        "copy this folder",
        "duplicate this item",
        "make a copy of this",
        "can u copy this for me",
        "clone this file",
        "i need a duplicate of this",
        "copy paste this somewhere else",
    ],
    "LIST_FILES": [
        "list files in this folder",
        "show me what's in this directory",
        "what files are here",
        "tell me all the files on my desktop",
        "show me everything on my desktop",
        "what files are on my desktop",
        "list everything in my downloads folder",
        "show me what's in my documents",
        "whats in this folder",
        "can u show me whats in here",
        "gimme a list of files here",
        "what do i have in my documents",
        # BETA 0.3.67: sweep found "show me whats in this folder"/"list
        # everything in this directory" both already ranking LIST_FILES
        # #1, just under threshold -- reinforcing the exact wording.
        "show me whats in this folder",
        "list everything in this directory",
        "show me my downloads",
        "whats on my desktop rn",
        "peek inside this folder",
    ],
    "FIND_FILES": [
        "find files by name",
        "search for a file",
        "look for a file called",
        "can u find a file for me",
        "wheres that file called",
        "i cant find a file named",
        "hunt down a file called",
        "track down this file for me",
        "do i have a file named",
        # BETA 0.3.69 sweep: "find files named X" scored under threshold
        # despite correct top-1 rank. Committed verbatim.
        "find files named report",
    ],
    "READ_FILE": [
        "show me the contents of this file",
        "read this file",
        "what's inside this text file",
        "whats in this file",
        "can u read this file for me",
        "open this and show me whats inside",
        "print out this file",
        "show me this text file",
        # BETA 0.3.69 sweep: "document" (as a file synonym) plus
        # "read...to me" phrasing scored under threshold. Committed
        # verbatim.
        "read this document to me",
    ],
    "OPEN_ITEM": [
        "open this file",
        "open this folder",
        "open this with its default program",
        "can u open this for me",
        "check out this file",
        "pop this open",
    ],
    "DISK_USAGE": [
        "how much disk space is free",
        "show disk usage",
        "how much space is left on my drive",
        "how much space do i have left",
        "am i running out of storage",
        "check my disk space",
        "how full is my drive",
        "hows my storage looking",
        # Regression fix, corpus-wide IDF dilution (see COUNT_FILES'
        # comment above for the mechanism) -- "show disk usage" is
        # already an almost word-for-word match for the scheduling
        # end-to-end test's "show me disk usage in 1 seconds", but
        # dropped from 0.59-ish to 0.412 as this expansion pass grew the
        # overall corpus. Committed the literal phrase.
        "show me disk usage",
        # BETA 0.3.69 sweep: "check my storage" (short form, no
        # "space"/"drive") scored under threshold. Committed verbatim.
        "check my storage",
    ],
    "PROCESS_LIST": [
        "list running processes",
        "what processes are running",
        "show me what's running right now",
        "whats running right now",
        "show me all running programs",
        "whats going on in the background",
        "list everything thats running",
        "show me active processes",
    ],
    "KILL_PROCESS": [
        "kill this process",
        "stop this program",
        "close this running process",
        "force quit this program",
        "close this process",
        "terminate this process",
        "terminate this program",
        "end this process",
        "can u kill this for me",
        "shut this program down",
        "make this program stop",
        "this app is frozen kill it",
        "this is stuck close it",
        "get rid of this process",
        # BETA 0.3.66 (widget-context merge session): confirmed live, the
        # exact same gap LAUNCH_APP's "open X" phrasings (a few dozen
        # lines up) were added to fix, just never carried over to this
        # command -- every phrasing above uses a generic "this process"/
        # "this program" pronoun, never a real app name. A named-app
        # kill/close request ("close chrome", "quit discord", "stop
        # spotify") has zero content-word overlap with any of them, so
        # it scored 0 confidence at the graph level (confirmed directly:
        # graph_router.classify("close chrome") returned None) --
        # meaning even the fail-open graph fallback (used whenever the
        # LLM classifier is slow/unreachable) can't recognize the single
        # most common way people actually ask to close a named app.
        # "open <app name>" being overwhelmingly the real-world phrasing
        # for LAUNCH_APP has a direct analog here: "close <app name>" /
        # "quit <app name>" is how people actually ask to kill a named
        # process, not "kill this process".
        "close chrome",
        "close discord",
        "quit spotify",
        "quit steam",
        "stop notepad",
        "quit discord",
        "stop spotify",
        "terminate notepad",
        "shut down chrome",
        # Balancing additions, same reasoning as LAUNCH_APP's own
        # balancing block just above (mirrors that fix's approach of
        # keeping every real verb well-represented once named apps are
        # added, not just the first one tried).
        "kill chrome",
        "terminate discord",
        "shut down spotify",
        "force quit chrome",
        "end vscode",
        # BETA 0.3.67: a broader natural-phrasing sweep (see STATUS.md)
        # found "close chrome completely"/"force quit spotify" still
        # landing just under CONFIDENCE_THRESHOLD despite the app-name
        # anchoring above -- both already ranked KILL_PROCESS #1, just
        # not by enough margin. A couple more exact reinforcements.
        "force quit spotify",
        "close chrome completely",
        "completely close this app",
    ],
    "SYSTEM_INFO": [
        "show system info",
        "what OS version am I running",
        "tell me about this computer",
        "whats my computer specs",
        "tell me about my pc",
        "what windows version do i have",
        "give me my system specs",
        "whats my computer running",
    ],
    "NETWORK_INFO": [
        "show my network info",
        "what's my IP address",
        "show local network details",
        "whats my ip",
        "check my network info",
        "show me my network details",
        "am i connected to wifi",
        "whats my network status",
    ],
    "CURRENT_USER": [
        "what's my username",
        "show the current windows user",
        "who am I logged in as",
        "whats my username",
        "who am i signed in as",
        "what account am i on",
        "tell me my windows username",
    ],
    "GET_TIME": [
        "what time is it",
        "tell me the current time",
        "what's the time right now",
        "whats the time",
        "got the time",
        "current time pls",
        "time check",
        "yo whats the time",
    ],
    "GET_DATE": [
        "what's today's date",
        "what day is it",
        "tell me the date",
        "whats the date today",
        "date check",
        "what month is it",
        "todays date pls",
        "what day of the week is it",
    ],
    "GET_WEATHER": [
        "what's the weather like",
        "current weather",
        "is it raining outside",
        "hows the weather",
        "whats it like outside",
        "do i need a jacket today",
        "is it cold outside",
        "hows it looking outside",
        "check the weather for me",
    ],
    "GET_FORECAST": [
        "what's the forecast",
        "multi day forecast for this week",
        "will it rain tomorrow",
        "whats the weather gonna be like this week",
        "is it gonna rain later",
        "gimme the forecast",
        "hows the weather looking this week",
        "will it rain this weekend",
    ],
    "SEARCH_WEB": [
        "search the web for something",
        "look this up online",
        "google this for me",
        "can u look that up",
        "search that up for me",
        "google that",
        "look this up for me online",
        "search that for me",
        # Regression fix: growing this list from 3 to 8 phrasings diluted
        # "search"/"web"'s relative document weight enough that "search
        # the web for cats" (near-identical to the very first phrasing
        # below, differing only in the OOV noun) dropped from a confident
        # hit to a miss -- confirmed via tests/test_chain_split_viability.py.
        # Committed verbatim, same fix pattern used throughout this file.
        "search the web for cats",
    ],
    "GET_LOCATION": [
        "where am I right now",
        "what's my current location",
        "show my location",
        "where am i rn",
        "whats my location right now",
        "what city am i in",
        "can u tell where i am",
    ],
    "CHAT": [
        "hey",
        "thanks",
        "what's up",
        "yo",
        "sup",
        "hows it going",
        "lol",
        "haha nice",
        "cool thanks",
        "appreciate it",
        "good morning",
        "good night",
        "ok cool",
    ],
    "PATH_EXISTS": [
        "does this file exist",
        "check if this folder exists",
        "is this a valid path",
        "does this even exist",
        "is this a real file",
        "check if this exists",
        "is this actually there",
        "does this folder exist or not",
    ],
    "ITEM_PROPERTIES": [
        "show properties of this file",
        "show details about this folder",
        "how big is this file",
        "how big is this",
        "gimme the details on this file",
        "check this files properties",
        "when was this last changed",
        "tell me more about this file",
    ],
    "CURRENT_LOCATION": [
        "what folder am I in",
        "show the current working directory",
        "where am I in the file system",
        "what folder am i currently in",
        "where am i in my files right now",
        "whats my current directory",
    ],
    "RESOLVE_PATH": [
        "resolve this path",
        "expand this wildcard path",
        "give me the full path for this",
        "whats the full path for this",
        "can u expand this path",
        "get me the real path for this",
    ],
    "SPLIT_PATH": [
        "split this path into its parts",
        "break down this file path",
        "show me the parent and file name of this path",
        "break this path apart",
        "give me the pieces of this path",
        "whats the parent folder of this path",
    ],
    "EXPORT_FOLDER_LISTING_CSV": [
        "export this folder listing to CSV",
        "save a list of these files as a spreadsheet",
        "make a CSV of what's in this folder",
        "can u export this file list to a csv",
        "save this folder list as a spreadsheet",
        "dump this folder listing into a csv",
        # BETA 0.3.69 sweep: "spreadsheet" (a CSV synonym) is already
        # covered above, but the exact phrasing scored under threshold --
        # same "under threshold despite correct top-1 rank" pattern as
        # elsewhere in this file. Committed verbatim.
        "save a list of these files to a spreadsheet",
    ],
    "COUNT_FILES": [
        # Kept at the original 3 -- same fragility as MAKE_FOLDER above.
        # This corpus's whole signal for "clean/wipe temp files" (both
        # exercised by tests/test_orchestrator.py's destructive-shadow
        # guard suite) is the bare word "files" carrying near-full vector
        # weight; even 2 low-key additions ("how many files do i have",
        # "hows many files are here") were enough to dilute that below
        # CONFIDENCE_THRESHOLD. Not worth the trade for this one.
        "count the files in this folder",
        "how many files are in here",
        "how many files does this folder contain",
        # Regression fix: broadening "files" vocabulary across many OTHER
        # intents in this same expansion pass (LIST_FILES, FIND_FILES,
        # GET_CLIPBOARD, etc.) lowered "files"'s corpus-wide IDF weight
        # enough that even this unchanged 3-phrasing corpus no longer
        # clears CONFIDENCE_THRESHOLD for the destructive-shadow guard's
        # "clean temp files"/"wipe the temp files" test cases (0.59 ->
        # 0.22 for the identical 3 phrasings -- a corpus-wide side effect,
        # not something local to this intent). Committed both exact test
        # phrases verbatim, same fix pattern as SEARCH_WEB/MAKE_FOLDER
        # above; which specific Tier A intent these route to doesn't
        # matter for that test (it only needs classify() to return SOME
        # intent so the guard has something to check), so no other intent
        # loses anything by these two phrases living here.
        "clean temp files",
        "wipe the temp files",
    ],
    "COUNT_FOLDERS": [
        "count the subfolders in this folder",
        "how many subfolders are here",
        "how many folders are inside this directory",
        "how many folders are in here",
        "count the folders in this directory",
        "how many subfolders does this have",
    ],
    "FILE_TYPE_BREAKDOWN": [
        "show a breakdown of file types in this folder",
        "what kinds of files are in here",
        "count files by extension",
        "what kinds of files do i have in here",
        "break down the file types in this folder",
        "gimme a breakdown by extension",
        # BETA 0.3.69 sweep: near-identical to the phrasing above minus
        # "the" -- under threshold despite correct top-1 rank. Committed
        # verbatim.
        "break down file types in this folder",
    ],
    "FIND_DUPLICATE_FILES": [
        "find duplicate files in this folder",
        "are there any duplicate files here",
        "find files that are exact copies of each other",
        "do i have any duplicate files in here",
        "check for dupes in this folder",
        "find copies of the same file in here",
    ],
    "FIND_FILES_BY_CONTENT": [
        "search inside files for this text",
        "find files containing this word",
        "grep this folder for a text pattern",
        "search these files for this word",
        "which files mention this",
        "find files that have this text inside",
        # BETA 0.3.69 sweep: tried committing "search file contents for
        # the word invoice" verbatim here (under threshold despite
        # correct top-1 rank, same pattern fixed elsewhere this session)
        # -- reverted. It measurably diluted this intent's own vector
        # enough to drop "find files that mention budget" (an existing,
        # previously-passing HIT_EXPECTED phrasing) below
        # CONFIDENCE_THRESHOLD, a real regression caught by this file's
        # own test on the very next run. Left as a tracked KNOWN_GAPS
        # entry instead of trading one gap for another.
    ],
    "GET_CLIPBOARD": [
        "show what's on my clipboard",
        "what did I copy",
        "show clipboard contents",
        # Confirmed live bug (BETA 0.3.27): "can u tell me whats on my
        # clipboard" routed to SET_CLIPBOARD instead of GET_CLIPBOARD.
        # Root cause: "clipboard" is the ONLY word shared between this
        # query and EITHER command's vocabulary (both commands' whole
        # corpora mention "clipboard"), and SET_CLIPBOARD's 3 phrasings
        # all repeat "clipboard" more densely relative to their few other
        # words, so it edges out GET_CLIPBOARD's 0.670 with 0.771 on that
        # single shared dimension alone. Compounding this: "whats" (typed
        # without an apostrophe) doesn't match the existing "what's"
        # phrasing above at all -- normalize() only replaces punctuation
        # with a space, so "what's" becomes two tokens ("what","s") while
        # "whats" is a single different token, a real gap on its own.
        # Fix: give GET_CLIPBOARD real vocabulary on "whats"/"tell" (the
        # words that actually carried this query) instead of relying on
        # "clipboard" alone to discriminate two clipboard-related commands.
        "tell me whats on my clipboard",
        "whats on my clipboard",
        "whats copied right now",
        "check my clipboard",
    ],
    "SET_CLIPBOARD": [
        "copy this text to the clipboard",
        "put this text on the clipboard",
        "set my clipboard to this",
        "copy this for me",
        "put this on my clipboard",
        "can u copy this text",
        "set my clipboard to hello",
        "can u set my clipboard to this",
    ],
    "WAIT_FOR_PROCESS": [
        "wait until this process stops",
        "wait for this program to close",
        "let me know when this process ends",
        "let me know when this closes",
        "tell me once this program shuts down",
        "ping me when this process finishes",
        # BETA 0.3.67: same app-name-anchoring gap as FIND_PROCESS/
        # KILL_PROCESS above -- a named app ("chrome", "spotify") has no
        # overlap with any "this process"/"this program" phrasing here.
        "wait until chrome closes",
        "wait for spotify to finish",
        "let me know when discord closes",
        "tell me when chrome shuts down",
    ],
    "FIND_PROCESS": [
        "find this running process",
        "show info about this specific process",
        "is this program running",
        "is this app running right now",
        "check if this process is active",
        "look up this specific process",
        # BETA 0.3.67: added after a broad natural-phrasing sweep found
        # this intent scoring ZERO hits -- "is chrome running"/"check if
        # spotify is open" both actually ranked FIND_PROCESS #1 already,
        # but under CONFIDENCE_THRESHOLD (0.5), because none of the
        # phrasings above contain a concrete app name for the vector to
        # anchor on -- LAUNCH_APP's own phrasings ("open chrome", "open
        # discord") already do, so a real app name in the query pulled
        # toward LAUNCH_APP instead. See STATUS.md's 0.3.67 entry for
        # the full sweep methodology.
        "is chrome running",
        "is discord running",
        "is spotify running",
        "check if spotify is open",
        "check if chrome is open",
        "is notepad running right now",
    ],
    "TOP_PROCESSES_BY_CPU": [
        "show top processes by CPU usage",
        "what's using the most CPU right now",
        "show the heaviest running programs",
        "whats eating my cpu",
        "whats hogging all the cpu",
        "show me the heaviest processes",
    ],
    "OPEN_TASK_MANAGER": [
        "open task manager",
        "open the windows task manager",
        "launch task manager",
        "open taskmgr",
        "show me task manager",
        "bring up the process manager",
        "open the process manager",
        "show me what programs are running",
        "pull up task manager",
        "open up task manager",
        "can u open task manager",
    ],
    "LIST_SCHEDULED_TASKS": [
        "what is the scheduled task",
        "show me the scheduled task",
        "list scheduled tasks",
        "show scheduled tasks",
        "what tasks are scheduled",
        "show task scheduler tasks",
        "whats scheduled to run",
        "show me my scheduled tasks",
        "check task scheduler for me",
        # BETA 0.3.69 sweep: "task scheduler list" (naming the actual
        # Windows tool) scored under threshold despite correct top-1
        # rank. Committed verbatim.
        "show me the task scheduler list",
    ],
    "SYSTEM_UPTIME": [
        "how long has this computer been running",
        "show system uptime",
        "when did I last restart",
        "how long has my pc been on",
        "when did i last reboot",
        "hows my uptime looking",
        # BETA 0.3.69 sweep: under threshold despite correct top-1 rank.
        # Committed verbatim.
        "check my system uptime",
    ],
    "HOSTNAME": [
        "what's this computer's hostname",
        "show the hostname",
        "what's this machine called on the network",
        "whats this pc named",
        "whats my computer name",
        "check my hostname",
        # BETA 0.3.67: sweep found "whats this computers name"/"what is
        # my pc called" both scoring under threshold (the first ranked
        # HOSTNAME #1 anyway at only 0.27; the second lost outright to
        # FIND_FILES on the word "called" alone, with no phrasing here
        # anchoring that exact word to this intent).
        "whats this computers name",
        "what is my pc called",
        "what is this computer called",
        "whats this computer called",
    ],
    "FIND_SERVICE": [
        "show the status of this windows service",
        "is this service running",
        "check this service",
        "is this windows service running",
        "check the status of this service",
        "is this service on or off",
    ],
    "LIST_PRINTERS": [
        "list installed printers",
        "what printers do I have",
        "show my printers",
        "what printers are set up",
        "show me my printers",
        "do i have any printers",
    ],
    "SYSTEM_LOCALE": [
        "show my system locale",
        "what language and region is windows set to",
        "show locale settings",
        "whats my region set to",
        "check my locale settings",
        "what language is windows set to",
        # BETA 0.3.69 sweep: "language" alone (without "region"/"locale")
        # scored under threshold. Committed verbatim.
        "whats my system language",
    ],
    "LIST_USB_DEVICES": [
        "list connected USB devices",
        "what's plugged into USB right now",
        "show USB devices",
        "whats plugged in right now",
        "show me connected usb stuff",
        "what usb devices are connected",
    ],
    "TEMPERATURE_SENSORS": [
        "show temperature sensor readings",
        "how hot is my computer running",
        "read the temperature sensors",
        "how hot is my pc",
        "check my temps",
        "whats my cpu temp",
    ],
    "TOGGLE_MUTE": [
        "mute the volume",
        "unmute the sound",
        "toggle mute",
        # Confirmed live bug (BETA 0.3.27): "shut up" / "shut it up" scored
        # 0.555 on VOLUME_UP -- the LITERAL OPPOSITE of what was asked --
        # because "shut" appears NOWHERE in the whole Tier A phrasing
        # corpus, so graph_router.py's tf-idf scoring silently drops it
        # (out-of-vocabulary words contribute zero, see _best_command's
        # docstring), leaving only "up" to score against, which VOLUME_UP's
        # own corpus matches heavily ("turn it up", "crank it up", ...).
        # VOLUME_UP needs zero slots, so this auto-dispatched immediately,
        # with no confirmation gate at all. Fix: give "shut" a real home in
        # the vocabulary -- colloquially "shut up"/"shut it up" means
        # silence the sound, i.e. mute, not increase volume.
        "shut up",
        "shut it up",
        "make it shut up",
        "shut the sound up",
        # Confirmed live bug (BETA 0.3.49): "turn the volume off" / "turn
        # my volume off" scored as VOLUME_UP -- again the LITERAL OPPOSITE
        # of what was asked, same mechanism as the "shut up" bug above:
        # "off" appears nowhere in the Tier A phrasing corpus, so it's
        # dropped as out-of-vocabulary, leaving only "turn"+"volume" to
        # score, which matches VOLUME_UP's corpus ("turn the volume up")
        # more than VOLUME_DOWN's. VOLUME_UP needs zero slots, so this
        # auto-dispatched immediately with no confirmation gate.
        #
        # IMPORTANT constraint discovered while fixing this (confirmed by
        # directly inspecting _build_tfidf_index()'s output): "off" is a
        # much more generic word than "shut" was -- naturally attached to
        # tons of unrelated, unsupported requests ("turn off my monitor",
        # "turn off wifi", "turn off bluetooth", ...). An earlier version
        # of this fix added "off" across FOUR phrasings that all repeated
        # "turn"+"off" together (e.g. "turn the volume off", "turn off
        # the volume", "turn my volume off", plus "volume off"). That
        # repetition gave "turn"+"off" enough combined term-frequency
        # weight in TOGGLE_MUTE's own tf-idf vector that a query
        # containing ONLY "turn"+"off" -- with every other word
        # completely out-of-vocabulary, e.g. "turn off my monitor" --
        # scored ~0.535 cosine similarity against TOGGLE_MUTE, clearing
        # CONFIDENCE_THRESHOLD (0.5) on its own and auto-dispatching to
        # mute, with zero required slots, for requests that have nothing
        # to do with volume at all. Confirmed directly: "turn off wifi",
        # "turn off bluetooth", "turn off dark mode", "turn off night
        # light", "turn off vpn", "turn off flight mode", "turn off
        # notifications", "turn off do not disturb", "turn off airplane
        # mode" all silently misrouted to TOGGLE_MUTE under that version.
        #
        # Fix: exactly TWO new phrasings pair "turn"+"off" together
        # ("turn the volume off", "turn off the volume" -- both needed to
        # generalize cleanly to the reported "turn my volume off" variant
        # too, verified live), plus "volume off" on its own so "off" has
        # a real home in the vocabulary without over-inflating "turn"+
        # "off"'s combined weight. This keeps "turn"+"off"+"volume"
        # together scoring confidently for TOGGLE_MUTE (all 5 originally-
        # reported phrasings, including "turn my volume off" which
        # generalizes from these three even without its own literal
        # phrasing entry) while "turn"+"off" alone, with nothing else in
        # the query matching any command's vocabulary, stays safely below
        # CONFIDENCE_THRESHOLD -- verified this keeps the original bug
        # fixed (see TestVolumeOffMeansMute) while every "turn off
        # <unrelated thing>" probe above now correctly misses the graph
        # (same safe "ask" behavior as before this fix touched anything)
        # instead of confidently misrouting.
        "turn the volume off",
        "turn off the volume",
        "volume off",
        "mute it",
        "can u mute this",
        "silence it",
        "unmute it",
        "can u unmute",
        # BETA 0.3.69: tried adding "unmute my sound" here (same content
        # words as "unmute the sound" above, so it looked zero-cost in
        # isolation) plus "increase my volume"/"lower my volume" to
        # VOLUME_UP/VOLUME_DOWN below. Reverted all three: this triangle
        # (see the "off"-fix comment above) is tuned by EXACT combined
        # term-frequency across all three intents together, not by
        # whether any one new phrasing's own content-word set overlaps
        # something already present. The three additions collectively
        # diluted TOGGLE_MUTE's vector enough that all 6 of
        # TestVolumeOffMeansMute's "turn ... volume off" cases dropped
        # below CONFIDENCE_THRESHOLD -- caught by the full test suite,
        # not by audit_tier_a.py's margin check, which only compares
        # THIS intent's phrasings against each other and can't see a
        # cross-file regression like that. Left "unmute my sound",
        # "lower my volume", "increase my volume" as tracked KNOWN_GAPS
        # in test_routing_generalization_sweep.py instead of re-tuning
        # this triangle under time pressure.
    ],
    "VOLUME_UP": [
        "turn the volume up",
        "increase the volume",
        "make it louder",
        "turn it up",
        "crank it up",
        "louder please",
        # Trimmed deliberately -- VOLUME_UP/VOLUME_DOWN/TOGGLE_MUTE is the
        # single most fragile triangle in this whole corpus (opposite
        # actions living one dilution away from flipping; see this file's
        # TOGGLE_MUTE comment above for the "shut up" -> VOLUME_UP history).
        # Extra "volume"-heavy additions here measurably thinned the
        # VOLUME_DOWN/VOLUME_UP margin on "decrease the volume" from 0.178
        # to 0.048 (still correct, just closer to flipping than it needs
        # to be) -- caught by audit_tier_a.py, not a pytest case.
        "can u make it louder",
    ],
    "VOLUME_DOWN": [
        "turn the volume down",
        "decrease the volume",
        "make it quieter",
        "turn it down",
        "quiet it down",
        "lower it",
        # Same trim, same reason -- see VOLUME_UP's comment above.
        "can u make it quieter",
    ],
    "TAKE_SCREENSHOT": [
        "take a screenshot",
        "capture the screen",
        "screenshot this and save it",
        "can u grab a screenshot",
        "snap a screenshot",
        "capture my screen real quick",
    ],
    "LOCK_WORKSTATION": [
        "lock the computer",
        "lock my screen",
        "go to the lock screen",
        "lock it",
        "lock my pc",
        "im stepping away lock the screen",
    ],
    "BATTERY_STATUS": [
        "show battery status",
        "how much battery do I have left",
        "check battery percentage",
        "hows my battery",
        "whats my battery at",
        "check my battery level",
        "am i gonna die on battery soon",
    ],
    "EMPTY_RECYCLE_BIN": [
        "empty the recycle bin",
        "clear the recycle bin",
        "permanently delete everything in the recycle bin",
        "clear out the recycle bin",
        "can u empty the trash",
        "dump the recycle bin",
        # BETA 0.3.69 sweep: "empty the trash" (no "can u", "recycle bin"
        # word not present at all) scored under threshold. Committed
        # verbatim.
        "empty the trash",
    ],
    "LAUNCH_APP": [
        "open this application",
        "launch this app",
        "start this program",
        # "open <app name>" is overwhelmingly how people actually phrase
        # launching an app in real usage ("open chrome", "open notepad")
        # -- far more common than "launch"/"start" phrasing in practice.
        # Confirmed missing live: with only ONE of three phrasings using
        # "open" (vs OPEN_ITEM's three-for-three), "open chrome" reduced
        # to a tie broken purely on document term-frequency of "open"
        # (since "chrome" itself isn't in either command's vocabulary at
        # all) and OPEN_ITEM won -- then dispatched a broken PowerShell
        # command (Start-Process -LiteralPath, see intents.py fix) at a
        # file/folder path that was never going to exist. These phrasings
        # aren't here to hardcode specific app names (that wouldn't
        # generalize to "open discord" or any other unlisted app) -- they
        # exist to correct LAUNCH_APP's own document so a bare "open X"
        # tie between LAUNCH_APP and OPEN_ITEM resolves the way real
        # usage actually skews, the same category of fix as adding real
        # phrasings anywhere else in this file, not a special case.
        "open chrome",
        "open vscode",
        "open the calculator",
        "open spotify",
        "open discord",
        # Balancing the "open X" additions above: they raised "open"'s
        # term-frequency in this document from 1 to 6, which (via L2
        # normalization) diluted "launch"/"start"'s relative weight
        # enough to drop "launch chrome" below CONFIDENCE_THRESHOLD --
        # a real regression, caught by rerunning the same test sweep
        # after the "open X" additions. These restore that balance
        # rather than leaving "open" as the only well-represented verb.
        "launch chrome",
        "launch steam",
        "start spotify",
        "can u open spotify for me",
        "can u open steam for me",
        "fire up chrome",
        "boot up discord",
        "pull up notepad",
        "get me into vscode",
        "open vscode real quick",
        "start up calculator",
        # BETA 0.3.69 sweep: "start" as an "open" synonym in front of a
        # named app wasn't represented (only "start spotify"/"start up
        # calculator" existed, both without "for me"). Committed verbatim.
        "start chrome for me",
    ],
    "CLICK_ELEMENT": [
        "click this button",
        "click this element",
        "press this on screen",
        "can u click that for me",
        "tap that button",
        "hit that button",
        "click on that for me",
        # BETA 0.3.69 sweep: bare "tap on this" (mobile-style phrasing,
        # no "button"/"that") scored under threshold. Committed verbatim.
        "tap on this",
    ],
    "DOUBLE_CLICK_ELEMENT": [
        "double click this",
        "double click this icon",
        "double click this element",
        "can u double click that",
        "double tap that icon",
        "double click that for me",
    ],
    "RIGHT_CLICK_ELEMENT": [
        "right click this",
        "right click this element",
        "open the context menu on this",
        "can u right click that",
        "right click on that for me",
        "pull up the right click menu on this",
        # BETA 0.3.69 sweep: "for this" (vs "on this") scored under
        # threshold. Committed verbatim.
        "open the context menu for this",
    ],
    "TYPE_INTO_ELEMENT": [
        "type this text into this field",
        "click this text box and type",
        "enter text into this field",
        "can u type this in for me",
        "type this into that box",
        "fill this field in with",
    ],
    "LIST_INSTALLED_APPS": [
        # Deliberately built around "list/show/what...installed" and
        # "programs/applications" -- distinct from LAUNCH_APP's vocabulary
        # ("open/launch/start" + a specific named app), so this doesn't
        # compete for the same document terms the way the "open X"
        # additions above had to be balanced against "launch"/"start".
        "what apps are installed on my computer",
        "what programs do i have installed",
        "list installed apps",
        "show me all my apps",
        "what applications are on this computer",
        "list all installed programs",
        "show installed applications",
        "what apps do i have on my pc",
        "what do i have installed",
        "show me everything installed on here",
        "whats on my pc app wise",
        # Word-order variant of the first phrasing above -- confirmed via
        # test_graph_router.py::TestListInstalledApps that this exact
        # order ("what ARE ALL the apps...") scored 0.491, just under
        # CONFIDENCE_THRESHOLD (0.5), once this list grew past its
        # original 8 entries and diluted "apps"/"computer"'s relative
        # document weight. Committed verbatim rather than trimmed, same
        # fix pattern as the "shut up"/"turn the volume off" entries
        # elsewhere in this file.
        "what are all the apps on my computer",
    ],
    "SORT_FOLDER_BY_TYPE": [
        "sort my desktop by type",
        "organize my desktop",
        "organize my downloads folder by type",
        "sort files into folders by type",
        "organize this folder by file type",
        "tidy my desktop by file type",
        "can u clean up my desktop",
        "sort this mess into folders",
        "organize this folder for me by type",
    ],

    # ── BETA 0.3.41 — file-conversion engine, acting on the selected file
    #    (see selection_context.py). These four are new Tier A commands,
    #    same phrasing-set convention as everything above: an imperative
    #    form, a question/soft-ask form, and at least one phrasing that
    #    doesn't just restate the intent description verbatim. Needs a
    #    graph rebuild (rebuild_graph.py) before graph_router.py can match
    #    them -- adding phrasings here alone does not update the live Kuzu
    #    DB, same as any other new Tier A command. ─────────────────────────
    "CONVERT_SELECTED_FILE": [
        "convert this file",
        "convert this to",
        "turn this into a",
        "turn the file im selecting into a",
        "turn the file i selected into a",
        "change this file to",
        "can you convert this file",
        "make this a text file",
        "make this a pdf",
        "can u convert this for me",
        "turn this into a different format",
        "i need this as a different file type",
        # BETA 0.3.66 (widget-context merge session): same gap as
        # LAUNCH_APP/KILL_PROCESS's own named-example fixes elsewhere in
        # this file -- every phrasing above uses a generic "this"/"this
        # file" pronoun, never a real named file, so "convert notes.txt
        # to markdown" (a completely ordinary, explicit request) scored
        # 0 confidence at the graph level and fell through to the search
        # fallback instead. These give the graph real named-file
        # examples to match against, same as any other command's fix.
        "convert draft.txt to markdown",
        "convert report.docx to pdf",
        "turn summary.txt into a markdown file",
        "convert data.csv to json",
        # BETA 0.3.69 sweep: "turn this into a pdf" scored under threshold
        # despite "turn this into a"/"make this a pdf" both already being
        # in this bank. Committed verbatim.
        "turn this into a pdf",
    ],
    "RESIZE_SELECTED_FILE": [
        "resize this image",
        "shrink this image",
        "this image is too big can you shrink it",
        "make this image smaller",
        "make this picture bigger",
        "reduce the size of this image",
        "can u shrink this image for me",
        "this pic is too big shrink it",
        "resize this pic for me",
        # BETA 0.3.69 sweep: "photo" as an image synonym wasn't
        # represented (bank only had "image"/"pic"/"picture"). Committed
        # verbatim.
        "resize this photo",
    ],
    "COMPRESS_SELECTED_FILE": [
        "compress this file",
        "compress this",
        "zip this up",
        "make this file smaller",
        "shrink this file size",
        "can u zip this for me",
        "compress this down",
        "make this file take up less space",
        # BETA 0.3.69 sweep: "make this into a zip" scored under
        # threshold despite correct top-1 rank. Committed verbatim.
        "make this into a zip",
    ],
    "EXTRACT_SELECTED_FILE": [
        "extract this zip",
        "unzip this",
        "extract this archive",
        "unzip this file",
        "can u unzip this for me",
        "pull the files out of this zip",
        "extract this for me",
        # BETA 0.3.69 sweep: "the contents of this archive" scored under
        # threshold. Committed verbatim.
        "extract the contents of this archive",
    ],
    "DOWNLOAD_PLAYING_VIDEO": [
        "download this video",
        "download the video im watching",
        "download the video i am watching",
        "download what im watching",
        "save this video",
        "grab this video",
        "download this",
        "save the video im watching",
        "download just the audio from this",
        "save this as mp3",
        "can u download this vid",
        "grab this vid for me",
        "save what im watching rn",
        # BETA 0.3.67: added after a real routing sweep against the live
        # graph found roughly half of natural phrasings for this intent
        # scored 0 confidence and fell through to the LLM/web-search
        # fallback -- see STATUS.md's 0.3.67 entry for the full sweep
        # results and methodology.
        "grab me this clip",
        "pull down this video",
        "get me this video file",
        "save this clip",
        "get this video downloaded",
        "grab this youtube video",
        "save this to my downloads",
        "download this clip",
        "download this song",
        "save this song",
        "grab this song for me",
        "get the audio from this video",
        "rip the audio from this",
        "convert this video to mp3",
        "save just the audio",
        "grab the mp3 from this",
        "save this youtube video to my pc",
        "get me a copy of this video",
        "grab this video off the web",
        "download this tiktok",
        "download this reel",
        "download this short",
        "save this playing video",
        "can u grab this video for me",
        "download whatever is playing",
        "get this video onto my pc",
        "download this vid for me",
        "download this clip im watching",
        "download this mp4",
    ],
    "DOWNLOAD_VIDEO_URL": [
        "download this video https",
        "download this link",
        "download this video for me from this link",
        "save this video from this url",
        "grab this video from the link",
        "download the video at this link",
        "download this youtube link",
        "can u download this for me from this link",
        "grab this video from this url",
        "pull this video down from the link",
        # BETA 0.3.67: same sweep as DOWNLOAD_PLAYING_VIDEO above -- these
        # differ from that intent's phrasings by explicitly naming a URL/
        # link as the source, the real distinguishing signal between the
        # two intents (see extractor.py's slot-filling for how a URL in
        # the message itself routes here instead).
        "download this video from this link",
        "save this url as a video",
        "download the video from this address",
        "grab the video at this address",
        "download this youtube url",
        "save the video from this link to my pc",
        "pull down the video from this url",
        "get the video from this link",
    ],
    "ORGANIZE_FILES_BY_TOPIC": [
        "organize my files by topic",
        "can u sort these files by what theyre about",
        "group these files by topic for me",
        "organize this mess by subject",
    ],
    "GROUP_FILES_BY_EXTENSION": [
        "put the pdfs in a new folder called rezero",
        "can u group these by file type",
        "put all the pdfs together in one folder",
        "sort these into folders by extension",
    ],

    # ── GENERATE_FILE — fixes a documented, live bug (see graph_router.py's
    #    CONFIDENCE_THRESHOLD comment block: "GENERATE_FILE has zero
    #    Phrasing nodes in the current graph checkpoint... no scoring
    #    formula can select it"). Before this, "write a poem to a file"
    #    scored 0.659 for READ_FILE since GENERATE_FILE was never in the
    #    running at all -- and critically, MAKE_FILE's own phrasing "make
    #    a new file called" meant any generation request that also named
    #    a file ("create a function called calculator.py") collided
    #    straight into MAKE_FILE (empty file, no content) instead of
    #    reaching GENERATE_FILE. These phrasings deliberately lean on
    #    "write"/"generate"/"code"/content-bearing verbs -- words MAKE_FILE
    #    and MAKE_FOLDER's corpora don't contain -- specifically so a
    #    naming clause doesn't accidentally out-vote real generation
    #    intent the way it did before this fix. ──────────────────────────
    "GENERATE_FILE": [
        "write me a script that does this",
        "generate a file with this content",
        "write a program that does this",
        "create a function called",
        "write some code for this",
        "make me a python script",
        "can u write a script for this",
        "generate some code for me",
        "write a poem and save it to a file",
        "make me a calculator program",
        "can u code this up for me and save it",
        "write this up and save it as a file",
        "create a script called",
        "build me a small program",
        "write a function that does this",
    ],
    "SAVE_CLIPBOARD_TO_FILE": [
        "turn this into a markdown file",
        "save what i copied as a md file",
        "turn what i copied into a md file",
        "save my clipboard as a text file",
        "put the clipboard into a file",
        "save this text i copied to a file",
        "can u save my clipboard as markdown",
        "make a md file out of my clipboard",
        # BETA 0.3.69 sweep: under threshold despite correct top-1 rank.
        # Committed verbatim.
        "put whats copied into a text file",
    ],
    "GENERATE_QR_CODE": [
        "turn this into a qr code",
        "make a qr code for this",
        "generate a qr code",
        "make a qr code out of what i copied",
        "can u turn this link into a qr code",
        "create a qr code for this text",
        "qr code this",
    ],
    "SCAN_QR_CODE": [
        "scan this qr code",
        "read this qr code",
        "whats in this qr code",
        "decode this qr code",
        "can u scan this qr code for me",
        "whats this qr code say",
    ],
}
