"""
wcl_resolver.py — tiered resolver for the windows_command_library graph.

This REPLACES graph_router.py's flat word-overlap scoring for everything
windows_command_library-sourced (previously tiers A2/B in toki_graph_db).
TOKI's own 59 original intents stay exactly where they were, matched by
GraphRouter against toki_graph_db (now rebuilt WITHOUT the wcl data mixed
in -- see migrate_to_kuzu.py) -- two separate, purpose-built graphs instead
of one graph trying to do both jobs.

Resolution policy (no confidence scores -- strict tiers, adapted verbatim
from the standalone build's 08_resolver.py):
  1. Exact alias match (single)       -> RESOLVED, no model
  2. Exact alias match (multiple)     -> AMBIGUOUS
  3. SynonymOf 1-hop match (single)   -> RESOLVED, no model
  4. SynonymOf 1-hop match (multiple) -> AMBIGUOUS
  5. Fuzzy alias match (difflib)      -> RESOLVED/AMBIGUOUS, still no model
  6. Abbreviation/full-form variant retry (BETA 0.3.29) -> re-runs tiers
     1-5 against known short/long noun substitutions (see
     ABBREVIATION_PAIRS below), only after tiers 1-5 miss on the query
     exactly as given
  7. Verb...noun bracket match (BETA 0.3.30) -> handles phrasings where
     the verb and the object noun bookend the value ("stop the print
     spooler service", "format the usb drive") -- see _bracket_resolve()
     below. Tried once, after tiers 1-5 miss, standalone (NOT composed
     with tier 6's abbreviation variants -- see _bracket_resolve()
     docstring for why that composition is deliberately not done yet).
  8. Leading noun+verb swap retry (BETA 0.3.33) -> handles phrasings
     where a 2-word verb+noun prefix (already resolvable via tiers 1-2,
     e.g. "lock bitlocker") appears with the noun and verb SWAPPED
     ("bitlocker lock ..."). Reuses tiers 1-2 verbatim against the
     swapped-prefix variant rather than adding new matching logic -- see
     _leading_pair_swap() below for the exact scoping.
  9. Nothing matches                  -> UNRESOLVED (loose_candidates only,
                                          never auto-dispatched)

orchestrator.py only ever auto-dispatches a RESOLVED result whose syntax
has zero remaining {variable} placeholders (see WCL_COMMANDS in
orchestrator.py) -- a RESOLVED command that still needs slot-filling, same
as AMBIGUOUS and UNRESOLVED, falls through to the LLM's normal
CHAT/GENERATE/ASK_CONTEXT classification untouched. Generic slot-filling
for variable-having commands is still the open follow-up milestone -- this
swap only replaces the MATCHING quality, not the dispatchability boundary.
"""
import re
import difflib
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import kuzu

DB_PATH = Path(__file__).parent / "wcl_kg" / "windows_commands_db"

# BETA 0.3.64 (this session): needed for _leading_word_related() below --
# wcl_kg/vocab.py already maintains a real, curated verb-synonym mapping
# (VERB_CLUSTERS/find_leading_cluster) built for the alias-generation
# pipeline itself, which is a much more reliable signal for "are these
# two leading words actually the same idea" than raw character-overlap
# ever can be for short words -- see that function's use below for the
# concrete real bug (mute/update coincidentally scoring 0.6 on
# SequenceMatcher despite being unrelated) that motivated reusing it here
# instead of tuning yet another standalone threshold. Import is
# best-effort and __file__-relative, same pattern as DB_PATH above, so a
# missing/moved wcl_kg degrades to the plain ratio fallback rather than
# crashing the whole resolver.
try:
    _wcl_kg_dir = str(Path(__file__).parent / "wcl_kg")
    if _wcl_kg_dir not in sys.path:
        sys.path.insert(0, _wcl_kg_dir)
    from vocab import find_leading_cluster as _find_leading_cluster
except Exception:
    _find_leading_cluster = None

FILLER_PREFIXES = [
    "please ", "could you ", "can you ", "would you ", "i want to ",
    "i need to ", "how do i ", "how do you ", "how to ",
]

# BETA 0.3.29: abbreviation/full-form pairs, for Tier 6 below.
#
# Each entry is (short_form, long_form). Both directions are tried --
# a query using either form gets a variant substituting the other, which
# is then run back through the SAME tiers 1-5 above (exact/synonym/strip/
# fuzzy), not matched directly. This is deliberately a fixed, hand-
# reviewed list, not a thesaurus/dictionary-API lookup: an external
# synonym source could quietly wire together words that are NOT actually
# interchangeable for command routing (e.g. "reset" and "refresh" are
# synonyms in English but very different actions here) -- see this
# project's own Tier 1 "silent wrong action" bug class. Every pair below
# was chosen because BOTH forms are real, plausible user phrasing for the
# SAME underlying noun, confirmed against actual alias-vocabulary
# frequency in wcl_kg/windows_commands_db (not guessed):
#   net(1671)/network(1118)   -- both heavily used; this is the exact
#                                 gap that caused the Restart-NetAdapter
#                                 "network adapter" bug fixed this session
#   vm(2391)/virtual machine(0) -- VM commands are 100% short-form; the
#                                 long form has zero coverage
#   config(2)/configuration(116), info(77)/information(2),
#   vol(2)/volume(163), auth(106)/authentication(0),
#   temp(10)/temporary(0)     -- all real, lopsided-but-real imbalances
#
# Deliberately NOT included: acronyms that are the ONLY form ever used in
# this dataset with no real long-form alternative in the wild for these
# commands (dns, dhcp, ip, smb, qos, acl, ps) -- expanding those would add
# noise, not coverage, since nobody phrases a request with the expansion.
# Also not included: abbreviation pairs where one side had zero hits on
# BOTH sides of a real gap check (svc/service, proc/process, app/
# application, sys/system, admin/administrator, etc.) -- these are
# speculative rather than confirmed, and belong in the dataset-wide audit
# (still open on priority.md) once there's real query evidence for them,
# not added here on a guess.
ABBREVIATION_PAIRS = [
    ("net", "network"),
    ("vm", "virtual machine"),
    ("config", "configuration"),
    ("info", "information"),
    ("vol", "volume"),
    ("auth", "authentication"),
    ("temp", "temporary"),
]


def _abbreviation_variants(q: str) -> List[str]:
    """Given an already-normalize()'d query, returns alternate forms with
    each known short/long pair swapped for its counterpart -- e.g. "reset
    net adapter" -> ["reset network adapter"]. Whole-word substitution
    only (word-boundary regex), so "net" never matches inside "internet"
    or "planet". Returns [] if no pair applies; never includes the
    original string itself (caller already tried that)."""
    variants = []
    tokens = q.split()
    tokenset = set(tokens)
    for short, long in ABBREVIATION_PAIRS:
        long_tokens = long.split()
        if short in tokenset:
            variants.append(re.sub(rf"\b{re.escape(short)}\b", long, q))
        # only attempt the reverse (long -> short) when the long form is
        # itself multi-word if its first token is present as a whole
        # phrase match, to avoid partial substitution inside unrelated
        # multi-word phrases
        if long_tokens[0] in tokenset and long in q:
            variants.append(q.replace(long, short))
    # de-dupe while preserving order, and never return the input unchanged
    seen = set()
    out = []
    for v in variants:
        if v != q and v not in seen:
            seen.add(v)
            out.append(v)
    return out


# BETA 0.3.64 (this session, real-data stress test 2026-08-22): shared
# stopword list for Tier 9 (_content_words / _containment_resolve below)
# and the bracket-resolve safety check in _bracket_resolve(). Small and
# fixed on purpose -- same posture as ABBREVIATION_PAIRS above -- this
# only strips words that are near-never load-bearing for picking a
# command (articles/prepositions), never anything that could itself be
# a real object noun.
STOPWORDS = frozenset({
    "the", "a", "an", "of", "for", "on", "in", "my", "this", "that",
    "to", "is", "are", "with", "and", "or", "at", "by", "me",
})

# BETA 0.3.64 (this session): confirmed directly against real alias rows
# (e.g. every one of Get-Volume's aliases is "<verb> me the volume" for
# verb in this exact set) that this dataset uses these words purely as
# interchangeable "retrieve"-synonym padding, not as real distinguishing
# content -- see the ambiguity_groups TEMPLATE_GENERATED_ALIASES gap the
# rebuilt KB already flagged for the same underlying reason. The problem:
# several of these words (get, list, show, print, status) are ALSO real,
# meaningful nouns/objects elsewhere in this same command surface ("print
# spooler service", "get" as a verb prefix, a task's "status"), so a
# containment match can't tell "print" the padding-verb from "print" the
# real word without this list -- confirmed via a real false match this
# session: "stop the print spooler service" collided with Get-Service's
# padding-verb alias "print me the service" purely because both strings
# contain the word "print". Excluded ONLY from the ALIAS side's word
# count in Tier 9 (_containment_resolve), never from the query side and
# never from any other tier's literal alias-text matching -- this is a
# heuristic to stop over-general padding-verb aliases from qualifying as
# tier 9's required "2 real distinguishing words", not a claim that
# these words never mean anything.
ALIAS_PADDING_VERBS = frozenset({
    "display", "list", "show", "output", "print", "view", "read",
    "get", "status", "manage", "what", "check",
})

# BETA 0.3.64 (this session): confirmed a real, dangerous failure mode in
# Tier 5's fuzzy match -- difflib's SequenceMatcher only measures
# character-sequence overlap, with zero concept of what a word MEANS, so
# a harmless read query can land on a destructive command purely by
# coincidental spelling. Confirmed live: "list all vm switches" scored
# 0.821 similarity against the real alias "uninstall vm switch" (both
# strings happen to share a lot of letters -- "list all" vs "uninstall"
# overlap more than they look like they should), which cleared the 0.82
# cutoff and returned Remove-VMSwitch for a query that was just asking
# to LIST switches. These two small sets are used ONLY to veto a tier 5
# candidate whose own leading word crosses from one of these categories
# to the other relative to the query's leading word -- never to accept a
# match, so a false negative here only costs a fallthrough to a weaker
# tier or UNRESOLVED, never a wrong RESOLVED.
READ_VERBS = frozenset({"list", "show", "get", "display", "view", "what"})
DESTRUCTIVE_VERBS = frozenset({
    "remove", "delete", "uninstall", "clear", "disable", "stop", "wipe",
    "format", "kill", "clean", "reset", "revoke", "unregister",
})


def _leading_word_related(word_a: str, word_b: str) -> bool:
    """True if word_a/word_b are plausibly the SAME intended word --
    either literally, a same-cluster real synonym (via wcl_kg/vocab.py's
    curated VERB_CLUSTERS, when importable), or a close-enough typo.

    BETA 0.3.64 (this session). Cluster lookup is the PRIMARY signal
    when available for either word, specifically because raw
    character-overlap ratio is fooled by short words that coincidentally
    share letters despite meaning nothing alike -- confirmed real bug:
    "mute" vs "update" scores 0.6 on SequenceMatcher (both contain
    "u","t","e" in order) despite being unrelated, which let "mute the
    volume" (audio) slip through a plain ratio>=0.5 gate and still land
    on Set-Volume (a destructive DISK command) via its "update the
    volume" synonym alias, even after the exact "set"/"mute" pair
    (ratio 0.286) was correctly rejected by that same gate.

    If EITHER word has a recognized cluster, trust that over the ratio:
    same word or same cluster -> related; a recognized cluster that
    DIFFERS from the other word's (cluster or lack of one) -> not
    related, full stop, no ratio fallback -- the vocab already gave a
    real answer, a coincidental string-shape rhyme doesn't get a second
    vote.

    Only when NEITHER word is in any known cluster (something outside
    this domain's curated verb vocabulary entirely) does this fall back
    to a ratio check -- and a stricter one (0.75, not 0.5) than the
    first version of this fix used, precisely because that's exactly the
    situation with no semantic signal to lean on, so it needs to lean
    harder on the words looking almost identical, not just similar.
    """
    if word_a == word_b:
        return True
    if _find_leading_cluster is not None:
        cluster_a = _find_leading_cluster(word_a)
        cluster_b = _find_leading_cluster(word_b)
        if cluster_a is not None or cluster_b is not None:
            if cluster_a is not None and cluster_b is not None:
                return word_b in cluster_a[1] or word_a in cluster_b[1]
            return False
    return difflib.SequenceMatcher(None, word_a, word_b).ratio() >= 0.75


def _content_words(tokens: List[str]) -> List[str]:
    """Tokens with stopwords removed -- used by Tier 9's containment
    match and the bracket-resolve safety check, both of which need "the
    real nouns/verbs in this phrase", not every token verbatim.

    Deliberately NOT length-filtered (an earlier version of this
    function also dropped tokens of length <= 2, matching loose_search's
    filter -- found and reverted the same session it was written: this
    domain's vocabulary is full of short, load-bearing words -- "vm",
    "ip", "os", "pc", "db" -- and stripping them by length alone silently
    deleted the ONE distinguishing word out of alias phrases like "make a
    new vm", leaving only generic filler like {"make","new"} behind,
    which then falsely subset-matched unrelated queries like "make a new
    FOLDER called test". Stopwords are removed by an explicit fixed list
    instead, specifically because that risk doesn't apply -- nothing on
    that list is ever itself a real command object.)"""
    return [w for w in tokens if w not in STOPWORDS]


def _leading_pair_swap(q: str) -> Optional[str]:
    """Given an already-normalize()'d query, swaps ONLY the first two
    tokens (e.g. "bitlocker lock mount point d" -> "lock bitlocker mount
    point d") -- used by Tier 8 to catch a noun-before-verb phrasing by
    re-running the EXISTING verb-first tiers against the swapped form,
    rather than teaching any tier to understand noun-first order
    directly. Returns None if there aren't at least 3 tokens (2 to swap
    + a genuine remaining value) -- same "must have a real middle"
    requirement _bracket_resolve() (tier 7) uses."""
    tokens = q.split()
    if len(tokens) < 3:
        return None
    return " ".join([tokens[1], tokens[0]] + tokens[2:])


def normalize(q: str) -> str:
    q = q.lower().strip()
    # BETA 0.3.64 (this session): hyphens/underscores MUST become a
    # space, not just vanish, before the general punctuation strip below.
    # Found via stress-testing real PowerShell-shaped input: typing an
    # actual cmdlet name verbatim -- "Get-Volume", "get-service" -- or
    # any hyphenated compound word with no surrounding whitespace
    # ("print-spooler service") used to fuse the two halves into one
    # glued token ("getvolume", "printspooler") once the old
    # `re.sub(r"[^\w\s]", "", q)` deleted the hyphen outright. That
    # merged token then never matches any real (space-separated) alias
    # text at all, and worse, tier 5's difflib fuzzy match doesn't fail
    # loud -- it just returns whatever a mangled token happens to be
    # closest to, e.g. "getvolume" landed on Set-Volume, "get-service"
    # landed on Set-Service. Confirmed via this session's stress test
    # that this only bit single/two-token queries hard (typing a bare
    # cmdlet name); longer sentences ("stop the print-spooler service")
    # mostly survived by luck, since Tier 7's bracket match only looks
    # at the first/last token and discards the mangled middle anyway --
    # not something to rely on.
    q = re.sub(r"[-_]", " ", q)
    q = re.sub(r"[^\w\s]", "", q)
    q = re.sub(r"\s+", " ", q)
    changed = True
    while changed:
        changed = False
        for prefix in FILLER_PREFIXES:
            if q.startswith(prefix):
                q = q[len(prefix):]
                changed = True
    return q.strip()


def _strip_filler_prefixes_preserving_original(text: str) -> str:
    """Same repeated-prefix-stripping loop as normalize(), but case-
    insensitive matching against the ORIGINAL string with punctuation and
    case fully preserved -- so token positions here line up with what a
    human actually typed, unlike normalize()'s output which has already
    lowercased and stripped punctuation. Needed by Tier 2 to return a real
    slot value ("notes.txt") instead of normalize()'s mangled token
    ("notestxt", punctuation stripped)."""
    changed = True
    while changed:
        changed = False
        lowered = text.lower()
        for prefix in FILLER_PREFIXES:
            if lowered.startswith(prefix):
                text = text[len(prefix):]
                changed = True
                break
    return text.strip()


class WCLResolver:
    """Fails open exactly like GraphRouter: if wcl_kg/windows_commands_db
    doesn't exist or kuzu isn't installed, self.conn is None and resolve()
    always returns UNRESOLVED, so orchestrator.py's fallback to the LLM
    path is unaffected -- see graph_router.py's module docstring for the
    same pattern applied there."""

    def __init__(self, db_path: Path = DB_PATH):
        self.conn = None
        self._all_aliases: Optional[List[str]] = None
        if not db_path.exists():
            return
        try:
            db = kuzu.Database(str(db_path), read_only=True)
            self.conn = kuzu.Connection(db)
        except Exception:
            self.conn = None

    def close(self):
        self.conn = None

    def all_aliases(self) -> List[str]:
        if self._all_aliases is None:
            res = self.conn.execute("MATCH (a:Alias) RETURN a.text")
            self._all_aliases = []
            while res.has_next():
                self._all_aliases.append(res.get_next()[0])
        return self._all_aliases

    def resolve(self, query: str) -> Dict[str, Any]:
        if self.conn is None:
            return {"status": "UNRESOLVED", "tier": None, "loose_candidates": []}

        q = normalize(query)
        result = self._resolve_normalized(q, original_query=query)
        if result["status"] != "UNRESOLVED":
            return result

        # Tier 6: abbreviation/full-form variant retry (BETA 0.3.29).
        #
        # Only tried after tiers 1-5 have already failed on the query
        # exactly as given -- this is a fallback, not a replacement, so a
        # query that already resolves normally is completely unaffected
        # by anything below. For each known short/long noun pair (see
        # ABBREVIATION_PAIRS above) that appears in the query, build the
        # swapped variant and run it back through the SAME tiers 1-5,
        # not a separate/looser matcher -- "reset network adapter" only
        # resolves here because "reset net adapter" would have resolved
        # via ordinary Tier 1 exact match; this tier just supplies the
        # missing wording, it doesn't loosen what counts as a match.
        #
        # If more than one variant would resolve to DIFFERENT commands,
        # that's treated as ambiguous rather than silently picking one --
        # same fail-safe posture as every other tier here.
        variant_results = []
        for variant in _abbreviation_variants(q):
            variant_result = self._resolve_normalized(variant)
            if variant_result["status"] in ("RESOLVED", "AMBIGUOUS"):
                variant_results.append(variant_result)

        if len(variant_results) == 1:
            out = dict(variant_results[0])
            out["tier"] = 6
            return out
        if len(variant_results) > 1:
            # Merge candidates from every variant that produced a hit;
            # if they all agree on one command, that's still a single
            # coherent RESOLVED/AMBIGUOUS answer, just collected from more
            # than one variant -- only genuinely different commands make
            # this stay ambiguous.
            merged = []
            full_by_command = {}
            for vr in variant_results:
                if vr["status"] == "RESOLVED":
                    merged.append((vr["command"], vr["syntax"], vr["danger_level"]))
                    full_by_command[vr["command"]] = vr
                else:
                    merged.extend(vr["candidates"])
            distinct = list({c for c in merged})
            if len(distinct) == 1:
                name = distinct[0][0]
                if name in full_by_command:
                    out = dict(full_by_command[name])
                    out["tier"] = 6
                    return out
                # Only reached via an AMBIGUOUS variant collapsing to one
                # candidate across variants -- no requires_admin/
                # requires_confirmation/category on hand for a bare
                # 3-tuple candidate, so report what we do know rather
                # than fabricate the rest.
                return {
                    "status": "RESOLVED", "tier": 6,
                    "command": name, "syntax": distinct[0][1],
                    "danger_level": distinct[0][2],
                }
            return {"status": "AMBIGUOUS", "tier": 6, "candidates": distinct}

        # Tier 8: leading noun+verb swap retry (BETA 0.3.33).
        #
        # Confirmed live gap: "lock bitlocker mount point D" resolves
        # cleanly via tier 2 (verb-first: "lock bitlocker" is a real
        # alias, "mount point D" strips as the trailing value) -- but
        # "bitlocker lock mount point D" (noun BEFORE verb) stayed
        # UNRESOLVED, because tiers 1-7 all expect the verb to lead.
        # Real users overwhelmingly say it verb-first ("lock bitlocker"),
        # but this exact noun-first phrasing was the original repro this
        # session was asked to re-check, and BitLocker operations are
        # destructive -- worth the narrow, well-contained fix rather than
        # leaving it silently misrouted to whatever Tier A guesses.
        #
        # Deliberately narrow, same posture as tier 7:
        #   - swaps ONLY tokens[0] and tokens[1] -- not an arbitrary
        #     reordering search. If the 2-word swap doesn't produce a
        #     hit, this tier is a no-op; it never tries a 3rd, 4th, ...
        #     token as the "real" verb.
        #   - reuses tiers 1-2's EXACT alias/prefix matching verbatim by
        #     just re-running _resolve_normalized() on the swapped
        #     string -- no new fuzzy/loose matching logic added here at
        #     all, which is also why this doesn't need its own AMBIGUOUS-
        #     collection code: whatever tiers 1-5 would have returned for
        #     the swapped query (RESOLVED or AMBIGUOUS) is returned as-is,
        #     just re-tagged tier 8.
        #   - requires at least 3 tokens (2 to swap + a genuine remaining
        #     value) -- same "must have a real middle" requirement as
        #     tier 7.
        #   - tried AFTER tier 6 (abbreviation retry), not combined with
        #     it or tier 7 -- same "don't compose two forms of widening
        #     in one guess" posture as tier 7's own docstring.
        swapped = _leading_pair_swap(q)
        if swapped is not None:
            swapped_result = self._resolve_normalized(swapped)
            if swapped_result["status"] in ("RESOLVED", "AMBIGUOUS"):
                out = dict(swapped_result)
                out["tier"] = 8
                return out

        # Tier 9: alias-token-containment match (BETA 0.3.64, this
        # session -- moved here from inside _resolve_normalized after
        # finding it was stealing the turn from tiers 6/8 below: those
        # are only tried when the base pass is UNRESOLVED, and tier 9's
        # weaker containment guess was turning some queries AMBIGUOUS
        # before tier 8's much stronger, exact-match-based swap retry
        # ever got a chance -- e.g. "bitlocker lock mount point D" used
        # to get a fully-confident tier 8 RESOLVED, but with tier 9
        # running earlier it got intercepted into a same-answer-but-
        # lower-confidence AMBIGUOUS instead, for no benefit. Now it's
        # truly last: every stronger tier (1/3/2/5/7 directly, 6/8 via
        # retries) has already had first crack, on the original query
        # AND every abbreviation/swap variant. Only consulted when ALL
        # of that still comes back UNRESOLVED. See _containment_resolve()
        # docstring for what this catches and why it can only ever
        # return AMBIGUOUS, never a bare RESOLVED.
        containment = self._containment_resolve(q, q.split())
        if containment is not None:
            return containment

        return result  # original UNRESOLVED, with its own loose_candidates

    def _normalized_alias_index(self) -> Dict[str, List[tuple]]:
        """Cached normalize(alias_text) -> [(alias_text, command_name,
        syntax, danger_level, requires_admin, requires_confirmation,
        category), ...], built once from _all_alias_rows().

        Exists specifically for Tier 1's punctuation fallback below --
        found via a full sweep of all 13,780 real aliases this session:
        normalize() strips ALL punctuation from the QUERY side (re.sub
        r"[^\\w\\s]"), but a small, real family of stored alias text
        (confirmed: 68 aliases across 22 "mstsc /flag" commands --
        "mstsc /admin", "mstsc /console", etc.) still has literal
        punctuation in it. Tier 1's exact-match graph query compares the
        normalized query against that raw, un-normalized text, so those
        68 aliases could never match ANY phrasing at all, and the query
        was instead falling through to a shorter, wrong prefix match
        ("mstsc" alone) via tier 2. Skips any alias whose OWN normalized
        form is empty or under 2 chars -- specifically to exclude
        symbol-only aliases like "%" or "?" (PowerShell's ForEach-Object/
        Where-Object shorthands), which normalize() reduces to "" and
        which would otherwise all collide on that one empty key. Those
        two remain a known, separate, unrecoverable gap (normalize()
        would have to stop stripping punctuation entirely to fix them,
        which risks every other tier that relies on it) -- not silently
        papered over by this index.
        """
        if not hasattr(self, "_norm_alias_idx_cache") or self._norm_alias_idx_cache is None:
            idx: Dict[str, List[tuple]] = {}
            for row in self._all_alias_rows():
                alias_text = row[0]
                key = normalize(alias_text)
                if len(key) < 2:
                    continue
                idx.setdefault(key, []).append(row)
            self._norm_alias_idx_cache = idx
        return self._norm_alias_idx_cache

    def _resolve_normalized(self, q: str, original_query: Optional[str] = None) -> Dict[str, Any]:
        """Tiers 1-5, operating on an already-normalize()'d query string.
        Split out from resolve() so Tier 6 (abbreviation variants, see
        resolve()) can re-run this exact same matching logic against a
        substituted query without duplicating it or loosening any
        individual tier's own matching rules.

        original_query: the true raw user text (real casing/punctuation
        preserved), used only by Tier 2 to build a stripped_value that
        looks like what the user actually typed. Tier 6 calls this with
        an abbreviation-substituted q that has no corresponding "real"
        original text -- for that case original_query is left as None
        and Tier 2 falls back to q itself (already-normalized, so a
        stripped_value from an abbreviation-variant match loses original
        casing/punctuation, same as the pre-existing defensive fallback
        below already handles for a token-count mismatch)."""
        if original_query is None:
            original_query = q

        # Tier 1: exact literal alias match
        res = self.conn.execute(
            "MATCH (a:Alias {text: $q})<-[:HasAlias]-(c:Command) "
            "RETURN c.name, c.syntax, c.danger_level, c.requires_admin, c.requires_confirmation, c.category",
            {"q": q},
        )
        rows = []
        while res.has_next():
            rows.append(res.get_next())
        if len(rows) == 1:
            return self._resolved(1, rows[0])
        if len(rows) > 1:
            # BETA 0.3.27 fix: candidates used to drop danger_level (only
            # (name, syntax) survived), even though the underlying row
            # already carries it -- that silently blinded orchestrator.py's
            # destructive-shadow guard (priority.md #11) to any AMBIGUOUS
            # result, e.g. "clean temp files" resolves AMBIGUOUS here with
            # a genuinely destructive Clear-TempFiles candidate sitting
            # right in the list, but the guard only ever checked RESOLVED
            # results, so it never got a chance to fire. Now (name, syntax,
            # danger_level) so the guard can see it.
            return {"status": "AMBIGUOUS", "tier": 1, "candidates": [(r[0], r[1], r[2]) for r in rows]}

        # Tier 1b: punctuation-normalized fallback (BETA 0.3.64, this
        # session). Only reached when the raw exact-match query above
        # found NOTHING -- existing behavior for every alias without
        # stray punctuation is completely unchanged, since this never
        # runs otherwise. See _normalized_alias_index() docstring for
        # exactly what this catches ("mstsc /admin" and its 21 sibling
        # commands) and why it deliberately can't catch everything.
        norm_rows = self._normalized_alias_index().get(q)
        if norm_rows:
            distinct = list({r[1] for r in norm_rows})
            if len(distinct) == 1:
                return self._resolved(1, norm_rows[0][1:])
            picked = {}
            for r in norm_rows:
                picked[r[1]] = r[1:]
            return {
                "status": "AMBIGUOUS", "tier": 1,
                "candidates": [(r[0], r[1], r[2]) for r in picked.values()],
            }

        # Tier 3: SynonymOf 1-hop from any recognized leading token
        tokens = q.split()
        leading_candidates = []
        if len(tokens) >= 2:
            leading_candidates.append(" ".join(tokens[:2]))
        if tokens:
            leading_candidates.append(tokens[0])
        for phrase in leading_candidates:
            res = self.conn.execute(
                """
                MATCH (start:Intent {name: $phrase})-[:SynonymOf*0..1]->(syn:Intent)
                      <-[:HasIntent]-(c:Command)-[:HasAlias]->(a:Alias)
                WHERE a.text CONTAINS $rest
                RETURN DISTINCT c.name, c.syntax, c.danger_level, c.requires_admin, c.requires_confirmation, c.category
                """,
                {"phrase": phrase, "rest": q.replace(phrase, "").strip() or q},
            )
            rows = []
            while res.has_next():
                rows.append(res.get_next())
            if len(rows) == 1:
                return self._resolved(3, rows[0])
            if len(rows) > 1:
                return {"status": "AMBIGUOUS", "tier": 3, "candidates": [(r[0], r[1], r[2]) for r in rows]}

        # Tier 2: trailing-value stripping. A real single-variable command
        # phrase in the wild looks like "<real alias> <the user's actual
        # value>" -- e.g. "cat notes.txt", where "cat" alone is a real,
        # exact alias but the whole string never matches tier 1 because
        # of the trailing filename. Try progressively shorter PREFIXES
        # (never suffixes -- these commands are phrased verb/alias-first,
        # value-last, confirmed against this project's own real alias
        # data) until one matches an alias exactly and unambiguously.
        # Stops at the FIRST match (shortest strip = most conservative),
        # and only fires when something was actually stripped off (a
        # query that already matches in full is tier 1's job, not this
        # one's).
        if len(tokens) >= 2:
            # Original-text tokens (filler-stripped, but NOT punctuation-
            # stripped/lowercased) for building the actual returned value
            # -- token count/positions line up with `tokens` (normalize()'s
            # output) because normalize() only strips punctuation WITHIN
            # tokens, never merges/splits whole tokens, and both use the
            # same filler-prefix stripping. Caught and fixed before
            # shipping: using `tokens` itself here returned "notestxt"
            # instead of "notes.txt" -- the period had already been
            # stripped by normalize().
            original_tokens = _strip_filler_prefixes_preserving_original(original_query).split()
            for cut in range(len(tokens) - 1, 0, -1):
                prefix = " ".join(tokens[:cut])
                res = self.conn.execute(
                    "MATCH (a:Alias {text: $q})<-[:HasAlias]-(c:Command) "
                    "RETURN c.name, c.syntax, c.danger_level, c.requires_admin, c.requires_confirmation, c.category",
                    {"q": prefix},
                )
                rows = []
                while res.has_next():
                    rows.append(res.get_next())
                if len(rows) == 1:
                    out = self._resolved(2, rows[0])
                    out["stripped_value"] = (
                        " ".join(original_tokens[cut:])
                        if len(original_tokens) == len(tokens)
                        else " ".join(tokens[cut:])  # defensive fallback, see below
                    )
                    return out
                if len(rows) > 1:
                    # Ambiguous even after stripping -- don't keep trying
                    # shorter prefixes of an already-ambiguous match, that
                    # would only get MORE ambiguous, not less.
                    return {"status": "AMBIGUOUS", "tier": 2, "candidates": [(r[0], r[1], r[2]) for r in rows]}

        # Tier 5: fuzzy near-miss against real alias text
        close = difflib.get_close_matches(q, self.all_aliases(), n=3, cutoff=0.82)
        if close:
            # BETA 0.3.64 (this session): content-completeness pre-pass,
            # ahead of the category/leading-word vetoes below. Real bug
            # this fixes: "check windows defender status" had TWO real
            # candidates clear the 0.82 cutoff -- the correct one, "show
            # windows defender status" (0.877, contains the query's one
            # actually-distinctive word "defender"), and a wrong one,
            # "check windows update status" (0.821, MISSING "defender"
            # entirely, substituting "update"). The leading-word veto
            # alone made this worse, not better: it trivially keeps
            # "check windows update status" (literally same leading word
            # as the query) while correctly-but-unhelpfully rejecting
            # "show windows defender status" over a real but, in this
            # exact context, beside-the-point verb-cluster mismatch
            # ("check" clusters with verify/diagnose, "show" with
            # display/get in wcl_kg/vocab.py -- a fair distinction in
            # general, just not the thing that actually separates these
            # two candidates). A candidate that's MISSING one of the
            # query's own real content words, when another candidate
            # has it exactly, is worse evidence than a borderline verb
            # mismatch -- so this counts real (padding-stripped) missing
            # words per candidate and keeps only the candidates tied for
            # fewest.
            q_content = set(_content_words(tokens)) - ALIAS_PADDING_VERBS
            def _symmetric_diff(alias_text: str) -> int:
                # BETA 0.3.64 (this session): symmetric, not one-
                # directional. An earlier version of this only counted
                # words the QUERY was missing from the alias, which
                # correctly preferred "show windows defender status"
                # over "check windows update status" for the Defender
                # case above -- but it never penalized an alias for
                # introducing EXTRA concepts the query never asked for.
                # Real case found testing this same session: "print the
                # network adapter list" scored a "perfect" (0 missing)
                # one-directional match against "print me the vm network
                # adapter acl" purely because it contains every word the
                # query has -- plus two extra, meaningfully different
                # concepts ("vm", "acl") the query never mentioned at
                # all, silently narrowing a general adapter-listing
                # request down to a VM-scoped ACL command. Counting the
                # full symmetric difference in both directions still
                # prefers an exact/near-exact match (0 either way) but no
                # longer rewards a candidate just for being a superset --
                # it now correctly prefers a same-family candidate with
                # fewer bolted-on extra words when the truly matching one
                # (a plain, non-VM-scoped adapter alias) isn't in the
                # fuzzy candidate pool at all.
                a_content = set(_content_words(alias_text.split())) - ALIAS_PADDING_VERBS
                return len(q_content - a_content) + len(a_content - q_content)
            if q_content:
                scored = [(c, _symmetric_diff(c)) for c in close]
                best = min(n for _, n in scored)
                close = [c for c, n in scored if n == best]
            else:
                best = None

            # Safety veto: reject any candidate whose own leading word
            # crosses categories with the query's leading word (read <->
            # destructive). This check is ABSOLUTE -- unlike the
            # leading-word-relatedness check further below, a perfect
            # content-word match never overrides it, because a query
            # that's clearly asking to READ something landing on a
            # DESTRUCTIVE command is the one class of Tier 5 mistake this
            # codebase can't tolerate regardless of how well the rest of
            # the words line up. See READ_VERBS/DESTRUCTIVE_VERBS above
            # for the real bug this fixes ("list all vm switches" ->
            # Remove-VMSwitch, purely from character-overlap luck).
            q_lead = tokens[0] if tokens else ""
            def _category_safe(alias_text: str) -> bool:
                a_lead = alias_text.split()[0] if alias_text else ""
                if q_lead in READ_VERBS and a_lead in DESTRUCTIVE_VERBS:
                    return False
                if q_lead in DESTRUCTIVE_VERBS and a_lead in READ_VERBS:
                    return False
                return True
            close = [c for c in close if _category_safe(c)]

            # Leading-word-relatedness veto: general backstop alongside
            # the category veto above. Real bug found: "mute the volume"
            # (audio) scored 0.828 whole-string similarity against the
            # real alias "set the volume" (Set-Volume -- a DESTRUCTIVE
            # disk-partition command, confirmed via its own danger_level
            # field), high enough to clear the 0.82 cutoff purely because
            # both strings share the long suffix "the volume" while the
            # one word that actually carries the meaning ("mute" vs
            # "set") gets diluted into a small fraction of the overall
            # ratio. _leading_word_related() uses wcl_kg/vocab.py's
            # curated VERB_CLUSTERS as its primary signal (falls back to
            # a strict 0.75 ratio only when neither word is clustered) --
            # see that function's own docstring for why raw ratio alone
            # isn't trustworthy here (mute/update coincidentally scores
            # 0.6 despite being unrelated).
            #
            # UNLIKE the category veto above, this one is skipped for a
            # candidate that is BOTH the unique surviving candidate AND a
            # perfect (0 missing) content-word match -- see the Windows
            # Defender case in this block's own opening comment for
            # exactly why: a real content-word mismatch is stronger,
            # more specific evidence than a verb-cluster distinction that
            # doesn't hold up in this particular phrasing. Only applies
            # when q_content was non-empty and unique; with 0 or 2+ still
            # in the running, this veto still applies to every one of
            # them, same as before this session.
            if q_content and best == 0 and len(close) == 1:
                pass
            else:
                close = [c for c in close if q_lead == (c.split()[0] if c else "") or _leading_word_related(q_lead, c.split()[0] if c else "")]
        if close:
            rows = []
            for alias_text in close:
                res = self.conn.execute(
                    "MATCH (a:Alias {text: $t})<-[:HasAlias]-(c:Command) "
                    "RETURN c.name, c.syntax, c.danger_level, c.requires_admin, c.requires_confirmation, c.category",
                    {"t": alias_text},
                )
                while res.has_next():
                    rows.append(res.get_next())
            distinct = list({(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows})
            if len(distinct) == 1:
                # BETA 0.3.65 (this session): pre-emption check, confirmed
                # via a real live case -- "print the network adapter list"
                # was landing here as a confident single-candidate RESOLVED
                # (Get-VMNetworkAdapterAcl) even though a genuinely BETTER,
                # non-VM-scoped candidate ("list network adapters" ->
                # Get-NetAdapter, an exact content-word match with zero
                # extra concepts) exists in the graph. The problem was
                # never the scoring inside `close` -- it's that difflib's
                # 0.82 whole-string cutoff never let the better candidate
                # INTO the pool at all (its raw character-overlap ratio
                # against the query is ~0.75, just under cutoff, despite
                # being the semantically exact match), so the symmetric-
                # diff re-ranking above was only ever choosing among
                # options that were all, to varying degrees, wrong.
                #
                # Fix: before trusting a single `close`-pool survivor,
                # scan the FULL alias table (already cached via
                # _all_alias_rows(), same cost class as tier 9) for any
                # alias whose padding-stripped content words are an EXACT
                # match for the query's -- not merely fuzzy-close -- for a
                # DIFFERENT command. That candidate never had to clear the
                # 0.82 cutoff to prove itself: exact content-word equality
                # is strictly stronger evidence than whole-string character
                # overlap. If found, this tier can no longer claim a
                # confident single answer -- fall to AMBIGUOUS with both,
                # same fail-safe posture as every other veto in this tier.
                # Never used to manufacture a NEW RESOLVED on its own, only
                # to withhold a wrong one.
                winner_name = distinct[0][0]
                if q_content:
                    # Fold trivial plurals (word/word+"s") AND known
                    # short/long abbreviation pairs before the equality
                    # check.
                    #
                    # Plural fold found while verifying this fix:
                    # "network adapter" (query, singular) vs "network
                    # adapters" (real alias, plural) are the same concept
                    # but compared unequal as bare sets, which silently
                    # defeated the whole check for exactly the case it was
                    # written for. Only strips a bare trailing "s" on
                    # words longer than 3 chars (so "vm"/"os"/"ip"-class
                    # short words are never touched), never a general
                    # stemmer -- same narrow, hand-checked posture as
                    # every other heuristic in this file.
                    #
                    # Abbreviation fold added after a second live case:
                    # "net adjust adapter" -- fed through Tier 6's own
                    # net->network substitution, landing on "network
                    # adjust adapter" -- was STILL confidently resolving
                    # wrong (to Get-NetAdapter via the unrelated "network
                    # net adapter" alias) instead of the correct, genuinely
                    # destructive Set-NetAdapter ("adjust net adapter"),
                    # because the correct alias uses "net" while the
                    # substituted query now says "network" -- an exact-set
                    # check with no abbreviation-awareness treated those as
                    # different content and missed it. Reuses
                    # ABBREVIATION_PAIRS (the exact same hand-reviewed,
                    # frequency-confirmed list Tier 6 already trusts) to
                    # canonicalize each word to its short form before
                    # comparing -- deliberately the SAME list, not a new
                    # one, so this can't disagree with what Tier 6 already
                    # considers interchangeable.
                    _SHORT_FORM = {long: short for short, long in ABBREVIATION_PAIRS}
                    def _fold(words):
                        out = set()
                        for w in words:
                            w = _SHORT_FORM.get(w, w)
                            if len(w) > 3 and w.endswith("s"):
                                w = w[:-1]
                            out.add(w)
                        return frozenset(out)
                    q_content_folded = _fold(q_content)
                    exact_content_matches = {}
                    for alias_text2, name2, syntax2, danger2, admin2, confirm2, category2 in self._all_alias_rows():
                        if name2 == winner_name:
                            continue
                        a_content2 = set(_content_words(alias_text2.split())) - ALIAS_PADDING_VERBS
                        if a_content2 and _fold(a_content2) == q_content_folded:
                            exact_content_matches[name2] = (name2, syntax2, danger2, admin2, confirm2, category2)
                    if exact_content_matches:
                        all_candidates = {winner_name: distinct[0]}
                        all_candidates.update(exact_content_matches)
                        return {
                            "status": "AMBIGUOUS", "tier": 5,
                            "candidates": [(r[0], r[1], r[2]) for r in all_candidates.values()],
                        }
                out = self._resolved(5, distinct[0])
                out["matched_alias"] = close[0]
                return out
            if len(distinct) > 1:
                return {"status": "AMBIGUOUS", "tier": 5, "candidates": [(r[0], r[1], r[2]) for r in distinct]}

        # Tier 7: verb...noun bracket match (BETA 0.3.30). Only reached
        # after tiers 1/3/2/5 above have all missed.
        bracket = self._bracket_resolve(q, tokens, original_query)
        if bracket is not None:
            return bracket

        return {"status": "UNRESOLVED", "tier": None, "loose_candidates": self.loose_search(q)}

    def _all_alias_rows(self) -> List[tuple]:
        """Cached (alias_text, command_name, syntax, danger_level,
        requires_admin, requires_confirmation, category) rows for every
        Alias in the graph -- same one-time-load posture as
        all_aliases(), reused by Tier 9 so a run of unresolved queries
        doesn't re-issue this same full scan per query."""
        if not hasattr(self, "_alias_rows_cache") or self._alias_rows_cache is None:
            res = self.conn.execute(
                "MATCH (a:Alias)<-[:HasAlias]-(c:Command) RETURN a.text, c.name, "
                "c.syntax, c.danger_level, c.requires_admin, c.requires_confirmation, c.category"
            )
            rows = []
            while res.has_next():
                rows.append(tuple(res.get_next()))
            self._alias_rows_cache = rows
        return self._alias_rows_cache

    def _containment_resolve(self, q: str, tokens: List[str]) -> Optional[Dict[str, Any]]:
        """Tier 9: fires only when a real alias's own content words are
        ALL present among the query's content words (never the reverse) --
        catches a query that simply wraps a genuine, exact multi-word
        alias in extra filler ("list all volumes" fully contains the real
        alias "list volumes" plus one incidental extra word "all"; tier 1
        misses because the full strings differ, and tier 5's difflib
        ratio is computed over the WHOLE string, so one extra word can
        drag a genuine match below the 0.82 cutoff even though the alias
        is completely, unambiguously present).

        Requires the matched alias to contribute at least 2 real content
        words, specifically so a short/generic single-word alias (a bare
        "vm" or "list") can't fire this off one incidental shared word --
        that keeps this tier's precision close to tier 1's (real,
        multi-word, exact substring-of-tokens match), not tier 5's
        (approximate). Distinct commands found this way -> AMBIGUOUS,
        same fail-safe posture as every other tier here.
        """
        content = set(_content_words(tokens))
        if len(content) < 2:
            return None
        matched: Dict[str, tuple] = {}
        for alias_text, name, syntax, danger, admin, confirm, category in self._all_alias_rows():
            alias_words = set(_content_words(alias_text.split()))
            # Padding-verb words still have to be PRESENT in the query
            # (alias_words.issubset(content) below still checks the full
            # set) -- they're just not allowed to be the thing that
            # SATISFIES the "2 real distinguishing words" bar on their
            # own, since they're template synonym-padding, not real
            # per-command content. See ALIAS_PADDING_VERBS above.
            distinguishing = alias_words - ALIAS_PADDING_VERBS
            if len(distinguishing) >= 2 and alias_words.issubset(content):
                matched[name] = (name, syntax, danger, admin, confirm, category)
        if not matched:
            return None
        # BETA 0.3.64 (this session): a single containment match is
        # deliberately NEVER returned as bare RESOLVED, even though every
        # other single-match tier above (1/2/3/5) does exactly that.
        # Found three distinct real false positives testing this same
        # session -- "make a new folder called test" -> New-VM (via
        # "make a new vm"), "copy file to backup" -> Out-File (via
        # "backup file") -- both purely from two real words coexisting
        # SOMEWHERE in the query, with no positional/grammatical
        # constraint, because alias text here doesn't reliably encode
        # word order or verb/object role ("backup" as a target noun vs.
        # "backup" as the alias's intended verb are indistinguishable to
        # a bag-of-words check). Tiers 1/2/3/5 don't have this problem --
        # they match against near-complete alias strings, not scattered
        # word membership, so a single hit there is much stronger
        # evidence. Tier 9's real value is surfacing a good, GROUNDED
        # candidate (or two) instead of leaving the right answer buried
        # in loose_candidates -- same value whether reported as
        # RESOLVED-with-uncertainty or AMBIGUOUS-with-one-candidate, and
        # AMBIGUOUS is the status this codebase already uses everywhere
        # else to mean "don't auto-trust this," so reusing it here costs
        # nothing and is consistent with every other tier's fail-safe
        # posture.
        return {
            "status": "AMBIGUOUS",
            "tier": 9,
            "candidates": [(r[0], r[1], r[2]) for r in matched.values()],
        }

    def _bracket_resolve(
        self, q: str, tokens: List[str], original_query: str
    ) -> Optional[Dict[str, Any]]:
        """Tier 7: handles phrasings where the verb and the object noun
        BOOKEND the value -- "stop the print spooler service" (verb=stop,
        value="print spooler", noun=service) or "format the usb drive"
        (verb=format, value=usb, noun=drive). None of tiers 1-6 reach this
        shape: tier 2's prefix-strip only removes a trailing value (verb-
        then-value, no trailing noun to match), and tier 6's abbreviation
        swap doesn't relocate where the value sits.

        Deliberately narrow, given this sits right next to
        orchestrator.py's destructive-shadow guard and auto-dispatch path:
          - single leading token (the verb) + single trailing token (the
            noun) only -- NOT multi-word head/tail combos. Keeps the
            search space small enough to audit by hand rather than
            silently bridging things that were never meant to match.
          - "<head> <tail>" must be an EXACT alias-table match, same
            standard as tier 1 -- never fuzzy, unlike tier 5. A bracket
            guess that ALSO had to be fuzzy would stack two forms of
            uncertainty on top of each other.
          - requires a genuine non-empty middle (the value); a query
            that already matches head+tail back-to-back is tier 1's job.
          - unambiguous single command only -- 2+ real alias hits for
            "<head> <tail>" -> AMBIGUOUS, not a guess at which one.
          - NOT composed with tier 6's abbreviation retry (i.e. this is
            never run against an abbreviation-swapped variant, and tier 6
            never re-runs this) -- each is a real widening of what can
            match on its own; stacking both at once (e.g. an abbreviation
            swap that only THEN becomes a bracket match) hasn't been
            exercised or reasoned through yet, so it deliberately isn't
            wired up until it has been.

        Returns None (not UNRESOLVED) on a miss, so the caller falls
        through to its own final UNRESOLVED/loose_candidates -- same
        convention as every other tier here returning early only on a
        genuine RESOLVED/AMBIGUOUS.
        """
        if len(tokens) < 3:
            return None
        head, tail = tokens[0], tokens[-1]
        middle = tokens[1:-1]
        if not middle:
            return None

        candidate = f"{head} {tail}"
        res = self.conn.execute(
            "MATCH (a:Alias {text: $q})<-[:HasAlias]-(c:Command) "
            "RETURN c.name, c.syntax, c.danger_level, c.requires_admin, c.requires_confirmation, c.category",
            {"q": candidate},
        )
        rows = []
        while res.has_next():
            rows.append(res.get_next())
        if not rows:
            return None
        if len(rows) > 1:
            return {"status": "AMBIGUOUS", "tier": 7, "candidates": [(r[0], r[1], r[2]) for r in rows]}

        # Safety check (BETA 0.3.64, this session -- real bug found via
        # stress testing): this tier's whole design collapses everything
        # between head and tail down to nothing, on the assumption the
        # middle is pure filler/modifiers of the tail noun (true for
        # "format the usb drive" -> middle "the usb" modifies "drive").
        # That assumption breaks when a middle word is actually the real
        # object of the sentence, not a modifier -- "create a checkpoint
        # of the vm" degrades to head+tail "create vm", confidently
        # returning New-VM, and silently discards "checkpoint", which was
        # what the user actually asked to create. Guard against this by
        # checking whether any real (non-stopword) middle word, paired
        # with the SAME tail, is ALSO a genuine exact alias for a
        # DIFFERENT command -- if so, the middle wasn't filler, and
        # trusting the cruder head+tail guess over it would be
        # confidently wrong. Fall to AMBIGUOUS (both real candidates)
        # instead, same fail-safe posture as every other tier here.
        competing = []
        for word in middle:
            if word in STOPWORDS:
                continue
            alt = f"{word} {tail}"
            if alt == candidate:
                continue
            res2 = self.conn.execute(
                "MATCH (a:Alias {text: $q})<-[:HasAlias]-(c:Command) "
                "RETURN c.name, c.syntax, c.danger_level, c.requires_admin, c.requires_confirmation, c.category",
                {"q": alt},
            )
            while res2.has_next():
                r2 = res2.get_next()
                if r2[0] != rows[0][0]:
                    competing.append(r2)

        # Broader version of the same check (BETA 0.3.64, this session):
        # the exact "word + tail" check above still missed a real,
        # dangerous case -- "add a hard disk to the vm" collapses to
        # "add vm" -> confidently RESOLVED New-VM (creating a whole new
        # VM), even though Add-VMHardDiskDrive's real alias is "add vm
        # hard disk drive". The exact check can't find it because that
        # alias also contains the word "drive", which the user's phrasing
        # never used -- "word + tail" as an exact string never matches.
        # So for any middle word not already covered above, also check
        # for OTHER commands with an alias that CONTAINS both that word
        # and the tail as whole words (substring, not exact-string) --
        # strictly broader than the check above, so it can only add
        # competing candidates, never remove the safety the exact check
        # already provides. Still can only ever downgrade a bracket guess
        # to AMBIGUOUS, same as every other check on this tier -- never
        # promotes anything to a new RESOLVED on its own.
        if not competing:
            for word in middle:
                if word in STOPWORDS or word in ALIAS_PADDING_VERBS:
                    continue
                res3 = self.conn.execute(
                    "MATCH (a:Alias)<-[:HasAlias]-(c:Command) "
                    "WHERE a.text =~ $wpat AND a.text =~ $tpat "
                    "RETURN DISTINCT c.name, c.syntax, c.danger_level, c.requires_admin, c.requires_confirmation, c.category",
                    {"wpat": rf".*\b{re.escape(word)}\b.*", "tpat": rf".*\b{re.escape(tail)}\b.*"},
                )
                while res3.has_next():
                    r3 = res3.get_next()
                    if r3[0] != rows[0][0]:
                        competing.append(r3)

        if competing:
            seen = {rows[0][0]: rows[0]}
            for r in competing:
                seen.setdefault(r[0], r)
            all_rows = list(seen.values())
            return {"status": "AMBIGUOUS", "tier": 7, "candidates": [(r[0], r[1], r[2]) for r in all_rows]}

        out = self._resolved(7, rows[0])

        # Recover the bracketed value with real casing/punctuation --
        # same approach as tier 2's stripped_value (see
        # _strip_filler_prefixes_preserving_original): lines up 1:1 with
        # `tokens` because normalize() only strips punctuation WITHIN
        # tokens, never merges/splits whole tokens.
        original_tokens = _strip_filler_prefixes_preserving_original(original_query).split()
        if len(original_tokens) == len(tokens):
            value_tokens = original_tokens[1:-1]
        else:
            value_tokens = middle  # defensive fallback, same posture as tier 2

        # Strip a leading article off the bracketed value -- "the print
        # spooler" -> "print spooler" is what a person would actually
        # type as a service/process name; "the" never is. One fixed
        # check, not a general stopword filter (which would risk eating
        # real name words elsewhere).
        if value_tokens and value_tokens[0].lower() in ("the", "a", "an"):
            value_tokens = value_tokens[1:]
        if not value_tokens:
            return None

        out["stripped_value"] = " ".join(value_tokens)
        return out

    def _resolved(self, tier: int, row) -> Dict[str, Any]:
        name, syntax, danger, admin, confirm, category = row
        return {
            "status": "RESOLVED",
            "tier": tier,
            "command": name,
            "syntax": syntax,
            "danger_level": danger,
            "requires_admin": admin,
            "requires_confirmation": confirm,
            "category": category,
        }

    def loose_search(self, q: str, limit: int = 8) -> List[tuple]:
        """Grounding material for the model on a genuine UNRESOLVED, not a
        resolved answer -- orchestrator.py never auto-dispatches this."""
        if self.conn is None:
            return []
        words = [w for w in q.split() if len(w) > 2]
        if not words:
            return []
        res = self.conn.execute(
            """
            MATCH (c:Command)
            WHERE ANY(w IN $words WHERE c.description CONTAINS w OR c.category CONTAINS w OR c.name CONTAINS w)
            RETURN c.name, c.description, c.syntax
            LIMIT $limit
            """,
            {"words": words, "limit": limit},
        )
        rows = []
        while res.has_next():
            rows.append(res.get_next())
        return rows

    def verify_model_suggestion(self, command_name: str) -> Dict[str, Any]:
        """Grounds a model-proposed command name against the graph's real
        vetted metadata before anything downstream trusts it. Not wired
        into orchestrator.py's dispatch path yet -- there's currently no
        call site that lets the model propose a windows_command_library
        command name at all (UNRESOLVED just falls through to the normal
        CHAT/GENERATE/ASK_CONTEXT classification). Left available for
        whenever that "model proposes, graph verifies" path gets added."""
        if self.conn is None:
            return {"grounded": False}
        res = self.conn.execute(
            "MATCH (c:Command {name: $name}) "
            "RETURN c.syntax, c.danger_level, c.requires_admin, c.requires_confirmation",
            {"name": command_name},
        )
        if res.has_next():
            syntax, danger, admin, confirm = res.get_next()
            return {
                "grounded": True,
                "syntax": syntax,
                "danger_level": danger,
                "requires_admin": admin,
                "requires_confirmation": confirm,
            }
        return {"grounded": False}
