"""
tier_a_components.py -- semantic component definitions for the
EXPERIMENTAL component router, v2.

METHODOLOGY (read this before editing): every alias and every
required/any_of/forbidden entry below was derived by reading ALL 71
commands' real phrasing sets in graph_source_data/tier_a_phrasings.py
directly (dumped once, in full, and reviewed top to bottom) plus a
document-frequency pass over that same corpus to find words shared
across 3+ commands (the genuinely collision-prone vocabulary). Multi-word
aliases below are lifted VERBATIM from a command's own real phrasing
text specifically where the word-frequency pass showed that word
colliding with another command's vocabulary (e.g. "turn" appears in
CONVERT_SELECTED_FILE, TOGGLE_MUTE, VOLUME_UP and VOLUME_DOWN's own
phrasings -- so "turn" alone is never used as a bare alias anywhere
below; every command that needs it gets its own multi-word phrase
instead, taken from its own corpus).

This file was written and FROZEN before tests/test_component_router.py
was ever run against it a second time. The first run's results are
reported as-is in TESTING_REPORT.md, including failures, rather than
iterating alias-by-alias against which specific test string failed --
that would just be curve-fitting to the test oracle. Where a genuine
design flaw was caught by re-reading the SOURCE phrasing text (not by
running tests), it's fixed here and the reasoning is documented inline.
Known, accepted limitations (inherent ambiguity that exists in TF-IDF's
own corpus too, e.g. COPY_ITEM vs SET_CLIPBOARD sharing the near-
identical phrase "copy this for me") are documented rather than forced.

═══ v2.1 ITERATION (post blind-run TESTING_REPORT.md) ═══════════════════
The blind run scored 58/67 (86.6%) against TF-IDF's 64/67 (95.5%). Every
one of the 9 failures was classified BEFORE fixing (missing vocabulary /
incorrect component definition / missing disambiguation constraint /
genuinely-unsupported test), per that classification:

  1-3. "turn off wifi"->NETWORK_INFO, "stop the print spooler
       service"->FIND_SERVICE, "reset network adapter"->NETWORK_INFO
       (all should miss): MISSING DISAMBIGUATION CONSTRAINT. Root-caused
       by reading graph_router.py's OWN real, shipped
       _read_only_shadowed_by_action_verb guard directly (not
       reinvented) -- ACTION_MODIFY's alias list is copied VERBATIM from
       graph_router.py's _ACTION_VERBS constant. Initially applied
       forbidden=[ACTION_MODIFY] only to graph_router.py's exact
       _READ_ONLY_LOOKALIKES 3-item scope -- but re-testing surfaced a
       real regression this narrow scope didn't cover ("format the usb
       drive" -> DISK_USAGE, a false positive DISK_USAGE isn't even in
       the reference list for). Investigated why TF-IDF gets that one
       right without an explicit guard: it doesn't have one for
       DISK_USAGE -- it's protected only by the INCIDENTAL side effect
       of L2-normalized cosine similarity naturally diluting when
       off-vocabulary words ("format", "usb") are added to a short query,
       which happens to lower DISK_USAGE's score below threshold too even
       without a deliberate rule. The component model has no equivalent
       incidental protection for ANY intent -- a hard component match is
       binary, not continuous -- so the general fix is to apply this
       constraint uniformly across every read-only, single-noun info
       intent that COULD plausibly share its noun with a real write verb,
       not just the 3 TF-IDF's own dilution happened not to already
       cover. Checked each candidate intent's own corpus first (see the
       intent map below) and excluded SYSTEM_UPTIME, whose own real
       phrasing legitimately uses "restart" ("last restart").
  4-5. "set my clipboard to hello", "put this on the clipboard"
       (should hit SET_CLIPBOARD): MISSING VOCABULARY. SET_CLIPBOARD's
       own real phrasings literally use "set"/"put" verbs that were
       never added as aliases. Fixed with a dedicated
       ACTION_SET_CLIPBOARD component (multi-word aliases lifted
       verbatim from SET_CLIPBOARD's own phrasing corpus) rather than
       broadening the shared ACTION_COPY, since "put"/"set" collide with
       MOVE_ITEM/GROUP_FILES_BY_EXTENSION per the original
       document-frequency pass.
  6-7. "sort my desktop by type", "organize my downloads folder by type"
       (should hit SORT_FOLDER_BY_TYPE): INCORRECT COMPONENT DEFINITION.
       OBJECT_EXTENSION's alias list wrongly included "by type", which
       never actually appears in FILE_TYPE_BREAKDOWN's or
       GROUP_FILES_BY_EXTENSION's real corpus (checked directly -- their
       real phrasings use "by extension"/"file type", never bare "by
       type"). Removed "by type" from OBJECT_EXTENSION; SORT_FOLDER_BY_
       TYPE never required OBJECT_EXTENSION in the first place, so this
       is a pure precision fix with no other side effect.
  8.   "erase this dir" (my own held-out test, not source-grounded):
       INVALID/UNSUPPORTED TEST. "erase" never appears anywhere in
       DELETE_ITEM's real phrasing corpus -- there was no source
       evidence to have built this alias from. Left unfixed; the test
       itself was the problem, not the router.
  9.   "build a new folder" (my own held-out test): INVALID/UNSUPPORTED
       TEST -- and TF-IDF ALSO misses it, confirming it wasn't a fair
       comparison case. "build" appears exactly once in the whole real
       corpus, in GENERATE_FILE's own phrasing ("build me a small
       program"), never MAKE_FOLDER's. Left unfixed.

Separately (found by asking "what general capability does TF-IDF have
that the component model doesn't yet" for the OOV-target-name problem
described in orchestrator.py's _NAME_FROM_OUTSIDE_VOCAB_INTENTS -- see
that constant's own docstring on why MAKE_FOLDER/LAUNCH_APP/KILL_PROCESS/
FIND_PROCESS/FIND_SERVICE/WAIT_FOR_PROCESS need OOV-name handling under
TF-IDF at all): re-read KILL_PROCESS's and WAIT_FOR_PROCESS's real
phrasing corpus and found several real phrasings that never mention
"process"/"program"/"app" at all ("can u kill this for me", "this is
stuck close it", "let me know when this closes") -- meaning the
OBJECT_PROCESS requirement I'd put on both was stricter than their OWN
real training data, independent of any OOV concern. Dropped OBJECT_
PROCESS as a requirement for KILL_PROCESS/WAIT_FOR_PROCESS (kept it for
FIND_PROCESS/FIND_SERVICE, whose real corpus DOES always mention the
categorical noun). This also means the component model doesn't need
TF-IDF's confidence-threshold/whitelist workaround for these two at all
-- an unmatched OOV word (a real app name) just becomes a harmless
`unmatched_word`, it never dilutes anything the way it dilutes an
L2-normalized cosine-similarity vector. Verified empirically in
TESTING_REPORT_V2.md, not assumed.
"""

from typing import Dict, List, TypedDict


class ComponentDef(TypedDict):
    canonical_name: str
    category: str  # ACTION | OBJECT | TARGET | MODIFIER | CONTEXT
    aliases: List[str]
    description: str


COMPONENTS: Dict[str, ComponentDef] = {

    # ═══ ACTIONS ═══════════════════════════════════════════════════════

    "ACTION_MODIFY": {
        "canonical_name": "MODIFY",
        "category": "ACTION",
        # VERBATIM copy of graph_router.py's real, shipped _ACTION_VERBS
        # constant -- this is the exact reference mechanism, not a
        # reinvention. Used ONLY as a forbidden/disqualifying signal on
        # the three read-only-lookalike intents graph_router.py's own
        # _READ_ONLY_LOOKALIKES names (see those intents' forbidden
        # lists below) -- never a required component of any real intent.
        "aliases": [
            "stop", "start", "restart", "reset", "format", "disable", "enable",
            "delete", "remove", "kill", "wipe", "erase", "uninstall", "turn",
        ],
        "description": "graph_router.py's own _ACTION_VERBS, migrated verbatim -- forbidden-only signal",
    },
    "ACTION_CREATE": {
        "canonical_name": "CREATE",
        "category": "ACTION",
        # "start"/"new" deliberately excluded as bare aliases: "start" also
        # means launching an app (LAUNCH_APP: "start this program", "start
        # spotify", "start up calculator"); "new" is used as an adjective

        # modifying MANY unrelated nouns across the corpus, not a verb.
        # "touch" is MAKE_FILE's own exclusive word (real Unix-y phrasing,
        # doesn't collide with anything else in the corpus).
        "aliases": ["create", "make", "touch"],
        "description": "Creating a new file/folder",
    },
    "ACTION_CREATE_FILE_ONLY": {
        "canonical_name": "START_NEW_FILE",
        "category": "ACTION",
        # "start a new file" is MAKE_FILE's own real phrasing -- captured
        # as a multi-word alias so bare "start" (LAUNCH_APP's territory)
        # is never touched.
        "aliases": ["start a new file"],
        "description": "MAKE_FILE-specific creation phrasing using 'start'",
    },
    "ACTION_GENERATE_CONTENT": {
        "canonical_name": "GENERATE_CONTENT",
        "category": "ACTION",
        # Checked against every phrasing in the corpus: "write"/"generate"/
        # "code this up" appear ONLY in GENERATE_FILE's own set.
        "aliases": ["write", "generate", "code this up"],
        "description": "Generating written content/code (GENERATE_FILE)",
    },
    "ACTION_DELETE": {
        "canonical_name": "DELETE",
        "category": "ACTION",
        # "get rid of" as a multi-word alias (DELETE_ITEM's own phrasing:
        # "get rid of this file") -- bare "get" alone is far too generic
        # (shared with KILL_PROCESS/LAUNCH_APP/RESOLVE_PATH per the
        # document-frequency pass) to ever be a bare alias.
        "aliases": ["delete", "remove", "trash", "toss", "nuke", "yeet", "get rid of"],
        "description": "Deleting a file/folder/item",
    },
    "ACTION_EMPTY": {
        "canonical_name": "EMPTY",
        "category": "ACTION",
        # EMPTY_RECYCLE_BIN's own distinct verb set -- "empty"/"clear
        # out"/"dump"/"permanently delete" -- kept SEPARATE from generic
        # ACTION_DELETE specifically so DELETE_ITEM's own vocabulary can't
        # accidentally satisfy EMPTY_RECYCLE_BIN or vice versa; the two
        # are disambiguated by requiring EMPTY_RECYCLE_BIN's own action
        # AND the recycle-bin object together.
        "aliases": ["empty", "clear", "dump", "permanently delete"],
        "description": "Emptying/clearing the recycle bin specifically",
    },
    "ACTION_RENAME": {
        "canonical_name": "RENAME",
        "category": "ACTION",
        # "rename" doesn't appear anywhere else in the document-frequency
        # pass (exclusive to RENAME_ITEM already) -- multi-word phrasings
        # added anyway since RENAME_ITEM's corpus genuinely varies this
        # much without ever using the bare word "rename".
        "aliases": ["rename", "change the name", "give this a new name", "change what this is called"],
        "description": "Renaming a file/folder",
    },
    "ACTION_MOVE": {
        "canonical_name": "MOVE",
        "category": "ACTION",
        # "put" excluded as a bare alias -- shared with GROUP_FILES_BY_
        # EXTENSION ("put the pdfs...") and SET_CLIPBOARD ("put this on my
        # clipboard") per the document-frequency pass. MOVE_ITEM's own
        # "put"-phrasing captured as a multi-word alias instead.
        "aliases": ["move", "relocate", "shift", "drag", "put this file somewhere else"],
        "description": "Moving a file/folder",
    },
    "ACTION_COPY": {
        "canonical_name": "COPY",
        "category": "ACTION",
        # "copy" IS genuinely ambiguous in the SOURCE corpus itself --
        # COPY_ITEM's "can u copy this for me" and SET_CLIPBOARD's "copy
        # this for me" are near-identical phrasings for two different
        # commands. This is a REAL, inherent ambiguity that exists in
        # TF-IDF's own training data too, not something this component
        # model introduces -- documented as a known limitation rather
        # than force-resolved. OBJECT_CLIPBOARD vs OBJECT_FILE_OR_FOLDER
        # is the only real disambiguator, and it only works when the
        # query actually names its object explicitly.
        "aliases": ["copy", "duplicate", "clone"],
        "description": "Copying/duplicating a file/folder, or writing to clipboard",
    },
    "ACTION_SET_CLIPBOARD": {
        "canonical_name": "SET_CLIPBOARD_VERB",
        "category": "ACTION",
        # v2.1 fix (category 1, missing vocabulary): SET_CLIPBOARD's own
        # real phrasing corpus uses "set"/"put" verbs that were never
        # captured at all in v2's first pass. Multi-word, lifted
        # VERBATIM (as prefixes, not full phrases) from that command's
        # own real phrasings ("put this text on the clipboard", "set my
        # clipboard to this", "put this on my clipboard", "set my
        # clipboard to hello") -- NOT folded into the generic ACTION_COPY
        # because bare "set"/"put" collide with MOVE_ITEM
        # ("put this file somewhere else") and GROUP_FILES_BY_EXTENSION
        # ("put the pdfs...") per the original document-frequency pass.
        # Deliberately kept as PREFIXES ("set my", not "set my
        # clipboard") -- a regression caught in re-testing showed that
        # including "clipboard" inside the multi-word alias consumes that
        # word entirely (multi-word aliases are matched and stripped
        # before single-word scanning runs -- see
        # component_extractor.py), starving OBJECT_CLIPBOARD's own
        # separate required match. Leaving "clipboard" out of this
        # phrase lets it fall through to content-word scanning normally.
        "aliases": ["set my", "put this on the", "put this on my", "put this text on the"],
        "description": "SET_CLIPBOARD's own 'set'/'put' verb phrasings (prefix-only)",
    },
    "ACTION_LIST": {
        "canonical_name": "LIST",
        "category": "ACTION",
        "aliases": ["list", "peek"],
        "description": "Listing/enumerating a collection",
    },
    "ACTION_QUERY_GENERIC": {
        "canonical_name": "QUERY",
        "category": "ACTION",
        # "show"/"whats"/"check"/"tell" are each shared across 15-26
        # different commands per the document-frequency pass -- they
        # carry almost no discriminating power alone. Kept as a SEPARATE,
        # weak component that most read/info intents accept as optional
        # supporting evidence (any_of), never as a hard requirement by
        # itself, exactly mirroring how little standalone weight these
        # words would get in a real TF-IDF vector too (very low IDF).
        "aliases": ["show", "whats", "what's", "check", "tell"],
        "description": "Generic query verb shared across most info/read intents -- weak signal alone",
    },
    "ACTION_FIND": {
        "canonical_name": "FIND",
        "category": "ACTION",
        "aliases": ["find", "search", "hunt down", "track down", "look up", "grep"],
        "description": "Searching for something",
    },
    "ACTION_READ": {
        "canonical_name": "READ",
        "category": "ACTION",
        "aliases": ["read", "print out"],
        "description": "Reading/displaying file contents",
    },
    "ACTION_SCAN_OR_DECODE": {
        "canonical_name": "SCAN_OR_DECODE",
        "category": "ACTION",
        # v0.3.60 fix: SCAN_QR_CODE's own exclusive verbs -- checked
        # against the full corpus first, "scan"/"decode" appear in
        # exactly this command's own real phrasings and nowhere else.
        # Kept separate from ACTION_READ (also a valid any_of signal for
        # SCAN_QR_CODE, via "read this qr code") rather than folded in,
        # so this component's own presence/absence stays legible on its
        # own in any future debugging of SCAN_QR_CODE specifically.
        "aliases": ["scan", "decode"],
        "description": "Scanning/decoding a QR code (SCAN_QR_CODE's own exclusive verbs)",
    },
    "ACTION_OPEN": {
        "canonical_name": "OPEN",
        "category": "ACTION",
        "aliases": ["open", "launch", "fire up", "boot up", "pull up", "pop this open"],
        "description": "Opening a file/folder, or launching an application",
    },
    "ACTION_START_PROGRAM": {
        "canonical_name": "START_PROGRAM",
        "category": "ACTION",
        # "start"/"start up" bare-word excluded (collides with MAKE_FILE's
        # "start a new file" and, in principle, any future "start X"
        # phrasing) -- captured only as LAUNCH_APP's own real multi-word
        # phrasings.
        #
        # "run" (BETA 0.3.62, live stress-testing): added as a bare
        # alias -- unlike "start", nothing else in this taxonomy uses
        # bare "run" today, so there's no equivalent live collision to
        # guard against. Confirmed via STATUS.md's own note on this
        # session: RUN_MACRO has zero presence anywhere in either this
        # component map or graph_source_data/tier_a_phrasings.py, so
        # it isn't reachable via either router right now regardless of
        # this change -- flagging for whoever gives RUN_MACRO real
        # training data later: "run" will need a forbidden-macro guard
        # on LAUNCH_APP once RUN_MACRO actually has "run ... macro"
        # phrasings to collide with.
        "aliases": ["start this program", "start spotify", "start up", "get me into", "run"],
        "description": "LAUNCH_APP-specific 'start'/'run' phrasing",
    },
    "ACTION_CLOSE": {
        "canonical_name": "CLOSE",
        "category": "ACTION",
        "aliases": ["kill", "terminate", "close", "force quit", "shut this program down", "make this program stop"],
        "description": "Killing/closing a running process",
    },
    "ACTION_WAIT": {
        "canonical_name": "WAIT",
        "category": "ACTION",
        "aliases": ["wait", "ping me when", "let me know when"],
        "description": "Waiting for a process to end",
    },
    "ACTION_COUNT": {
        "canonical_name": "COUNT",
        "category": "ACTION",
        "aliases": ["count", "how many"],
        "description": "Counting items",
    },
    "ACTION_ORGANIZE": {
        "canonical_name": "ORGANIZE",
        "category": "ACTION",
        "aliases": ["organize", "sort", "tidy", "clean up", "group these"],
        "description": "Organizing/arranging files",
    },
    "ACTION_COMPRESS": {
        "canonical_name": "COMPRESS",
        "category": "ACTION",
        "aliases": ["compress", "zip this", "shrink this file"],
        "description": "Compressing a file",
    },
    "ACTION_EXTRACT": {
        "canonical_name": "EXTRACT",
        "category": "ACTION",
        "aliases": ["extract", "unzip", "pull the files out"],
        "description": "Extracting an archive",
    },
    "ACTION_CONVERT": {
        "canonical_name": "CONVERT",
        "category": "ACTION",
        # "turn" NEVER used bare (see module docstring) -- every real
        # CONVERT_SELECTED_FILE phrasing that uses "turn" is captured
        # verbatim as a multi-word alias instead.
        "aliases": [
            "convert", "change this file to",
            "turn this into a", "turn the file im selecting into a",
            "turn the file i selected into a",
        ],
        "description": "Converting a file's format",
    },
    "ACTION_RESIZE": {
        "canonical_name": "RESIZE",
        "category": "ACTION",
        "aliases": ["resize", "shrink this image", "reduce the size"],
        "description": "Resizing an image",
    },
    "ACTION_DOWNLOAD": {
        "canonical_name": "DOWNLOAD",
        "category": "ACTION",
        "aliases": ["download", "save this video", "grab this video", "pull this video down"],
        "description": "Downloading a video",
    },
    "ACTION_MUTE": {
        "canonical_name": "MUTE",
        "category": "ACTION",
        # Every one of these is TOGGLE_MUTE's own real phrasing, verbatim
        # -- see module docstring re: "turn"/"shut"/"off" never being
        # used as bare single-word aliases anywhere in this file.
        "aliases": [
            "mute", "unmute", "silence", "toggle mute",
            "shut up", "shut it up", "make it shut up", "shut the sound up",
            "turn the volume off", "turn off the volume", "volume off",
        ],
        "description": "Toggling audio mute",
    },
    "ACTION_VOLUME_UP": {
        "canonical_name": "VOLUME_UP",
        "category": "ACTION",
        "aliases": ["increase the volume", "make it louder", "turn the volume up", "turn it up", "crank it up", "louder please"],
        "description": "Raising volume",
    },
    "ACTION_VOLUME_DOWN": {
        "canonical_name": "VOLUME_DOWN",
        "category": "ACTION",
        "aliases": ["decrease the volume", "make it quieter", "turn the volume down", "turn it down", "quiet it down", "lower it"],
        "description": "Lowering volume",
    },
    "ACTION_LOCK": {
        "canonical_name": "LOCK",
        "category": "ACTION",
        "aliases": ["lock"],
        "description": "Locking the workstation",
    },
    "ACTION_SCREENSHOT": {
        "canonical_name": "SCREENSHOT",
        "category": "ACTION",
        "aliases": ["screenshot", "capture the screen", "snap a screenshot", "capture my screen"],
        "description": "Capturing the screen",
    },
    "ACTION_CLICK": {
        "canonical_name": "CLICK",
        "category": "ACTION",
        "aliases": ["tap", "hit", "press"],
        "description": "Clicking a UI element (bare click handled separately below)",
    },
    "ACTION_CLICK_BARE": {
        "canonical_name": "CLICK_BARE",
        "category": "ACTION",
        # "click" itself excluded from ACTION_CLICK above and isolated
        # here because TYPE_INTO_ELEMENT's own real phrasing "click this
        # text box and type" also contains the word "click" -- per the
        # document-frequency pass, "click" is shared across 4 commands.
        # DOUBLE_CLICK_ELEMENT/RIGHT_CLICK_ELEMENT get their OWN
        # multi-word aliases below so this bare "click" only ever fires
        # for plain CLICK_ELEMENT.
        "aliases": ["click"],
        "description": "Bare 'click' -- CLICK_ELEMENT's own default verb",
    },
    "ACTION_DOUBLE_CLICK": {
        "canonical_name": "DOUBLE_CLICK",
        "category": "ACTION",
        "aliases": ["double click", "double tap"],
        "description": "Double-clicking a UI element",
    },
    "ACTION_RIGHT_CLICK": {
        "canonical_name": "RIGHT_CLICK",
        "category": "ACTION",
        "aliases": ["right click"],
        "description": "Right-clicking a UI element",
    },
    "ACTION_TYPE": {
        "canonical_name": "TYPE",
        "category": "ACTION",
        # Bare "type" excluded (collides with "by type"/"file type" --
        # see SORT_FOLDER_BY_TYPE/GROUP_FILES_BY_EXTENSION/
        # FILE_TYPE_BREAKDOWN). Every multi-word phrase here is lifted
        # directly from TYPE_INTO_ELEMENT's own real phrasing set,
        # including "and type" for its own "click this text box and
        # type" phrasing (caught by re-reading that command's corpus
        # directly, before any test was run).
        "aliases": ["type this into", "type this in", "type into", "and type", "enter text into", "fill this field"],
        "description": "Typing text into a UI field",
    },

    # ═══ OBJECTS ═══════════════════════════════════════════════════════

    "OBJECT_FOLDER": {
        "canonical_name": "FOLDER",
        "category": "OBJECT",
        "aliases": ["folder", "directory", "dir"],
        "description": "A folder/directory",
    },
    "OBJECT_FILE": {
        "canonical_name": "FILE",
        "category": "OBJECT",
        "aliases": ["file"],
        "description": "A file",
    },
    "OBJECT_FILE_OR_FOLDER": {
        "canonical_name": "ITEM",
        "category": "OBJECT",
        "aliases": ["item", "this", "that"],
        "description": "An unspecified file/folder referred to contextually",
    },
    "OBJECT_ARCHIVE": {
        "canonical_name": "ARCHIVE",
        "category": "OBJECT",
        "aliases": ["zip", "archive"],
        "description": "A compressed archive",
    },
    "OBJECT_IMAGE": {
        "canonical_name": "IMAGE",
        "category": "OBJECT",
        "aliases": ["image", "picture", "pic"],
        "description": "An image file",
    },
    "OBJECT_PROCESS": {
        "canonical_name": "PROCESS",
        "category": "OBJECT",
        "aliases": ["process", "program", "app"],
        "description": "A running process/program",
    },
    "OBJECT_CPU_USAGE": {
        "canonical_name": "CPU_USAGE",
        "category": "OBJECT",
        "aliases": ["cpu", "heaviest", "eating my cpu", "hogging"],
        "description": "CPU usage ranking",
    },
    "CONTEXT_SPECIFIC_PROCESS": {
        "canonical_name": "SPECIFIC_PROCESS",
        "category": "CONTEXT",
        "aliases": ["specific process", "specific"],
        "description": "Referring to one particular process, not the full list",
    },
    "CONTEXT_PROCESS_DISTRESS": {
        "canonical_name": "PROCESS_DISTRESS",
        "category": "CONTEXT",
        # v2.1 fix (regression found in re-testing, category 3): dropping
        # OBJECT_PROCESS as a hard requirement for KILL_PROCESS (see that
        # intent's own comment) opened a real false positive -- "kill the
        # lights" started matching on bare ACTION_CLOSE alone. Re-reading
        # KILL_PROCESS's own corpus: two of its real phrasings ("this app
        # is frozen kill it", "this is stuck close it") distinguish
        # themselves with a distress word, not a categorical noun. Adding
        # this as an alternate any_of signal (alongside OBJECT_PROCESS)
        # closes the regression without reintroducing the over-strict
        # requirement -- "kill the lights" has neither a process noun nor
        # a distress word, so it correctly fails again.
        "aliases": ["stuck", "frozen", "not responding", "hung"],
        "description": "A process/app described as stuck/frozen -- KILL_PROCESS's own distress vocabulary",
    },
    "OBJECT_MARKDOWN": {
        "canonical_name": "MARKDOWN",
        "category": "OBJECT",
        # v0.3.60 fix: kept SEPARATE from OBJECT_FILE (bare "file") on
        # purpose -- SAVE_CLIPBOARD_TO_FILE's own real phrasing "can u
        # save my clipboard as markdown" is the one case in its corpus
        # with no literal "file" word at all, so it needs its own signal.
        # Splitting it out (rather than folding "markdown" into OBJECT_
        # FILE's alias list) is what makes it possible to forbid THIS
        # specific word on CONVERT_SELECTED_FILE below without also
        # forbidding CONVERT_SELECTED_FILE's own real, legitimate bare
        # "file" phrasings ("make this a text file", "different file
        # type") -- see CONVERT_SELECTED_FILE's forbidden list for why
        # that distinction mattered here (a real confidently-wrong
        # collision was caught and fixed, not just a theoretical one).
        # Checked against the full corpus first: "markdown" appears in
        # exactly SAVE_CLIPBOARD_TO_FILE's own real phrasings and nowhere
        # else.
        "aliases": ["markdown"],
        "description": "The word 'markdown' specifically, as a target file format",
    },
    "OBJECT_QR_CODE": {
        "canonical_name": "QR_CODE",
        "category": "OBJECT",
        # v0.3.60 fix: GENERATE_QR_CODE/SCAN_QR_CODE were entirely
        # missing from this taxonomy -- it was built against a source
        # snapshot that predates the QR-code feature. Checked against the
        # full corpus first: "qr"/"qr code" appear in exactly these two
        # commands' own real phrasings and nowhere else, so a bare "qr"
        # single-word alias is safe alongside the multi-word "qr code"
        # (multi-word aliases are matched/consumed first -- see
        # component_extractor.py -- so "qr code this" still matches the
        # 2-word phrase as a unit; "qr" is a harmless backstop for any
        # future phrasing that drops "code").
        "aliases": ["qr code", "qr"],
        "description": "A QR code, as an object/image or concept",
    },
    "OBJECT_CLIPBOARD": {
        "canonical_name": "CLIPBOARD",
        "category": "OBJECT",
        # v2.2 fix (see "0.3.60 QR/clipboard-file gap" note at the top of
        # this file): "copied"/"what i copied" added as SAVE_CLIPBOARD_TO_
        # FILE's own real alternate way of naming clipboard content
        # (never a literal "clipboard" mention in half its corpus). Also
        # a genuine free improvement to GET_CLIPBOARD's own real phrasing
        # "whats copied right now", which previously had ZERO clipboard
        # signal at all under this taxonomy (bare "clipboard" word never
        # appears in it) and so could never be a component-router
        # candidate for that phrase before this fix. Checked against the
        # full corpus first: "copied" appears in exactly these two
        # commands' own real phrasings and nowhere else.
        "aliases": ["clipboard", "copied"],
        "description": "The system clipboard, or a reference to its current contents",
    },
    "OBJECT_WEATHER": {
        "canonical_name": "WEATHER",
        "category": "OBJECT",
        "aliases": ["weather", "raining", "cold", "jacket", "outside"],
        "description": "Current weather conditions",
    },
    "OBJECT_FORECAST": {
        "canonical_name": "FORECAST",
        "category": "OBJECT",
        "aliases": ["forecast", "this week", "tomorrow", "this weekend"],
        "description": "Multi-day weather forecast",
    },
    "OBJECT_TIME": {
        "canonical_name": "TIME",
        "category": "OBJECT",
        "aliases": ["time"],
        "description": "Current time",
    },
    "OBJECT_DATE": {
        "canonical_name": "DATE",
        "category": "OBJECT",
        "aliases": ["date", "day", "month"],
        "description": "Current date",
    },
    "OBJECT_LOCATION": {
        "canonical_name": "LOCATION",
        "category": "OBJECT",
        "aliases": ["location", "city"],
        "description": "Current geographic location",
    },
    "OBJECT_WEB": {
        "canonical_name": "WEB",
        "category": "OBJECT",
        "aliases": ["web", "google", "online"],
        "description": "The web, for SEARCH_WEB",
    },
    "OBJECT_DISK": {
        "canonical_name": "DISK",
        "category": "OBJECT",
        "aliases": ["disk", "drive", "storage"],
        "description": "Disk/storage space",
    },
    "OBJECT_RECYCLE_BIN": {
        "canonical_name": "RECYCLE_BIN",
        "category": "OBJECT",
        "aliases": ["recycle bin", "the trash"],
        "description": "The recycle bin",
    },
    "OBJECT_VOLUME": {
        "canonical_name": "VOLUME",
        "category": "OBJECT",
        "aliases": ["volume", "sound"],
        "description": "System audio volume",
    },
    "OBJECT_SCREEN": {
        "canonical_name": "SCREEN",
        "category": "OBJECT",
        "aliases": ["screen"],
        "description": "The screen/lock-screen",
    },
    "OBJECT_TASK_MANAGER": {
        "canonical_name": "TASK_MANAGER",
        "category": "OBJECT",
        "aliases": ["task manager", "taskmgr", "process manager"],
        "description": "Windows Task Manager",
    },
    "OBJECT_SCHEDULED_TASK": {
        "canonical_name": "SCHEDULED_TASK",
        "category": "OBJECT",
        "aliases": ["scheduled task", "scheduled tasks", "task scheduler", "tasks are scheduled"],
        "description": "Windows scheduled tasks",
    },
    "OBJECT_SERVICE": {
        "canonical_name": "SERVICE",
        "category": "OBJECT",
        "aliases": ["service"],
        "description": "A Windows service",
    },
    "OBJECT_PRINTER": {
        "canonical_name": "PRINTER",
        "category": "OBJECT",
        "aliases": ["printer", "printers"],
        "description": "A printer",
    },
    "OBJECT_USB": {
        "canonical_name": "USB",
        "category": "OBJECT",
        "aliases": ["usb"],
        "description": "USB devices",
    },
    "OBJECT_TEMPERATURE": {
        "canonical_name": "TEMPERATURE",
        "category": "OBJECT",
        "aliases": ["temperature", "temps", "hot", "cpu temp"],
        "description": "Hardware temperature sensors",
    },
    "OBJECT_BATTERY": {
        "canonical_name": "BATTERY",
        "category": "OBJECT",
        "aliases": ["battery"],
        "description": "Battery status",
    },
    "OBJECT_NETWORK": {
        "canonical_name": "NETWORK",
        "category": "OBJECT",
        "aliases": ["network", "ip", "wifi"],
        "description": "Network info",
    },
    "OBJECT_HOSTNAME": {
        "canonical_name": "HOSTNAME",
        "category": "OBJECT",
        "aliases": ["hostname", "this pc named", "computer name", "machine called on the network"],
        "description": "The computer's hostname",
    },
    "OBJECT_LOCALE": {
        "canonical_name": "LOCALE",
        "category": "OBJECT",
        "aliases": ["locale", "language", "region"],
        "description": "System locale/language settings",
    },
    "OBJECT_UPTIME": {
        "canonical_name": "UPTIME",
        "category": "OBJECT",
        "aliases": ["uptime", "last restart", "last reboot"],
        "description": "System uptime",
    },
    "OBJECT_SYSTEM_INFO": {
        "canonical_name": "SYSTEM_INFO",
        "category": "OBJECT",
        "aliases": ["system info", "specs", "os version", "windows version", "system specs"],
        "description": "System/OS specs",
    },
    "OBJECT_USERNAME": {
        "canonical_name": "USERNAME",
        "category": "OBJECT",
        "aliases": ["username", "logged in", "signed in", "account am i"],
        "description": "Current Windows username",
    },
    "OBJECT_INSTALLED_APPS": {
        "canonical_name": "INSTALLED_APPS",
        "category": "OBJECT",
        "aliases": ["installed", "apps", "applications", "programs do i have"],
        "description": "Installed applications",
    },
    "OBJECT_CODE": {
        "canonical_name": "CODE",
        "category": "OBJECT",
        "aliases": ["script", "code", "function", "poem", "python script", "calculator program"],
        "description": "Generated code/content (GENERATE_FILE)",
    },
    "OBJECT_UI_ELEMENT": {
        "canonical_name": "UI_ELEMENT",
        "category": "OBJECT",
        "aliases": ["button", "icon", "element", "field", "text box"],
        "description": "A clickable/typeable UI element",
    },
    "OBJECT_DUPLICATES": {
        "canonical_name": "DUPLICATES",
        "category": "OBJECT",
        "aliases": ["duplicate", "duplicates", "dupes", "exact copies"],
        "description": "Duplicate files",
    },
    "OBJECT_EXTENSION": {
        "canonical_name": "EXTENSION",
        "category": "OBJECT",
        # v2.1 fix (category 2, incorrect component definition): "by
        # type" REMOVED -- checked directly against FILE_TYPE_BREAKDOWN's
        # and GROUP_FILES_BY_EXTENSION's real corpus and neither ever
        # uses that exact bare phrase (they say "by extension"/"file
        # type"); "by type" only ever appears in SORT_FOLDER_BY_TYPE's
        # own corpus, which never required OBJECT_EXTENSION anyway. The
        # original inclusion caused a false tie between
        # SORT_FOLDER_BY_TYPE and FILE_TYPE_BREAKDOWN on "sort my desktop
        # by type" (both looked equally specific, so the router correctly
        # refused to guess -- but the underlying membership was wrong,
        # not the tie-break logic).
        "aliases": ["extension", "file type", "file types", "kinds of files"],
        "description": "File extension/type grouping",
    },
    "OBJECT_FILE_CONTENT": {
        "canonical_name": "FILE_CONTENT",
        "category": "OBJECT",
        "aliases": ["containing", "text pattern", "mention", "text inside", "inside files"],
        "description": "Searching inside file contents (FIND_FILES_BY_CONTENT)",
    },
    "OBJECT_CSV": {
        "canonical_name": "CSV",
        "category": "OBJECT",
        "aliases": ["csv", "spreadsheet"],
        "description": "A CSV/spreadsheet export",
    },
    "OBJECT_PATH": {
        "canonical_name": "PATH",
        "category": "OBJECT",
        "aliases": ["path"],
        "description": "A filesystem path",
    },
    "OBJECT_TOPIC": {
        "canonical_name": "TOPIC",
        "category": "OBJECT",
        "aliases": ["topic", "subject", "what theyre about"],
        "description": "Topical grouping (ORGANIZE_FILES_BY_TOPIC)",
    },
    "OBJECT_PROPERTIES": {
        "canonical_name": "PROPERTIES",
        "category": "OBJECT",
        "aliases": ["properties", "details", "how big", "last changed"],
        "description": "File/folder metadata",
    },
    "CONTEXT_CURRENT_LOCATION": {
        "canonical_name": "CURRENT_LOCATION_CONTEXT",
        "category": "CONTEXT",
        "aliases": ["current directory", "where am i", "currently in", "working directory", "what folder am i"],
        "description": "Asking what folder we're currently in",
    },
    "TARGET_NAME": {
        "canonical_name": "NAME",
        "category": "TARGET",
        "aliases": ["called", "named"],
        "description": "A given name for a new/renamed item",
    },
}


# ═══ Intent -> component requirements ══════════════════════════════════
# "required": ALL must be present. "any_of": at least ONE must be present
# (used only where a command's real phrasings genuinely split between two
# near-synonymous signals with no single word shared by all of them).
# "forbidden": presence of ANY of these disqualifies the candidate outright
# -- the component-world equivalent of graph_router.py's own
# _read_only_shadowed_by_action_verb guard, used only where two commands'
# real vocabularies genuinely overlap on a noun but differ on the verb.
INTENT_COMPONENT_MAP: Dict[str, Dict[str, List[str]]] = {
    "MAKE_FOLDER": {"required": ["ACTION_CREATE", "OBJECT_FOLDER"]},
    "MAKE_FILE": {
        "required": ["OBJECT_FILE"], "any_of": ["ACTION_CREATE", "ACTION_CREATE_FILE_ONLY"],
        # v0.3.60 fix: MAKE_FILE's own real corpus never mentions
        # "clipboard"/"copied" (verified directly) -- without this,
        # "make a md file out of my clipboard" ties SAVE_CLIPBOARD_TO_FILE
        # at equal specificity (OBJECT_FILE required + ACTION_CREATE
        # any_of = 2, vs SAVE_CLIPBOARD_TO_FILE's OBJECT_FILE + OBJECT_
        # CLIPBOARD both required = 2) and _resolve_tie has no principled
        # way to break it. Forbidding OBJECT_CLIPBOARD here can never cost
        # MAKE_FILE a single one of its own real phrasings.
        "forbidden": ["OBJECT_CLIPBOARD"],
    },
    "GENERATE_FILE": {
        "required": ["ACTION_GENERATE_CONTENT"],
        # v0.3.60 fix: same reasoning as MAKE_FILE's forbidden list just
        # above, plus OBJECT_QR_CODE -- GENERATE_FILE requires ONLY
        # ACTION_GENERATE_CONTENT (no object at all), so without this,
        # "generate a qr code" ties GENERATE_QR_CODE at equal specificity
        # (both score 1: GENERATE_FILE's sole required component vs
        # GENERATE_QR_CODE's sole required component). GENERATE_FILE's
        # own real corpus (poems/scripts/functions) never mentions
        # "clipboard"/"copied"/"qr" (verified directly against the full
        # corpus) -- forbidding both can never cost it a real phrasing.
        "forbidden": ["OBJECT_QR_CODE", "OBJECT_CLIPBOARD"],
    },
    "DELETE_ITEM": {"required": ["ACTION_DELETE"], "any_of": ["OBJECT_FILE", "OBJECT_FOLDER", "OBJECT_FILE_OR_FOLDER"]},
    "EMPTY_RECYCLE_BIN": {"required": ["ACTION_EMPTY", "OBJECT_RECYCLE_BIN"]},
    "RENAME_ITEM": {"required": ["ACTION_RENAME"]},
    "MOVE_ITEM": {"required": ["ACTION_MOVE"]},
    "COPY_ITEM": {"required": ["ACTION_COPY"], "any_of": ["OBJECT_FILE", "OBJECT_FOLDER", "OBJECT_FILE_OR_FOLDER"]},
    "LIST_FILES": {"required": ["OBJECT_FOLDER"], "any_of": ["ACTION_LIST", "ACTION_QUERY_GENERIC"]},
    "SORT_FOLDER_BY_TYPE": {"required": ["ACTION_ORGANIZE"], "forbidden": ["TARGET_NAME"]},
    "ORGANIZE_FILES_BY_TOPIC": {"required": ["ACTION_ORGANIZE", "OBJECT_TOPIC"]},
    "GROUP_FILES_BY_EXTENSION": {"required": ["OBJECT_EXTENSION", "TARGET_NAME"]},
    "FIND_FILES": {"required": ["ACTION_FIND", "OBJECT_FILE"], "forbidden": ["OBJECT_FILE_CONTENT"]},
    "FIND_FILES_BY_CONTENT": {"required": ["OBJECT_FILE_CONTENT"]},
    "FIND_DUPLICATE_FILES": {"required": ["OBJECT_DUPLICATES"]},
    "READ_FILE": {"required": ["OBJECT_FILE"], "any_of": ["ACTION_READ", "ACTION_QUERY_GENERIC"]},
    "OPEN_ITEM": {"required": ["ACTION_OPEN"], "any_of": ["OBJECT_FILE", "OBJECT_FOLDER", "OBJECT_FILE_OR_FOLDER"]},
    "DISK_USAGE": {"required": ["OBJECT_DISK"], "forbidden": ["ACTION_MODIFY"]},
    "COUNT_FILES": {"required": ["ACTION_COUNT", "OBJECT_FILE"]},
    "COUNT_FOLDERS": {"required": ["ACTION_COUNT", "OBJECT_FOLDER"]},
    "FILE_TYPE_BREAKDOWN": {"required": ["OBJECT_EXTENSION"], "forbidden": ["TARGET_NAME"]},
    "EXPORT_FOLDER_LISTING_CSV": {"required": ["OBJECT_CSV"]},
    "PATH_EXISTS": {"required": ["OBJECT_PATH"]},
    "RESOLVE_PATH": {"required": ["OBJECT_PATH"]},
    "SPLIT_PATH": {"required": ["OBJECT_PATH"]},
    "ITEM_PROPERTIES": {"required": ["OBJECT_PROPERTIES"]},
    "CURRENT_LOCATION": {"required": ["CONTEXT_CURRENT_LOCATION"]},

    "PROCESS_LIST": {"required": ["OBJECT_PROCESS"], "forbidden": ["ACTION_CLOSE", "ACTION_WAIT", "OBJECT_CPU_USAGE", "CONTEXT_SPECIFIC_PROCESS"]},
    "FIND_PROCESS": {"required": ["OBJECT_PROCESS"], "any_of": ["ACTION_FIND", "CONTEXT_SPECIFIC_PROCESS"], "forbidden": ["ACTION_CLOSE", "ACTION_WAIT"]},
    # v2.1 fix (category 2, incorrect component definition): OBJECT_PROCESS
    # DROPPED as a requirement for these two -- re-reading their own real
    # corpus directly found phrasings that never mention "process"/
    # "program"/"app" at all ("can u kill this for me", "this is stuck
    # close it", "let me know when this closes"). Requiring the
    # categorical noun was stricter than these commands' OWN training
    # data, independent of any out-of-vocabulary-target-name concern.
    # forbidden=[each other's action] keeps them from colliding on
    # phrasings that legitimately share "close" ("wait for this program
    # to close" contains both ACTION_WAIT and ACTION_CLOSE -- ACTION_WAIT
    # must win there, so KILL_PROCESS forbids it).
    # v2.1: re-tightened after a genuine regression was found in re-testing
    # (NOT a test-string patch -- "kill the lights" started false-
    # positiving once OBJECT_PROCESS stopped being required at all).
    # required=[ACTION_CLOSE] alone was too permissive; any_of restores a
    # real distinguishing signal (either the categorical noun, when
    # present, or KILL_PROCESS's own distress vocabulary for the
    # bare-pronoun phrasings) while still not demanding the OOV target
    # name itself. One known, disclosed gap this does NOT close: "can u
    # kill this for me" (a real KILL_PROCESS phrasing) has neither signal
    # at all once "this" is stopword-filtered -- TF-IDF only gets that
    # exact phrase right because it's memorized verbatim as training
    # data, which doesn't generalize to any OTHER contextless "kill this"
    # said about something unrelated. Left as an honest architectural
    # trade-off (see TESTING_REPORT_V2.md) rather than papered over.
    "KILL_PROCESS": {"required": ["ACTION_CLOSE"], "any_of": ["OBJECT_PROCESS", "CONTEXT_PROCESS_DISTRESS"], "forbidden": ["ACTION_WAIT"]},
    "WAIT_FOR_PROCESS": {"required": ["ACTION_WAIT"]},
    "TOP_PROCESSES_BY_CPU": {"required": ["OBJECT_CPU_USAGE"]},
    "OPEN_TASK_MANAGER": {"required": ["OBJECT_TASK_MANAGER"]},

    "SYSTEM_INFO": {"required": ["OBJECT_SYSTEM_INFO"], "forbidden": ["ACTION_MODIFY"]},
    # forbidden=["ACTION_MODIFY"]: migrated VERBATIM from graph_router.py's
    # own _READ_ONLY_LOOKALIKES set (== exactly {FIND_SERVICE, NETWORK_INFO,
    # LIST_USB_DEVICES} -- not applied more broadly than the reference
    # implementation itself applies it).
    "NETWORK_INFO": {"required": ["OBJECT_NETWORK"], "forbidden": ["ACTION_MODIFY"]},
    "CURRENT_USER": {"required": ["OBJECT_USERNAME"], "forbidden": ["ACTION_MODIFY"]},
    "GET_TIME": {"required": ["OBJECT_TIME"]},
    "GET_DATE": {"required": ["OBJECT_DATE"]},
    "GET_WEATHER": {"required": ["OBJECT_WEATHER"], "forbidden": ["OBJECT_FORECAST"]},
    "GET_FORECAST": {"required": ["OBJECT_FORECAST"]},
    "SEARCH_WEB": {"required": ["ACTION_FIND"], "any_of": ["OBJECT_WEB"]},
    "GET_LOCATION": {"required": ["OBJECT_LOCATION"], "forbidden": ["ACTION_MODIFY"]},
    "GET_CLIPBOARD": {"required": ["OBJECT_CLIPBOARD"], "any_of": ["ACTION_QUERY_GENERIC", "ACTION_READ"], "forbidden": ["ACTION_COPY", "ACTION_SET_CLIPBOARD"]},
    "SET_CLIPBOARD": {"required": ["OBJECT_CLIPBOARD"], "any_of": ["ACTION_COPY", "ACTION_SET_CLIPBOARD"]},
    "LIST_SCHEDULED_TASKS": {"required": ["OBJECT_SCHEDULED_TASK"], "forbidden": ["ACTION_MODIFY"]},
    "SYSTEM_UPTIME": {"required": ["OBJECT_UPTIME"]},
    "HOSTNAME": {"required": ["OBJECT_HOSTNAME"], "forbidden": ["ACTION_MODIFY"]},
    "FIND_SERVICE": {"required": ["OBJECT_SERVICE"], "forbidden": ["ACTION_MODIFY"]},
    "LIST_PRINTERS": {"required": ["OBJECT_PRINTER"], "forbidden": ["ACTION_MODIFY"]},
    "SYSTEM_LOCALE": {"required": ["OBJECT_LOCALE"], "forbidden": ["ACTION_MODIFY"]},
    "LIST_USB_DEVICES": {"required": ["OBJECT_USB"], "forbidden": ["ACTION_MODIFY"]},
    "TEMPERATURE_SENSORS": {"required": ["OBJECT_TEMPERATURE"], "forbidden": ["ACTION_MODIFY"]},
    "BATTERY_STATUS": {"required": ["OBJECT_BATTERY"], "forbidden": ["ACTION_MODIFY"]},
    "LIST_INSTALLED_APPS": {"required": ["OBJECT_INSTALLED_APPS"], "forbidden": ["ACTION_MODIFY"]},

    "TOGGLE_MUTE": {"required": ["ACTION_MUTE"]},
    "VOLUME_UP": {"required": ["ACTION_VOLUME_UP"]},
    "VOLUME_DOWN": {"required": ["ACTION_VOLUME_DOWN"]},
    "LOCK_WORKSTATION": {"required": ["ACTION_LOCK"]},
    "TAKE_SCREENSHOT": {"required": ["ACTION_SCREENSHOT"]},

    "LAUNCH_APP": {"any_of": ["ACTION_OPEN", "ACTION_START_PROGRAM"]},
    "CLICK_ELEMENT": {"required": ["ACTION_CLICK_BARE"], "forbidden": ["ACTION_DOUBLE_CLICK", "ACTION_RIGHT_CLICK", "ACTION_TYPE"]},
    "DOUBLE_CLICK_ELEMENT": {"required": ["ACTION_DOUBLE_CLICK"]},
    "RIGHT_CLICK_ELEMENT": {"required": ["ACTION_RIGHT_CLICK"]},
    "TYPE_INTO_ELEMENT": {"required": ["ACTION_TYPE"]},

    "COMPRESS_SELECTED_FILE": {"required": ["ACTION_COMPRESS"]},
    "EXTRACT_SELECTED_FILE": {"required": ["ACTION_EXTRACT"]},
    "CONVERT_SELECTED_FILE": {
        "required": ["ACTION_CONVERT"],
        # v0.3.60 fix: a REAL, live-verified collision, not theoretical
        # -- ACTION_CONVERT's own pre-existing multi-word alias "turn
        # this into a" is a literal substring of both "turn this into a
        # qr code" and "turn this into a markdown file". Before this
        # fix, CONVERT_SELECTED_FILE (required ACTION_CONVERT alone) won
        # BOTH confidently and WRONGLY -- worse than a miss, since
        # LayeredGraphRouter (orchestrator.py) trusts the component
        # router's first confident answer and never cross-checks
        # GraphRouter/TF-IDF once it has one. OBJECT_QR_CODE/OBJECT_
        # MARKDOWN are both checked against CONVERT_SELECTED_FILE's own
        # real corpus directly (tier_a_phrasings.py) -- neither word
        # appears in it even once (its own real phrasings use "pdf",
        # "text file", "different format", "different file type"), so
        # forbidding both here can never cost CONVERT_SELECTED_FILE one
        # of its own real phrasings, only remove a confident wrong answer
        # on someone else's vocabulary. With this forbidden clause, both
        # colliding phrases now correctly resolve to GENERATE_QR_CODE (a
        # real required-component match) or None (nothing else
        # qualifies for the markdown-file case -- correctly falls
        # through to GraphRouter/TF-IDF, which already gets it right).
        "forbidden": ["OBJECT_QR_CODE", "OBJECT_MARKDOWN"],
    },
    "RESIZE_SELECTED_FILE": {"required": ["ACTION_RESIZE"], "any_of": ["OBJECT_IMAGE"]},
    "DOWNLOAD_PLAYING_VIDEO": {"required": ["ACTION_DOWNLOAD"], "forbidden": ["TARGET_NAME"]},
    "DOWNLOAD_VIDEO_URL": {"required": ["ACTION_DOWNLOAD"]},

    # v0.3.60 additions -- GENERATE_QR_CODE/SCAN_QR_CODE/
    # SAVE_CLIPBOARD_TO_FILE were entirely missing from this taxonomy
    # (built against a source snapshot that predates the QR/clip-to-file
    # feature); confirmed 0.3.60 is a real Windows machine's ACTUAL
    # dispatchable Tier A intent set, diffed against INTENT_COMPONENT_MAP
    # directly (`MATCH (c:Command) WHERE c.tier = 'A' RETURN c.name` on
    # the real graph, minus this map's own keys) -- these 3 were the only
    # real gap found. Every alias/required/any_of/forbidden choice below
    # was derived the same way as everywhere else in this file: reading
    # each intent's own real phrasing corpus (tier_a_phrasings.py)
    # directly, cross-checked against the FULL corpus for collisions
    # before committing any bare-word alias (see OBJECT_QR_CODE/
    # ACTION_SCAN_OR_DECODE/OBJECT_CLIPBOARD's "copied" alias/OBJECT_
    # FILE's "markdown" alias above, each documents its own collision
    # check inline). GENERATE_QR_CODE requires ONLY OBJECT_QR_CODE
    # (no action) so a bare "qr code this"/"turn this into a qr code"
    # (no create/generate verb) still resolves -- SCAN_QR_CODE's own
    # any_of (ACTION_READ/ACTION_QUERY_GENERIC/ACTION_SCAN_OR_DECODE)
    # naturally outscores it via specificity whenever a real scan/read/
    # decode/query verb is present, so no forbidden clause was needed
    # between the two.
    "GENERATE_QR_CODE": {"required": ["OBJECT_QR_CODE"]},
    "SCAN_QR_CODE": {"required": ["OBJECT_QR_CODE"], "any_of": ["ACTION_READ", "ACTION_QUERY_GENERIC", "ACTION_SCAN_OR_DECODE"]},
    "SAVE_CLIPBOARD_TO_FILE": {"required": ["OBJECT_CLIPBOARD"], "any_of": ["OBJECT_FILE", "OBJECT_MARKDOWN"]},
}
