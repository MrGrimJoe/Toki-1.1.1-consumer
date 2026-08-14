"""
graph_router.py -- first-pass intent router, sits in front of
OllamaRouter.classify() inside orchestrator.py.

Returns the EXACT same shape OllamaRouter.classify() returns:
    {"intent": "MAKE_FOLDER"}   on a confident hit
    {"error": "..."}            never returned by this router (graph
                                 has no failure mode that maps to a
                                 real error -- a miss just means "ask
                                 OllamaRouter instead", handled by the
                                 caller, not by this class)
    None                        on a miss -- the caller's signal to
                                 fall through to OllamaRouter.classify()
                                 unchanged.

Only intents that exist in TOKI's real INTENTS dict (intents.py +
intents_extended.py + intents_app_control.py, 60 total as of v2.11 --
59 dispatchable commands + ASK_CONTEXT) can ever be returned -- the
graph also holds ~1,160 broader Windows commands (Tier B, from
windows_command_library.json) for future expansion, but those are NOT
wired into dispatch yet because TOKI's executor/extractor only know
how to run Tier A's 59 templates. A Tier B hit is deliberately treated
as a miss for now -- see _is_dispatchable() below. Wiring Tier B in
for real means teaching extractor.py/executor.py to run raw wcl syntax
strings, which is a separate, bigger piece of work than this
integration.

── Two-tier matching (mirrors orchestrator.py's LLM category/command
   split) ──────────────────────────────────────────────────────────
Tier 1 picks a CATEGORY (FILESYSTEM/PROCESS/SYSTEM/INFO/GENERATE/
APP_CONTROL -- see categories.py; CHAT and ASK_CONTEXT are never
graph-matched, see NON_GRAPH_CATEGORIES below) by unioning, per
category, the set of DISTINCT query words matched by ANY Tier A
command in that category, and scoring coverage = |union| / |query
words|. This is a genuinely different signal from tier 2: it asks "is
this the right neighborhood" using every command's vocabulary in a
category collectively, not just whichever single command happens to
score highest.

Tier 2 then picks the single best COMMAND inside the winning category
only, scored the old way (that command's own matched-word count /
query word count).

Both stages are scoped to Tier A only (c.tier = 'A'), which also fixes
a real bug the previous single-tier version had: because the old
query matched across the FULL graph (Tier A + Tier B, ~1,219 nodes)
and only ever looked at the single top-ranked row, a Tier B command
outscoring the correct Tier A one (very likely -- Tier B has ~20x
more nodes competing for the same words) would silently eat the top
slot and discard the correct Tier A match sitting at rank 2-5. Scoping
both tiers to Tier A removes Tier B from the scoring entirely, not
just from the final dispatch check.

Known limitation: there is no separate category-level phrasing corpus
in the graph (the checkpoint only ever built per-COMMAND phrasings/
words) -- so tier 1's category vocabulary is entirely derived from
Tier A's existing thin, auto-generated per-command word sets, just
aggregated differently. This is still a real, distinct signal from
tier 2 (see above), but it isn't independently-collected category
data. Worth building real category-level phrasings if tier 1 turns
out to need its own tuning separate from tier 2's.

IMPORTANT, found while wiring this up (now fixed at the source, not
worked around): the ORIGINAL graph checkpoint's Tier A commands had
ZERO HAS_INTENT edges -- all 3,591 HAS_INTENT edges belonged to Tier B
only (verified directly against the old db: `MATCH
(c:Command)-[:HAS_INTENT]->(i:Intent) RETURN c.tier, count(i)` returned
only a 'B' row). Tier A's only real data was 118 thin, auto-generated
Phrasing nodes. This meant the ORIGINAL single-tier traversal match
could never actually surface a Tier A command through fuzzy matching
-- every traversal result it ever returned was Tier B, silently
discarded by `_is_dispatchable()`. Tier A was only reachable through
an exact phrasing string match.

Fixed properly in migrate_to_kuzu.py (a reconstructed build script --
see its own docstring) and graph_source_data/tier_a_phrasings.py: the
graph is now rebuilt with real HAS_INTENT edges for Tier A too (469 of
them, derived from an expanded, bug-fixed phrasing set), a fixed
normalize() that replaces separator characters with a space instead
of deleting them (the earlier "folder/directory" -> "folderdirectory"
glue bug), and 179 Tier A phrasings instead of 118. `_fetch_tier_a_matches`
below queries HAS_INTENT directly, same as it always should have been
able to -- if you're running against an OLDER toki_graph_db that
predates this, rebuild it: `python3 migrate_to_kuzu.py`.
"""

import re
import math
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

import kuzu

from synonyms import expand_synonyms, is_matched_via_synonym

DB_PATH = Path(__file__).parent / "toki_graph_db"

# Below this score, a candidate is not trusted enough to execute on --
# treated as a miss so OllamaRouter (and its existing no-guessing,
# ask-a-fixed-question behavior) still applies.
#
# Scoring is TF-IDF + cosine similarity over each command's phrasing text
# (see _build_tfidf_index() / _best_command()), replacing the older
# specificity-sum formula (1 / commands-a-word-appears-under, summed per
# candidate). That formula let a single rare FILLER word (e.g. "clear",
# "called", "stop") outvote a real multi-word match on a command's actual
# defining vocabulary, because summing independent per-word scores
# doesn't care how much of a command's own phrasing the query actually
# covers -- only whether isolated words happen to be rare. Confirmed
# concretely: "clear the screen" hit EMPTY_RECYCLE_BIN at 0.75 confidence
# (auto-dispatches -Force, zero slots, no LLM in the loop) purely because
# "clear" was the ONLY word in the whole Tier A vocabulary that maps to
# EMPTY_RECYCLE_BIN, so it got full weight while "screen"'s three-way
# ambiguity split it down. Cosine similarity doesn't have this failure
# mode by construction: a command's score is the ANGLE between the query
# vector and that command's own full phrasing vector, so matching one
# rare word gives you a thin sliver of that command's vector, while
# matching two-plus words that actually co-occur in its real phrasings
# (e.g. "make" + "folder" for MAKE_FOLDER) covers much more of it and
# wins on that basis -- no per-pair tuning table required, and it
# generalizes as new commands/phrasings are added rather than needing
# re-tuning per collision.
#
# 0.4 was picked from real evidence, not a guess: known-bad matches (the
# false positive above, plus "stop the music" -> KILL_PROCESS, "go to
# the store" -> LOCK_WORKSTATION, "remove this annoying popup" ->
# DELETE_ITEM) scored 0.23-0.35 under this scheme; known-good matches
# ("make a folder called test" -> MAKE_FOLDER, "what's the weather" ->
# GET_WEATHER, "what's the forecast" -> GET_FORECAST, "empty the recycle
# bin" -> EMPTY_RECYCLE_BIN) scored 0.51-0.83. 0.4 sits in the real gap
# between those two clusters.
#
# Known remaining limitation, NOT fixed by this scoring change: commands
# whose only real distinguishing feature is an out-of-vocabulary SLOT
# VALUE (KILL_PROCESS needs a process name, LAUNCH_APP needs an app name,
# TYPE_INTO_ELEMENT needs arbitrary typed text) can't be disambiguated by
# word-overlap scoring at all -- "kill notepad" and "kill the lights"
# score identically (0.311, both below threshold), because the graph's
# vocabulary was never taught what a process name looks like, only the
# verb "kill". Fixing that needs slot-extraction success as a gate
# (extract_slots() actually pulling a real value) layered on top of this
# score, not a better similarity formula -- flagged here, not solved.
#
# Also found and NOT fixed by this change, separate bug: GENERATE_FILE
# has zero Phrasing nodes in the current graph checkpoint -- it was never
# given any when the graph was built, so no scoring formula can select it
# ("write a poem to a file" falls through to READ_FILE, 0.659, because
# GENERATE_FILE was never in the running at all). This needs phrasings
# added at the data/migrate_to_kuzu.py level, not a graph_router.py fix.
#
# Revisit CONFIDENCE_THRESHOLD before trusting this on a much wider
# command set (e.g. if Tier B is ever wired into dispatch).
CONFIDENCE_THRESHOLD = 0.5

# CHAT and ASK_CONTEXT are judgment calls about the SHAPE of the message
# (is there a request here at all / is a detail missing) -- not "does this
# text match a known command phrasing." There's no command/phrasing data
# for either in the graph, so the graph never returns them; a miss here
# just falls through to the LLM's tier-1 call same as always.
NON_GRAPH_CATEGORIES = {"CHAT", "ASK_CONTEXT"}

STOPWORDS = {
    "a", "an", "the", "please", "me", "my", "can", "you", "could",
    "would", "to", "of", "for", "and", "is", "it", "this", "that",
    "on", "in", "at", "do", "i", "want", "need",
    # BETA 0.3.47: interrogative/discourse fillers. These were content
    # words before this change, which is the actual mechanism behind the
    # "does 'mexico'/'capital' mean list files" class of bug (see
    # FIXES_APPLIED_0.3.46.md) -- "what"/"does"/"mean" carry zero signal
    # about WHICH command a user wants (no Tier A command is defined by
    # them), but they DO show up in general-knowledge questions and
    # greetings constantly, so leaving them as content words let coincidental
    # overlap on a real command's phrasing text (e.g. "does" appearing in
    # some other phrasing) manufacture a fake candidate out of pure noise.
    # Confirmed directly: stripping these turns "what is the capital of
    # mexico" and "what does mexico and capital mean" from a false
    # LIST_FILES/PATH_EXISTS candidate into a genuine total miss (0
    # dispatchable words), and does NOT break real command phrasings that
    # happen to use these words too ("what's the weather" -> GET_WEATHER,
    # "what's my ip address" -> NETWORK_INFO, "who am i logged in as" ->
    # CURRENT_USER all still hit correctly) because those commands are
    # carried by their own domain nouns (weather/ip/address/logged), not
    # by the interrogative word itself.
    "what", "why", "who", "when", "where", "which", "how",
    "does", "did", "doing", "was", "were", "be", "been", "being",
    "mean", "means", "meant",
    "hi", "hello", "hey", "going", "there",
}


# Common executable/file-extension suffixes stripped by normalize() below,
# so a process/file name keeps scoring the same with or without its
# extension -- "kill notepad.exe" should produce the same content words as
# "kill notepad", not treat "notepad" and "exe" as two unrecognized tokens.
# Found live: extractor.py already strips ".exe" from slot VALUES after a
# command is chosen, but routing happens first -- without this, ".exe"-
# suffixed phrasings missed the graph entirely (0 candidates), so the
# extraction-level fix never got a chance to run. Keep this list short and
# genuinely common; it's a normalization aid, not a slot-value guesser.
_STRIPPED_SUFFIXES = (".exe", ".txt", ".docx", ".xlsx", ".pdf", ".jpg", ".png")


def normalize(text: str) -> str:
    text = text.lower().strip()
    for suffix in _STRIPPED_SUFFIXES:
        text = text.replace(suffix, " ")
    # Replace (not delete) anything that isn't a word/space/brace/hyphen --
    # deleting silently glued separators together (e.g. "folder/directory"
    # became "folderdirectory", one useless token instead of two matchable
    # words). Found by inspecting the shipped graph's own Phrasing.text
    # values directly; fixed here AND at the source in migrate_to_kuzu.py
    # so it can't quietly reappear on a future rebuild.
    text = re.sub(r"[^\w\s{}\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def content_words(norm_text: str) -> set:
    return {w for w in norm_text.split() if w and w not in STOPWORDS}


# BETA 0.3.27 fix -- confirmed live: "stop the print spooler service" ->
# FIND_SERVICE, "reset network adapter" -> NETWORK_INFO, "format the usb
# drive" -> LIST_USB_DEVICES. All three are READ-ONLY lookups that share
# their NOUN vocabulary (service/network/usb) with a genuinely different
# WRITE action the user actually asked for -- the query's action verb
# ("stop"/"reset"/"format") isn't anywhere in the read command's own
# phrasing corpus, so it contributes nothing to that command's score, but
# it also doesn't lower it either: the noun match alone clears
# CONFIDENCE_THRESHOLD. NETWORK_INFO and LIST_USB_DEVICES have zero
# required slots, so this silently ran the wrong (harmless-but-wrong) read
# instead of asking or falling through to wcl_resolver.py, which is where
# the real write command actually lives.
#
# Fix: after a read-only lookalike wins, check whether the query ALSO
# contains a write/action verb that ISN'T part of that command's own
# vocabulary. If so, treat it as a miss (return None / fall through) --
# same principle as the CONFIDENCE_THRESHOLD gate, just checking verb
# agreement instead of overall score. Deliberately a short, explicit list
# on both sides (not a general grammar/verb classifier) so it can't
# silently start blocking legitimate future phrasings for these same
# three commands -- if a real phrasing for FIND_SERVICE ever legitimately
# needs one of these words, add it to that command's OWN phrasing corpus
# in tier_a_phrasings.py and this guard stops firing for it automatically
# (see GraphRouter._read_only_shadowed_by_action_verb's docstring).
_ACTION_VERBS = {
    "stop", "start", "restart", "reset", "format", "disable", "enable",
    "delete", "remove", "kill", "wipe", "erase", "uninstall", "turn",
}

_READ_ONLY_LOOKALIKES = {"FIND_SERVICE", "NETWORK_INFO", "LIST_USB_DEVICES"}


class GraphRouter:
    def __init__(self, db_path: Path = DB_PATH):
        self.db = kuzu.Database(str(db_path))
        self.conn = kuzu.Connection(self.db)
        # (command -> L2-normalized tf-idf vector, word -> idf), built once
        # lazily on first classify() and cached -- see _get_tfidf_index().
        # Only invalidated by constructing a new GraphRouter (matches the
        # old behavior: the graph itself is treated as static per-process,
        # rebuilt only via migrate_to_kuzu.py + a restart).
        self._tfidf_cache: Optional[Tuple[Dict[str, Dict[str, float]], Dict[str, float]]] = None

    def _read_only_shadowed_by_action_verb(self, command: Optional[str], words: set) -> bool:
        """True if `command` is one of _READ_ONLY_LOOKALIKES and the query
        used an _ACTION_VERBS word that ISN'T part of that command's own
        real phrasing vocabulary -- see the module-level comment above
        _ACTION_VERBS for the concrete bugs this closes. Deliberately
        checks against the command's OWN vocabulary (not just presence in
        `words`) so a future legitimate phrasing that adds one of these
        verbs to that command's corpus automatically stops tripping this
        guard, with no separate allowlist to maintain."""
        if command not in _READ_ONLY_LOOKALIKES:
            return False
        action_words = words & _ACTION_VERBS
        if not action_words:
            return False
        vectors, _ = self._get_tfidf_index()
        cmd_vocab = set(vectors.get(command, {}))
        return not (action_words & cmd_vocab)

    def _is_dispatchable(self, tier: str) -> bool:
        """Only tier A (TOKI's original 59 intents) lives in toki_graph_db
        now. windows_command_library matching (formerly tiers A2/B here)
        moved to its own dedicated, better-built graph -- see
        wcl_resolver.py's module docstring for why."""
        return tier == "A"

    def classify(self, user_prompt: str) -> Optional[Dict[str, Any]]:
        """Matches OllamaRouter.classify()'s return contract on a hit.
        Returns None on a miss -- caller falls through to the LLM.

        BETA 0.3.3 fix: NON_GRAPH_CATEGORIES was declared and documented
        (module docstring, line ~32) as categories the graph should never
        return, but nothing ever actually checked it -- CHAT has a real,
        if thin, auto-generated Phrasing node (e.g. matching on the bare
        word "hey"), so a message like "hey how's it going" was silently
        graph-hitting CHAT and skipping the LLM call entirely. That
        matters because CHAT's response is thinking_text verbatim with no
        LLM-side reasoning behind it at all when it arrives this way --
        confirmed live: a graph-hit CHAT turn produced a templated stub
        ("(said HELLO, awaiting user's response)") instead of an actual
        reply. Every returned intent is now checked against
        NON_GRAPH_CATEGORIES via _intent_meta before being trusted."""
        norm = normalize(user_prompt)
        if not norm:
            return None

        # An exact phrasing match resolves both tiers in one step -- it IS
        # a specific command already, category is just metadata about
        # which bucket it lives in, not a separate decision at this point.
        exact = self._exact_match(norm)
        if exact and self._is_dispatchable(exact["tier"]):
            intent = self._to_intent_name(exact["id"])
            if intent not in NON_GRAPH_CATEGORIES:
                return {"intent": intent}

        words = content_words(norm)
        if not words:
            return None

        # match_words feeds ONLY the graph-vocabulary lookup/scoring below
        # (see synonyms.py's "Usage contract") -- the action-verb-shadow
        # guard a few lines down deliberately keeps using the original,
        # un-expanded `words` so this coverage fix can't silently change
        # that separate, already-tested guard's behavior.
        match_words = expand_synonyms(words)

        matches = self._fetch_dispatchable_matches(match_words)
        if not matches:
            return None

        command, confidence = self._best_command(matches, len(match_words))
        if command is None or confidence < CONFIDENCE_THRESHOLD:
            return None
        if command in NON_GRAPH_CATEGORIES:
            return None
        if self._read_only_shadowed_by_action_verb(command, words):
            return None

        return {"intent": command}

    def classify_or_ask(self, user_prompt: str) -> Dict[str, Any]:
        """Command-routing entry point for the LLM-free testing phase (see
        orchestrator.py's _process_single_request -- this REPLACES the old
        graph-miss-falls-through-to-OllamaRouter path for command-shaped
        requests; CHAT/GENERATE still go to the LLM, untouched).

        Always returns one of three shapes, never None:
            {"intent": "MAKE_FOLDER"}                    -- confident hit,
                                                              same as classify()
            {"ask": "...", "unknown_words": [...]}        -- unsure: a
                                                              clarifying
                                                              question plus
                                                              the specific
                                                              query words
                                                              that didn't
                                                              match anything,
                                                              for the
                                                              like/dislike ->
                                                              staging-DB loop
            {"ask": "...", "unknown_words": []}            -- total miss,
                                                              nothing in the
                                                              query matched
                                                              any command
                                                              vocabulary at
                                                              all (empty
                                                              prompt after
                                                              normalization
                                                              also lands here)

        No LLM call anywhere in this method -- the question text is built
        from data _fetch_dispatchable_matches/_best_command already
        computed, not generated by a model.
        """
        norm = normalize(user_prompt)
        if not norm:
            return {"ask": "I didn't catch that -- can you rephrase?", "unknown_words": []}

        exact = self._exact_match(norm)
        if exact and self._is_dispatchable(exact["tier"]):
            intent = self._to_intent_name(exact["id"])
            # Same fix as classify() above -- an exact CHAT/ASK_CONTEXT
            # phrasing match shouldn't short-circuit here either, even on
            # this fail-open path (only reached when the real LLM call
            # itself errored -- see this method's own docstring).
            if intent not in NON_GRAPH_CATEGORIES:
                return {"intent": intent}

        words = content_words(norm)
        if not words:
            return {"ask": "I didn't catch that -- can you rephrase?", "unknown_words": []}

        # match_words feeds ONLY the graph-vocabulary lookup/scoring below
        # (see synonyms.py's "Usage contract") -- the action-verb-shadow
        # guard and the unmatched-word reporting further down deliberately
        # keep using the original, un-expanded `words` (via
        # is_matched_via_synonym() for the latter) so this coverage fix
        # can't silently change that separate, already-tested guard's
        # behavior, or misreport a synonym-covered word as unknown.
        match_words = expand_synonyms(words)

        matches = self._fetch_dispatchable_matches(match_words)
        if not matches:
            # Nothing in the query matched ANY command's vocabulary, even
            # after synonym expansion -- every content word is "unknown"
            # to the graph, not just some of them.
            return {
                "ask": f"I didn't catch that -- what are you trying to do with \"{user_prompt.strip()}\"?",
                "unknown_words": sorted(words),
            }

        command, confidence = self._best_command(matches, len(match_words))
        # A CHAT/ASK_CONTEXT "command" isn't a real actionable candidate --
        # treat it exactly like no command at all, BEFORE any of the
        # confidence/ask-text logic below runs. Without this, a confident
        # CHAT match would either short-circuit as a fake dispatchable
        # intent (the bug this whole fix targets) or, once blocked from
        # that, fall into the below-threshold branch and produce a
        # nonsensical "does X mean you want to chat?" question -- and the
        # caller (orchestrator.py's pre-LLM gate) reads this dict's
        # "candidate" key as a real action-shaped signal, so a leftover
        # CHAT here would wrongly force a clarifying question onto a
        # genuine greeting instead of letting it reach the LLM normally.
        if command in NON_GRAPH_CATEGORIES:
            command = None
        if command is not None and self._read_only_shadowed_by_action_verb(command, words):
            command = None
        matched_words = {w for name, w in matches if name == command} if command else set()
        unknown_words = sorted(
            w for w in words
            if w not in matched_words and not is_matched_via_synonym(w, matched_words)
        )

        if command is not None and confidence >= CONFIDENCE_THRESHOLD:
            return {"intent": command}

        # Below threshold: the graph HAS a leading guess (command) but isn't
        # sure enough to auto-dispatch a real side-effecting action on it.
        # Ask about specifically the words that didn't match ITS vocabulary
        # -- that's exactly the vocab gap the like/dislike loop is meant to
        # close, per the 👍/👎-into-staging-DB design.
        if command and unknown_words:
            word_list = ", ".join(f'"{w}"' for w in unknown_words)
            ask = f"Oh, I didn't catch that -- does {word_list} mean you want to {command.replace('_', ' ').lower()}?"
        elif command:
            # Every query word matched SOME command's vocabulary already,
            # confidence is just spread thin across close candidates (e.g.
            # a near-tie) -- not a vocabulary gap, so don't blame a specific
            # word for it.
            ask = f"I'm not fully sure -- did you want to {command.replace('_', ' ').lower()}?"
        else:
            ask = f"I didn't catch that -- what are you trying to do with \"{user_prompt.strip()}\"?"

        return {"ask": ask, "unknown_words": unknown_words, "candidate": command}

    @staticmethod
    def _to_intent_name(graph_command_id: str) -> str:
        """Tier A command ids are stored in the graph as f"A_{name}"
        (see migrate_to_kuzu.py's load_tier_a) to keep them distinct
        from Tier B ids during migration. TOKI's real INTENTS dict is
        keyed on the bare name (e.g. "MAKE_FOLDER"), so the prefix
        must be stripped before this ever reaches orchestrator.py --
        returning it unstripped causes an immediate KeyError on
        INTENTS[intent]."""
        return graph_command_id[2:] if graph_command_id.startswith("A_") else graph_command_id

    def _exact_match(self, norm_text: str) -> Optional[dict]:
        result = self.conn.execute("""
            MATCH (p:Phrasing)-[:RESOLVES_TO]->(c:Command)
            WHERE p.text = $text
            RETURN c.id, c.tier
            ORDER BY c.tier ASC
        """, {"text": norm_text})
        if not result.has_next():
            return None
        row = result.get_next()
        return {"id": row[0], "tier": row[1]}

    def _fetch_dispatchable_matches(self, words: set) -> List[Tuple[str, str]]:
        """Every (intent name, word) pair among DISPATCHABLE commands
        (tier A or A2 -- see _is_dispatchable) where that word is one of
        the query's content words. Tier B is excluded from scoring
        entirely here, not just at the final dispatch check, for the same
        reason the original single-tier bug mattered: Tier B has ~4x more
        nodes than A+A2 combined competing for the same words, and would
        silently outscore the correct dispatchable match otherwise.

        Single flat scoring across A+A2 (no separate category pre-filter)
        -- the old two-stage category-then-command split depended on
        INTENT_CATEGORY, which only maps TOKI's original 59 intents, not
        the windows_command_library-sourced A2 commands. Their own
        `category` field is still stored on the Command node (26 buckets
        from the source file) for future use, but isn't needed for
        matching now that Tier B is out of the running -- ~340
        dispatchable candidates is small enough for direct per-command
        scoring to be both simpler and no less precise."""
        placeholders = ", ".join(f"$w{i}" for i in range(len(words)))
        params = {f"w{i}": w for i, w in enumerate(words)}

        query = f"""
            MATCH (c:Command)-[:HAS_INTENT]->(i:Intent)
            WHERE c.tier = 'A' AND i.word IN [{placeholders}]
            RETURN c.id, i.word
        """
        result = self.conn.execute(query, params)
        pairs = []
        while result.has_next():
            row = result.get_next()
            pairs.append((self._to_intent_name(row[0]), row[1]))
        return pairs

    def _get_tfidf_index(self) -> Tuple[Dict[str, Dict[str, float]], Dict[str, float]]:
        """Lazily builds and caches (command -> L2-normalized tf-idf vector,
        word -> idf) from every dispatchable command's own Phrasing texts.
        See _build_tfidf_index() for how; this just adds the cache."""
        if self._tfidf_cache is None:
            self._tfidf_cache = self._build_tfidf_index()
        return self._tfidf_cache

    def _build_tfidf_index(self) -> Tuple[Dict[str, Dict[str, float]], Dict[str, float]]:
        """Treats each dispatchable command's set of Phrasing texts as one
        "document" and builds a standard tf-idf vector for it -- term
        frequency (how often a word recurs across that command's own
        phrasings) times inverse document frequency (log((1+N)/(1+df)) + 1,
        the smoothed form scikit-learn's TfidfVectorizer uses, so a word
        shared by every command doesn't get a zero/negative weight).
        Vectors are L2-normalized so a later dot-product with a normalized
        query vector IS the cosine similarity directly, no extra division
        needed at query time.

        This is scored from PHRASING TEXT, not the flattened HAS_INTENT
        word-set the old specificity scorer used -- that distinction
        matters: it lets a command that repeats a word across several of
        its own phrasings (e.g. "find" appearing in 2 of FIND_FILES'
        phrasings) weight that word higher for itself specifically, not
        just record binary presence/absence.

        Rebuilt once per GraphRouter instance and cached -- see
        _get_tfidf_index(). Cheap (~340 dispatchable commands, ~500
        phrasings, one query) but no reason to redo it every classify()
        call within the same process.
        """
        result = self.conn.execute("""
            MATCH (p:Phrasing)-[:RESOLVES_TO]->(c:Command)
            WHERE c.tier = 'A'
            RETURN c.id, p.text
        """)
        command_terms: Dict[str, Dict[str, int]] = {}
        while result.has_next():
            cid, text = result.get_next()
            name = self._to_intent_name(cid)
            bucket = command_terms.setdefault(name, {})
            for w in content_words(normalize(text)):
                bucket[w] = bucket.get(w, 0) + 1

        num_commands = len(command_terms)
        doc_freq: Dict[str, int] = {}
        for terms in command_terms.values():
            for w in terms:
                doc_freq[w] = doc_freq.get(w, 0) + 1
        idf = {
            w: math.log((1 + num_commands) / (1 + df)) + 1.0
            for w, df in doc_freq.items()
        }

        vectors: Dict[str, Dict[str, float]] = {}
        for name, terms in command_terms.items():
            vec = {w: count * idf.get(w, 0.0) for w, count in terms.items()}
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            vectors[name] = {w: v / norm for w, v in vec.items()}

        return vectors, idf

    def _best_command(self, matches: List[Tuple[str, str]], num_query_words: int):
        """The command whose phrasing vector has the highest cosine
        similarity to the query, replacing the old specificity-sum scorer
        (see CONFIDENCE_THRESHOLD's comment above for exactly why that
        formula broke on cases like "clear the screen" -> EMPTY_RECYCLE_BIN
        and "make a folder called test" -> FIND_FILES, and why cosine
        similarity over each command's real phrasing vector doesn't have
        the same failure mode).

        `matches` (word-membership pairs from _fetch_dispatchable_matches)
        is only used here to build the query's own bag-of-words -- the
        actual per-command vectors come from _get_tfidf_index(), not from
        `matches`, since cosine similarity needs each command's FULL
        vector (including words the query didn't mention) to measure the
        angle correctly, not just the dimensions that happened to overlap.

        confidence is the cosine similarity itself (0-1, both vectors
        non-negative tf-idf so it can't go negative) -- directly
        comparable to CONFIDENCE_THRESHOLD, no extra rescaling.
        """
        query_words = {word for _, word in matches}
        if not query_words:
            return None, 0.0

        vectors, idf = self._get_tfidf_index()
        query_vec = {w: idf.get(w, 0.0) for w in query_words}
        query_norm = math.sqrt(sum(v * v for v in query_vec.values()))
        if query_norm <= 0:
            return None, 0.0

        def score(cmd_vec: Dict[str, float]) -> float:
            dot = sum(query_vec[w] * cmd_vec.get(w, 0.0) for w in query_vec)
            return dot / query_norm  # cmd_vec is already L2-normalized

        candidates = {name: vectors[name] for name in {n for n, _ in matches} if name in vectors}
        if not candidates:
            return None, 0.0

        scored = {name: score(vec) for name, vec in candidates.items()}
        top_score = max(scored.values())
        tied = [name for name, s in scored.items() if s == top_score]
        best_command = tied[0] if len(tied) == 1 else self._break_tie(tied)
        return best_command, round(top_score, 3)

    # Genuine ties between commands that don't otherwise conflict in
    # vocabulary (e.g. "weather today" -- "weather" cleanly means
    # GET_WEATHER, "today" cleanly means GET_DATE, so the SCORE ties even
    # though neither word is ambiguous on its own). Not a vocabulary fix
    # like the GET_WEATHER/GET_FORECAST overlap was -- there's no data
    # error to correct here, just a real coin-flip that needs an explicit
    # default. Listed pairs are unordered; the first listed command wins
    # when both appear in a tie. Extend this table as new tie cases turn
    # up during testing -- it's meant to be small and explicit, not
    # exhaustive or auto-derived, so every entry is a deliberate call you
    # made, not an inferred guess.
    _TIE_PREFERENCE: List[Tuple[str, str]] = [
        ("GET_WEATHER", "GET_DATE"),
        ("GET_WEATHER", "GET_TIME"),
    ]

    def _break_tie(self, tied: List[str]) -> str:
        """Only called when 2+ commands score EXACTLY equal. Falls back to
        tied[0] (dict/insertion order, same as the old unweighted
        behavior) if the tied set isn't in _TIE_PREFERENCE -- this method
        narrows an existing arbitrary choice for known cases, it doesn't
        promise to resolve every possible tie."""
        tied_set = set(tied)
        for preferred, other in self._TIE_PREFERENCE:
            if {preferred, other} <= tied_set:
                return preferred
        return tied[0]

    def close(self):
        self.conn.close()
        self.db.close()
