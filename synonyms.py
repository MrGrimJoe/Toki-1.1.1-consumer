"""
synonyms.py -- offline curated synonym table for graph_router.py's Tier A
word-overlap matching. Closes the gap PROJECT_STATE_OVERVIEW.md (BETA
0.3.37) lists under "what it does NOT have": "The offline curated-synonym-
table idea (raised in chat, for Tier A's out-of-vocabulary-word problem)
is still just a discussion -- no code exists for it either way."

── The problem this closes ─────────────────────────────────────────────
Tier A's entire matchable vocabulary comes from TIER_A_PHRASINGS
(graph_source_data/tier_a_phrasings.py) -- 61 commands, ~255 distinct
words, hand-written by a human. A perfectly reasonable word a user might
actually type ("erase this file", "clone this folder") can be completely
ABSENT from that vocabulary even though a clearly-matching Tier A command
exists, simply because nobody happened to write that exact word into a
phrasing. When that happens the word contributes ZERO signal to every
command's TF-IDF score -- not partial credit, nothing -- which can push a
genuinely correct match below CONFIDENCE_THRESHOLD, or lose a close
ranking it should have won. Confirmed concretely: "erase notes.txt"
scores 0.0 confidence against every Tier A command before this change
(verified in tests/test_synonyms.py), because "erase" appears nowhere in
TIER_A_PHRASINGS at all -- not a near-miss, a complete miss.

── Why a fixed table, not a live thesaurus/synonym API ────────────────
Same reasoning wcl_resolver.py's abbreviation retry already uses a fixed
curated table instead of a live lookup for the same class of problem:
predictable, fully offline, reviewable in a diff, and immune to a
synonym service ever mapping a word to something semantically unsafe
(e.g. a "helpful" synonym service deciding "clear" and "delete" are
related). Every entry below is a deliberate, reviewed choice, not an
inferred or auto-generated one.

── Why this table is small, and how entries were chosen ───────────────
This is NOT meant to be an exhaustive synonym dictionary. Every entry
was picked because:
  (a) the key is a real word that is CONFIRMED ABSENT from
      TIER_A_PHRASINGS's actual current vocabulary (checked directly
      against it, not guessed), and
  (b) the target word is tied to few enough Tier A commands (usually
      exactly one) that adding the synonym doesn't meaningfully increase
      cross-command ambiguity -- e.g. "disk" is tied to DISK_USAGE alone,
      so "storage" -> "disk" can't accidentally strengthen a WRONG
      command's score, only the one correct one.
Deliberately excluded: generic multi-purpose verbs ("run", "get", "do",
"open") that already carry too many unrelated meanings across Tier A AND
Tier B/WCL to safely collapse onto a single target -- see the module
docstring note in graph_router.py about "run diskpart"-shaped risk if a
generic verb were ever mapped onto a Tier A action like LAUNCH_APP.

── Usage contract ──────────────────────────────────────────────────────
expand_synonyms() must ONLY be used to build the word set handed to
graph-vocabulary matching/scoring (GraphRouter._fetch_dispatchable_matches
/ _best_command). Anything that needs the user's LITERAL words for a
different purpose -- the read-only-lookalike action-verb-shadow guard,
or the unmatched-word text in a clarifying question -- must keep using
the original, un-expanded word set unchanged, and use
is_matched_via_synonym() below to correctly treat a synonym-covered word
as "understood" without re-expanding that set itself. Expanding the
words used by the action-verb guard would silently change ITS behavior
too (a tested, safety-relevant guard) as an unintended side effect of a
vocabulary-coverage fix -- keeping the two paths on separate word sets
is what prevents that.
"""

from typing import Dict, Set

# key: a real word a user might type that does NOT appear anywhere in
# TIER_A_PHRASINGS today. value: an existing TIER_A_PHRASINGS vocabulary
# word it should be treated as equivalent to for matching purposes only.
#
# If a key below is ever added to TIER_A_PHRASINGS directly as real
# phrasing vocabulary, its entry here becomes a harmless no-op (the word
# would already match on its own) -- safe to leave in place or remove.
SYNONYM_MAP: Dict[str, str] = {
    # DELETE_ITEM / EMPTY_RECYCLE_BIN -- "delete" is already tied to both
    # of these in TIER_A_PHRASINGS, so these spread across both exactly
    # like "delete" itself already does; no new ambiguity introduced.
    "erase": "delete",
    "destroy": "delete",
    "discard": "delete",
    # "trash" and "rid" removed (BETA 0.3.51 casual-phrasing expansion):
    # both are now real TIER_A_PHRASINGS vocabulary directly ("trash
    # this"/"get rid of this file" were added to DELETE_ITEM's own
    # phrasing corpus), so the synonym entries became pure no-ops --
    # caught by test_synonyms.py::test_every_key_is_currently_absent_
    # from_tier_a_vocabulary, which exists specifically to catch this.
    # COPY_ITEM -- "copy" is also tied to GET_CLIPBOARD/SET_CLIPBOARD in
    # TIER_A_PHRASINGS already, but "replicate" is an unambiguously
    # file-duplication word, not a clipboard word, so it doesn't add new
    # confusion beyond what "copy" itself already carries.
    "replicate": "copy",
    # "clone" removed, same reason as "trash"/"rid" above -- "clone this
    # file" is now real COPY_ITEM vocabulary.
    # MOVE_ITEM -- "move" is tied to nothing else in TIER_A_PHRASINGS.
    "transfer": "move",
    # RENAME_ITEM -- "rename" is tied to nothing else.
    "relabel": "rename",
    # FIND_FILES / FIND_PROCESS / FIND_DUPLICATE_FILES /
    # FIND_FILES_BY_CONTENT -- "find" is already shared across these four
    # in TIER_A_PHRASINGS; "locate" spreads the same way, no new spread.
    "locate": "find",
    # FIND_FILES_BY_CONTENT specifically -- "files that mention X" is a
    # common real phrasing with no other content-search word in it.
    # First attempt mapped this to the generic "find" target shared by
    # FIND_FILES/FIND_PROCESS/FIND_DUPLICATE_FILES/FIND_FILES_BY_CONTENT
    # alike -- confirmed live that's too generic, it just shifted the
    # misroute to whichever other FIND_* command happened to score
    # highest (FIND_DUPLICATE_FILES), not the right one. "containing" is
    # FIND_FILES_BY_CONTENT's own word and appears in no other command's
    # phrasing corpus (checked tier_a_phrasings.py directly), so this
    # maps to the specific command instead of the whole find-family.
    # ("mention" singular removed -- now real vocabulary, see above.
    # "mentions" plural is still genuinely absent, kept.)
    "mentions": "containing",
    # DISK_USAGE -- "disk" is tied to nothing else. ("storage" removed:
    # "hows my storage looking" is now real DISK_USAGE vocabulary.)
    # TOGGLE_MUTE -- "mute" is tied to nothing else. ("silence" removed:
    # "silence it" is now real TOGGLE_MUTE vocabulary -- "silence the
    # volume" is a confident direct classify() hit now, not a
    # below-threshold synonym-assisted candidate; see
    # test_synonyms.py::TestSynonymClosesRealOOVMisses for the updated
    # expectation.)
    # LIST_INSTALLED_APPS -- "apps" is tied to nothing else.
    "software": "apps",
}


def expand_synonyms(words: Set[str]) -> Set[str]:
    """Returns a NEW set: `words` plus, for every word that has a curated
    synonym target, that target word too. Purely additive -- never
    removes or replaces the original word, so a word that already
    matches something on its own is completely unaffected."""
    expanded = set(words)
    for w in words:
        target = SYNONYM_MAP.get(w)
        if target:
            expanded.add(target)
    return expanded


def is_matched_via_synonym(word: str, matched_words: Set[str]) -> bool:
    """True if `word` should count as matched/understood because ITS
    curated synonym target is present in `matched_words`, even though
    `word` itself is not. Lets a caller working with the ORIGINAL
    (non-expanded) word set correctly recognize a synonym-covered word
    without expanding that set itself -- see this module's docstring,
    "Usage contract"."""
    target = SYNONYM_MAP.get(word)
    return target is not None and target in matched_words
