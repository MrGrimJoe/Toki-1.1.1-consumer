"""
migrate_to_kuzu.py -- builds toki_graph_db from source data.

This file didn't ship with the original graph checkpoint (only the
already-built `toki_graph_db` did), so it's reconstructed here rather
than recovered. Two things distinguish "reconstructed" from
"guessed":

1. The Kuzu SCHEMA below (node tables, columns, types, relationship
   tables) is not a guess -- it's copied directly from `CALL
   table_info(...)` / `CALL show_connection(...)` run against the
   real shipped db, so a fresh build from this script produces
   structurally identical tables to what TOKI already ships with.

2. Tier A and Tier B command data (name, tool, syntax, danger_level,
   requires_admin, etc. -- fields that don't exist anywhere in
   intents.py/intents_extended.py/intents_app_control.py, they're
   graph-only metadata) is likewise not invented -- it's extracted
   directly from the shipped db and stored as plain JSON in
   graph_source_data/. Same for Tier B's existing phrasings and
   HAS_INTENT words: this script doesn't fabricate 1,160 Windows
   commands, it re-serializes the ones already validated and shipped.

The one thing this script DOES change from the original build:

- Tier A phrasings now come from graph_source_data/tier_a_phrasings.py
  instead of the original mechanical set. That file fixes a real bug
  (see its docstring: "folder/directory" was silently glued into
  "folderdirectory" by a normalize() that deleted separator characters
  instead of replacing them with a space) and adds more variety per
  command (still hand-WRITTEN, not hand-TESTED -- see that file's
  docstring for the honest caveat).
- Tier A now gets real HAS_INTENT edges (word tokens from its own
  phrasings). The original checkpoint had ZERO HAS_INTENT edges for
  Tier A -- verified directly against the shipped db before this
  script was written -- which meant Tier A could only ever be reached
  through an exact phrasing string match, never fuzzy/traversal
  matching. This script closes that gap at the data layer.
- Both tiers' phrasing text is normalized with the FIXED normalize()
  (character replaced with space, not deleted) so the same class of
  bug can't quietly reappear in Tier B's re-imported phrasings either.

If you have the real windows_command_library.json (the original Tier
B source), pass it via --wcl-json and Tier B will load from that
instead of the extracted graph_source_data/tier_b_*.json fallback --
see load_tier_b_from_wcl_json() below for the expected shape. Without
it, this script uses the extracted fallback, which is real data (not
fabricated) but is a snapshot of what already shipped, not a fresh
import.

Usage:
    pip install kuzu
    python3 migrate_to_kuzu.py [--out toki_graph_db] [--wcl-json path/to/windows_command_library.json]
"""

import argparse
import json
import re
import shutil
from pathlib import Path
import kuzu

from categories import INTENT_CATEGORY, CATEGORIES
from graph_source_data.tier_a_phrasings import TIER_A_PHRASINGS

HERE = Path(__file__).parent
SOURCE_DIR = HERE / "graph_source_data"

STOPWORDS = {
    "a", "an", "the", "please", "me", "my", "can", "you", "could",
    "would", "to", "of", "for", "and", "is", "it", "this", "that",
    "on", "in", "at", "do", "i", "want", "need",
}


# Same list as graph_router.py's _STRIPPED_SUFFIXES -- see that module
# for the full reasoning (a process/file name should score the same with
# or without its extension). BETA 0.3.69: this constant was MISSING here
# entirely until now, a real pre-existing divergence from graph_router.py
# despite this function's own docstring already (incorrectly) claiming
# to be identical. Found via audit_tier_a.py: "convert draft.txt to
# markdown" (a committed phrasing) failed self-consistency after an
# unrelated addition to the same intent's phrasing list, because
# Phrasing.text was stored here as "convert draft txt to markdown" (the
# period became a space, "txt" survived as a real word) while
# graph_router.py's normalize() strips ".txt" at QUERY time before that
# same period-to-space substitution runs -- so the query's content words
# never included "txt" at all, permanently losing one word of overlap
# against every phrasing that happened to include a file extension.
# Harmless while the vector had slack; this session's addition used up
# that slack and made it a visible regression. Fixing the root cause
# here rather than re-padding the phrasing further.
_STRIPPED_SUFFIXES = (".exe", ".txt", ".docx", ".xlsx", ".pdf", ".jpg", ".png")


def normalize(text: str) -> str:
    """Same fixed normalize() as graph_router.py -- non-word/space/brace/
    hyphen characters become a SPACE, not nothing, so e.g. "folder/directory"
    normalizes to "folder directory" (two matchable words) instead of the
    original bug's "folderdirectory" (one useless glued token).

    BETA 0.3.69: also strips apostrophes outright (not to a space) before
    that substitution, AND now strips the same file-extension suffixes
    graph_router.py does (see _STRIPPED_SUFFIXES above) -- MUST stay
    identical to graph_router.py's normalize(), since Phrasing.text is
    stored here at graph-build time and re-read (then re-normalized, a
    no-op if already normalized the same way) by graph_router.py's
    _build_tfidf_index() at query time. Any mismatch here silently
    reintroduces a stored-vs-query normalization gap like the one this
    fix closes -- see graph_router.py's own normalize() docstring for
    the apostrophe half of this same class of bug."""
    text = text.lower().strip()
    for suffix in _STRIPPED_SUFFIXES:
        text = text.replace(suffix, " ")
    text = text.replace("'", "").replace("\u2019", "").replace("\u2018", "")
    text = re.sub(r"[^\w\s{}\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def content_words(norm_text: str) -> set:
    return {w for w in norm_text.split() if w and w not in STOPWORDS}


# ── Schema, copied from the shipped db's own table_info() output ───────────

SCHEMA_DDL = [
    """CREATE NODE TABLE Command(
        id STRING, name STRING, tool STRING, category STRING,
        description STRING, syntax STRING, variables STRING[],
        danger_level STRING, requires_admin BOOL, requires_confirmation BOOL,
        platform STRING, availability STRING, required_module STRING,
        tier STRING, PRIMARY KEY(id)
    )""",
    """CREATE NODE TABLE Phrasing(
        id STRING, text STRING, raw_text STRING, source STRING,
        PRIMARY KEY(id)
    )""",
    """CREATE NODE TABLE Intent(word STRING, PRIMARY KEY(word))""",
    """CREATE NODE TABLE Category(name STRING, PRIMARY KEY(name))""",
    """CREATE REL TABLE HAS_INTENT(FROM Command TO Intent)""",
    """CREATE REL TABLE IN_CATEGORY(FROM Command TO Category)""",
    """CREATE REL TABLE RESOLVES_TO(FROM Phrasing TO Command)""",
]


def build_schema(conn: kuzu.Connection) -> None:
    for ddl in SCHEMA_DDL:
        conn.execute(ddl)


def load_tier_a(conn: kuzu.Connection) -> None:
    commands = json.loads((SOURCE_DIR / "tier_a_commands.json").read_text())
    phrasing_counter = 0

    for cmd in commands:
        conn.execute(
            """CREATE (c:Command {id: $id, name: $name, tool: $tool,
               category: $category, description: $description, syntax: $syntax,
               variables: $variables, danger_level: $danger_level,
               requires_admin: $requires_admin,
               requires_confirmation: $requires_confirmation,
               platform: $platform, availability: $availability,
               required_module: $required_module, tier: $tier})""",
            cmd,
        )

        bare_name = cmd["name"]
        category = INTENT_CATEGORY.get(bare_name)
        if category:
            _ensure_category(conn, category)
            conn.execute(
                "MATCH (c:Command {id: $id}), (cat:Category {name: $cat}) "
                "CREATE (c)-[:IN_CATEGORY]->(cat)",
                {"id": cmd["id"], "cat": category},
            )

        phrasings = TIER_A_PHRASINGS.get(bare_name, [])
        seen_words = set()
        for raw_text in phrasings:
            phrasing_counter += 1
            pid = f"a_p{phrasing_counter:06d}"
            text = normalize(raw_text)
            conn.execute(
                "CREATE (p:Phrasing {id: $id, text: $text, raw_text: $raw, source: $source})",
                {"id": pid, "text": text, "raw": raw_text, "source": "tier_a_seed_v2"},
            )
            conn.execute(
                "MATCH (p:Phrasing {id: $pid}), (c:Command {id: $cid}) "
                "CREATE (p)-[:RESOLVES_TO]->(c)",
                {"pid": pid, "cid": cmd["id"]},
            )
            for w in content_words(text):
                if w not in seen_words:
                    seen_words.add(w)
                    _ensure_intent(conn, w)
                    conn.execute(
                        "MATCH (c:Command {id: $cid}), (i:Intent {word: $w}) "
                        "CREATE (c)-[:HAS_INTENT]->(i)",
                        {"cid": cmd["id"], "w": w},
                    )

    print(f"Tier A: {len(commands)} commands, {phrasing_counter} phrasings loaded")


_known_categories: set = set()
_known_intents: set = set()


def _ensure_category(conn: kuzu.Connection, name: str) -> None:
    if name in _known_categories:
        return
    _known_categories.add(name)
    existing = conn.execute("MATCH (c:Category {name: $n}) RETURN c.name", {"n": name})
    if not existing.has_next():
        conn.execute("CREATE (c:Category {name: $n})", {"n": name})


def _ensure_intent(conn: kuzu.Connection, word: str) -> None:
    if word in _known_intents:
        return
    _known_intents.add(word)
    existing = conn.execute("MATCH (i:Intent {word: $w}) RETURN i.word", {"w": word})
    if not existing.has_next():
        conn.execute("CREATE (i:Intent {word: $w})", {"w": word})


def load_tier_b_from_extracted_fallback(conn: kuzu.Connection) -> None:
    """No original windows_command_library.json was available -- this loads
    the extracted snapshot of what already shipped in the graph db instead
    (graph_source_data/tier_b_*.json), which is real, previously-validated
    data, not invented. Prefer --wcl-json if you have the original file."""
    commands = json.loads((SOURCE_DIR / "tier_b_commands.json").read_text())
    phrasings_by_cmd = json.loads((SOURCE_DIR / "tier_b_phrasings.json").read_text())
    intents_by_cmd = json.loads((SOURCE_DIR / "tier_b_intent_words.json").read_text())

    for cmd in commands:
        conn.execute(
            """CREATE (c:Command {id: $id, name: $name, tool: $tool,
               category: $category, description: $description, syntax: $syntax,
               variables: $variables, danger_level: $danger_level,
               requires_admin: $requires_admin,
               requires_confirmation: $requires_confirmation,
               platform: $platform, availability: $availability,
               required_module: $required_module, tier: $tier})""",
            cmd,
        )
        _ensure_category(conn, cmd["category"])
        conn.execute(
            "MATCH (c:Command {id: $id}), (cat:Category {name: $cat}) "
            "CREATE (c)-[:IN_CATEGORY]->(cat)",
            {"id": cmd["id"], "cat": cmd["category"]},
        )

        for i, ph in enumerate(phrasings_by_cmd.get(cmd["id"], [])):
            pid = f"b_p{cmd['id']}_{i:02d}"
            text = normalize(ph["raw_text"])
            conn.execute(
                "CREATE (p:Phrasing {id: $id, text: $text, raw_text: $raw, source: $source})",
                {"id": pid, "text": text, "raw": ph["raw_text"], "source": ph["source"]},
            )
            conn.execute(
                "MATCH (p:Phrasing {id: $pid}), (c:Command {id: $cid}) "
                "CREATE (p)-[:RESOLVES_TO]->(c)",
                {"pid": pid, "cid": cmd["id"]},
            )

        for w in intents_by_cmd.get(cmd["id"], []):
            _ensure_intent(conn, w)
            conn.execute(
                "MATCH (c:Command {id: $cid}), (i:Intent {word: $w}) "
                "CREATE (c)-[:HAS_INTENT]->(i)",
                {"cid": cmd["id"], "w": w},
            )

    print(f"Tier B: {len(commands)} commands loaded (from extracted fallback, not windows_command_library.json)")


def load_tier_b_from_wcl_json(conn: kuzu.Connection, wcl_path: Path) -> None:
    """Real windows_command_library.json, 1160 entries. Each has real
    `aliases` (hand-written short phrasings) and `examples` (user_input ->
    resolved_command pairs) -- both far better phrasing data than Tier A's
    auto-generated seeds ever were, so ALL 1160 get real HAS_INTENT edges
    and Phrasing nodes here, not just the dispatchable subset.

    Dispatchability is decided per-command, not per-file:
      - variables == []  -> tier "A2": the syntax string has no {slot}
        placeholders, so it's directly runnable the moment the KG matches
        it -- no slot-extraction work needed. _is_dispatchable() allows
        this tier.
      - variables non-empty -> tier "B": syntax needs values TOKI's
        extract_slots() doesn't know how to fill yet (variable names like
        vm_name/setting/value are per-command, not a closed set) -- stays
        hard-blocked from dispatch, same as before, until a generic
        (variable-name-keyed, not intent-keyed) slot filler exists.

    Command.id is stored as f"WCL_{original_id}" (e.g. "WCL_0092") --
    deliberately NOT reusing Tier A's "A_" prefix scheme, so
    orchestrator.py can tell at a glance (and by simple prefix check)
    that a returned intent needs WCL_COMMANDS, not INTENTS, to resolve.
    See _to_intent_name(): it only strips "A_", so "WCL_..." ids pass
    through to the caller unchanged, exactly as needed.
    """
    commands = json.loads(wcl_path.read_text())
    phrasing_counter = 0
    dispatchable_count = 0

    for cmd in commands:
        has_vars = bool(cmd.get("variables"))
        tier = "B" if has_vars else "A2"
        if tier == "A2":
            dispatchable_count += 1

        node_id = f"WCL_{cmd['id']}"
        conn.execute(
            """CREATE (c:Command {id: $id, name: $name, tool: $tool,
               category: $category, description: $description, syntax: $syntax,
               variables: $variables, danger_level: $danger_level,
               requires_admin: $requires_admin,
               requires_confirmation: $requires_confirmation,
               platform: $platform, availability: $availability,
               required_module: $required_module, tier: $tier})""",
            {
                "id": node_id,
                "name": cmd["name"],
                "tool": cmd.get("tool", "powershell"),
                "category": cmd.get("category", "misc"),
                "description": cmd.get("description", ""),
                "syntax": cmd.get("syntax", ""),
                "variables": cmd.get("variables", []),
                "danger_level": cmd.get("danger_level", "safe"),
                "requires_admin": bool(cmd.get("requires_admin", False)),
                "requires_confirmation": bool(cmd.get("requires_confirmation", False)),
                "platform": cmd.get("platform", "windows"),
                "availability": cmd.get("availability", "built_in"),
                "required_module": cmd.get("required_module"),
                "tier": tier,
            },
        )
        _ensure_category(conn, cmd.get("category", "misc"))
        conn.execute(
            "MATCH (c:Command {id: $id}), (cat:Category {name: $cat}) "
            "CREATE (c)-[:IN_CATEGORY]->(cat)",
            {"id": node_id, "cat": cmd.get("category", "misc")},
        )

        # Phrasings: every alias (short, hand-written) + every example's
        # user_input (a full sentence someone actually wrote as a query
        # for this exact command) -- both real data, not fabricated.
        raw_phrasings = list(cmd.get("aliases", []))
        raw_phrasings += [ex["user_input"] for ex in cmd.get("examples", []) if ex.get("user_input")]

        seen_words = set()
        for raw_text in raw_phrasings:
            phrasing_counter += 1
            pid = f"wcl_p{phrasing_counter:06d}"
            text = normalize(raw_text)
            if not text:
                continue
            conn.execute(
                "CREATE (p:Phrasing {id: $id, text: $text, raw_text: $raw, source: $source})",
                {"id": pid, "text": text, "raw": raw_text, "source": "windows_command_library"},
            )
            conn.execute(
                "MATCH (p:Phrasing {id: $pid}), (c:Command {id: $cid}) "
                "CREATE (p)-[:RESOLVES_TO]->(c)",
                {"pid": pid, "cid": node_id},
            )
            for w in content_words(text):
                if w not in seen_words:
                    seen_words.add(w)
                    _ensure_intent(conn, w)
                    conn.execute(
                        "MATCH (c:Command {id: $cid}), (i:Intent {word: $w}) "
                        "CREATE (c)-[:HAS_INTENT]->(i)",
                        {"cid": node_id, "w": w},
                    )
        # Also fold in the file's own curated `intents` words directly --
        # these are short hand-picked keywords (e.g. "add", "floppy"),
        # cheap extra signal on top of the phrasing-derived words above.
        for w in cmd.get("intents", []):
            w = w.strip().lower()
            if w and w not in seen_words:
                seen_words.add(w)
                _ensure_intent(conn, w)
                conn.execute(
                    "MATCH (c:Command {id: $cid}), (i:Intent {word: $w}) "
                    "CREATE (c)-[:HAS_INTENT]->(i)",
                    {"cid": node_id, "w": w},
                )

    print(
        f"windows_command_library: {len(commands)} commands loaded "
        f"({dispatchable_count} tier A2 / dispatchable, "
        f"{len(commands) - dispatchable_count} tier B / matched-only), "
        f"{phrasing_counter} phrasings"
    )


def load_plugins(conn: kuzu.Connection) -> None:
    """
    Discovers plugins in plugins/ and inserts their intents + phrasings into
    the graph database, exactly like Tier A intents. Plugin intents become
    proper Command + Phrasing + HAS_INTENT nodes, making them fuzzy-matchable
    by graph_router.py.

    Safe to call even if plugins/ doesn't exist or no plugins are installed --
    the plugin_manager.load_all() call is idempotent and fails open.
    """
    from plugin_manager import PluginManager
    pm = PluginManager()
    pm.load_all()

    if not pm.intents and not pm.phrasings:
        print("Plugins: none installed, skipping graph injection.")
        return

    phrasing_counter = 0

    for intent_name, defn in pm.intents.items():
        # Build a Command node. Use plugin_id as tier, intent_name as id.
        plugin_id = defn.get("plugin_id", "plugin")
        cmd_id = f"PLUGIN_{intent_name}"
        danger = defn.get("danger_level", "safe")
        template = defn.get("template", "")
        slots = defn.get("slots", [])
        conn.execute(
            """CREATE (c:Command {id: $id, name: $name, tool: $tool,
               category: $category, description: $description, syntax: $syntax,
               variables: $variables, danger_level: $danger_level,
               requires_admin: $requires_admin,
               requires_confirmation: $requires_confirmation,
               platform: $platform, availability: $availability,
               required_module: $required_module, tier: $tier})""",
            {
                "id": cmd_id,
                "name": intent_name,
                "tool": "plugin",
                "category": defn.get("category", "PLUGIN"),
                "description": defn.get("description", ""),
                "syntax": template,
                "variables": slots,
                "danger_level": danger,
                "requires_admin": False,
                "requires_confirmation": danger in ("caution", "destructive"),
                "platform": "windows",
                "availability": "always",
                "required_module": plugin_id,
                "tier": f"plugin_{plugin_id}",
            },
        )

        # Phrasings from manifest + __init__.py register() calls
        seen_words: set = set()
        for raw_text in pm.phrasings.get(intent_name, []):
            phrasing_counter += 1
            pid = f"plg_p{phrasing_counter:06d}"
            text = normalize(raw_text)
            conn.execute(
                "CREATE (p:Phrasing {id: $id, text: $text, raw_text: $raw, source: $source})",
                {"id": pid, "text": text, "raw": raw_text, "source": f"plugin_{plugin_id}"},
            )
            conn.execute(
                "MATCH (p:Phrasing {id: $pid}), (c:Command {id: $cid}) "
                "CREATE (p)-[:RESOLVES_TO]->(c)",
                {"pid": pid, "cid": cmd_id},
            )
            for w in content_words(text):
                if w not in seen_words:
                    seen_words.add(w)
                    _ensure_intent(conn, w)
                    conn.execute(
                        "MATCH (c:Command {id: $cid}), (i:Intent {word: $w}) "
                        "CREATE (c)-[:HAS_INTENT]->(i)",
                        {"cid": cmd_id, "w": w},
                    )

    print(
        f"Plugins: {len(pm.loaded)} loaded, {len(pm.intents)} intents, "
        f"{phrasing_counter} phrasings injected into graph."
    )


def load_active_tier_a_categories(conn: kuzu.Connection) -> None:
    """CHAT/ASK_CONTEXT have no phrasing/command data of their own (see
    graph_router.py's NON_GRAPH_CATEGORIES) but categories.py still wants
    every CATEGORY_NAMES entry to exist as a Category node for
    completeness."""
    for name in CATEGORIES:
        _ensure_category(conn, name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(HERE / "toki_graph_db"))
    parser.add_argument("--wcl-json", default=None, help="Path to the real windows_command_library.json, if you have it")
    args = parser.parse_args()

    out_path = Path(args.out)
    if out_path.exists():
        if out_path.is_dir():
            shutil.rmtree(out_path)
        else:
            out_path.unlink()

    db = kuzu.Database(str(out_path))
    conn = kuzu.Connection(db)

    build_schema(conn)
    load_active_tier_a_categories(conn)
    load_tier_a(conn)
    load_plugins(conn)

    # windows_command_library loading retired from here -- that job now
    # belongs to wcl_resolver.py's own dedicated graph (wcl_kg/), built by
    # the pipeline in wcl_kg/pipeline_scripts_reference/, which does real
    # synonym-cluster widening + tiered resolution instead of this file's
    # flat word-overlap matching. --wcl-json is kept as an explicit opt-in
    # below for anyone who wants the OLD flat-matched Tier B data back in
    # toki_graph_db for some reason, but it's no longer loaded by default.
    if args.wcl_json:
        load_tier_b_from_wcl_json(conn, Path(args.wcl_json))

    conn.close()
    print(f"Graph built at {out_path}")


if __name__ == "__main__":
    main()
