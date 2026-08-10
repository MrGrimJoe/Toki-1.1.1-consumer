"""
plugin_manager.py — TOKI plugin loader (community extension base).

HOW PLUGINS WORK
----------------
Drop a folder inside plugins/ (e.g. plugins/my_plugin/).
It MUST contain a plugin.json manifest. It MAY contain __init__.py.

plugin.json required fields:
    id          unique snake_case identifier, e.g. "my_plugin"
    name        human-readable label, e.g. "My Plugin"
    version     semver string, e.g. "1.0.0"

plugin.json optional fields:
    description     one-sentence summary
    author          author name / handle
    intents         dict of intent_name → intent definition (same shape as
                    intents.py entries — "description", "kind", "template",
                    "slots", "reversible", optionally "danger_level")
    phrasings       dict of intent_name → list of phrasing strings (to add
                    those phrasings into the graph router on next db rebuild)
    category        str — which TOKI category these intents belong to
                    (currently informational; only CHAT/GENERATE/ASK_CONTEXT
                    are LLM-reachable — plugin intents are graph-only like
                    all WCL_ intents)
    danger_level    default danger_level applied to every intent in this plugin
                    ("safe" / "caution" / "destructive"). Overridden per-intent
                    if the intent definition itself has a "danger_level" key.

__init__.py optional exports:
    register(manager: PluginManager) → None
        Called after the manifest is loaded. Can call manager.add_intent(),
        manager.add_phrasing(), or manager.add_hook() for dynamic registration.
    on_startup() → None
        Called once, just before TOKI starts handling requests.

ZERO HARD DEPENDENCIES — a plugin that fails to load is logged and SKIPPED,
never crashing the main app. A broken plugin is a no-op.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_log = logging.getLogger("toki.plugins")

_HERE = Path(__file__).parent
PLUGINS_DIR = _HERE / "plugins"


class PluginManager:
    """
    Single instance that owns the union of all loaded plugin contributions.
    Call load_all() once at startup, then pass the merged data to the
    orchestrator / migrate_to_kuzu.
    """

    def __init__(self) -> None:
        # merged intent dict — same shape as orchestrator.INTENTS
        self.intents: Dict[str, Dict[str, Any]] = {}
        # merged phrasing dict — intent_name → [phrasing_str, ...]
        self.phrasings: Dict[str, List[str]] = {}
        # hooks keyed by event name — "on_startup", future: "on_dispatch", etc.
        self._hooks: Dict[str, List[Callable]] = {}
        # list of successfully loaded plugin ids
        self.loaded: List[str] = []
        # list of (plugin_id, error_str) for failed plugins
        self.failed: List[tuple] = []

    # ── public API for __init__.py authors ──────────────────────────────────

    def add_intent(
        self,
        intent_name: str,
        definition: Dict[str, Any],
        phrasings: Optional[List[str]] = None,
    ) -> None:
        """Register a single intent from a plugin's Python code."""
        self.intents[intent_name] = definition
        if phrasings:
            self.phrasings.setdefault(intent_name, []).extend(phrasings)

    def add_phrasing(self, intent_name: str, phrasing: str) -> None:
        """Add a single phrasing for an existing (or plugin-defined) intent."""
        self.phrasings.setdefault(intent_name, []).append(phrasing)

    def add_hook(self, event: str, fn: Callable) -> None:
        """Register a callback for a lifecycle event (e.g. 'on_startup')."""
        self._hooks.setdefault(event, []).append(fn)

    # ── loader ───────────────────────────────────────────────────────────────

    def load_all(self) -> None:
        """Scan PLUGINS_DIR for subdirectories containing plugin.json and load each."""
        if not PLUGINS_DIR.is_dir():
            _log.info("No plugins/ directory found — skipping plugin loader.")
            return

        for plugin_dir in sorted(PLUGINS_DIR.iterdir()):
            if not plugin_dir.is_dir():
                continue
            manifest_path = plugin_dir / "plugin.json"
            if not manifest_path.exists():
                _log.debug("Skipping %s — no plugin.json", plugin_dir.name)
                continue
            try:
                self._load_one(plugin_dir, manifest_path)
            except Exception as exc:
                _log.warning(
                    "Plugin %s failed to load: %s", plugin_dir.name, exc, exc_info=True
                )
                self.failed.append((plugin_dir.name, str(exc)))

        total = len(self.loaded) + len(self.failed)
        _log.info(
            "Plugins: %d/%d loaded (%d intents, %d failed).",
            len(self.loaded),
            total,
            len(self.intents),
            len(self.failed),
        )

    def _load_one(self, plugin_dir: Path, manifest_path: Path) -> None:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        plugin_id = raw.get("id") or plugin_dir.name
        name = raw.get("name", plugin_id)
        version = raw.get("version", "0.0.0")

        default_danger = raw.get("danger_level", "safe")

        # ── load intents from manifest ───────────────────────────────────────
        manifest_intents: Dict[str, Dict] = raw.get("intents", {})
        for intent_name, defn in manifest_intents.items():
            merged_defn = dict(defn)
            # apply default danger_level if not overridden per-intent
            if "danger_level" not in merged_defn:
                merged_defn["danger_level"] = default_danger
            # ensure required fields have defaults
            merged_defn.setdefault("kind", "powershell")
            merged_defn.setdefault("slots", [])
            merged_defn.setdefault("reversible", True)
            merged_defn.setdefault("plugin_id", plugin_id)
            self.intents[intent_name] = merged_defn

        # ── load phrasings from manifest ─────────────────────────────────────
        manifest_phrasings: Dict[str, List[str]] = raw.get("phrasings", {})
        for intent_name, phrases in manifest_phrasings.items():
            self.phrasings.setdefault(intent_name, []).extend(phrases)

        # ── load Python __init__.py if present ───────────────────────────────
        init_path = plugin_dir / "__init__.py"
        if init_path.exists():
            module_name = f"toki_plugin_{plugin_id}"
            spec = importlib.util.spec_from_file_location(module_name, init_path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)  # type: ignore[attr-defined]
                register_fn = getattr(module, "register", None)
                if callable(register_fn):
                    register_fn(self)

        self.loaded.append(plugin_id)
        _log.info("Plugin loaded: %s v%s (%s)", name, version, plugin_id)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def fire_startup(self) -> None:
        """Call every registered on_startup hook."""
        for fn in self._hooks.get("on_startup", []):
            try:
                fn()
            except Exception as exc:
                _log.warning("on_startup hook error: %s", exc, exc_info=True)

    # ── summary ───────────────────────────────────────────────────────────────

    def summary(self) -> str:
        lines = [f"=== TOKI Plugin Report ({len(self.loaded)} loaded) ==="]
        for pid in self.loaded:
            intent_count = sum(
                1 for defn in self.intents.values() if defn.get("plugin_id") == pid
            )
            lines.append(f"  ✓ {pid}  ({intent_count} intents)")
        for pid, err in self.failed:
            lines.append(f"  ✗ {pid}  ERROR: {err}")
        if not self.loaded and not self.failed:
            lines.append("  (no plugins installed)")
        return "\n".join(lines)


# module-level singleton so orchestrator / migrate_to_kuzu import the same instance
plugin_manager = PluginManager()
