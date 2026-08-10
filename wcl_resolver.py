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
from pathlib import Path
from typing import Any, Dict, List, Optional

import kuzu

DB_PATH = Path(__file__).parent / "wcl_kg" / "windows_commands_db"

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

        return result  # original UNRESOLVED, with its own loose_candidates

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
