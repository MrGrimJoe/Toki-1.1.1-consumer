"""
plugins/example_plugin/__init__.py

Optional Python initializer for the example plugin.
If you don't need any Python logic, you can delete this file --
the plugin.json alone is enough to register intents and phrasings.

This file demonstrates how to use the PluginManager API for dynamic
registration of intents, phrasings, and lifecycle hooks.
"""

from __future__ import annotations
import logging

_log = logging.getLogger("toki.plugin.example_plugin")


def register(manager) -> None:
    """
    Called by plugin_manager.py after loading this plugin's manifest.
    `manager` is a PluginManager instance.

    You can add extra intents, phrasings, or hooks here that are
    too dynamic to express in plugin.json (e.g. reading a config file,
    checking if a program is installed, etc.).
    """
    # Example: add an extra phrasing for our plugin's intent
    manager.add_phrasing("PLUGIN_HELLO", "say hi from the plugin")

    # Example: register a startup hook
    manager.add_hook("on_startup", _on_startup)

    _log.info("example_plugin registered successfully.")


def _on_startup() -> None:
    """Called once just before TOKI starts handling requests."""
    _log.info("example_plugin: startup hook fired.")
