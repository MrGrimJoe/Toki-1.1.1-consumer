# TOKI Plugin Development Guide

Plugins live in this `plugins/` directory.  
Each plugin is **a subdirectory** containing at minimum a `plugin.json` manifest.

---

## Quick Start

```
plugins/
└── my_plugin/
    ├── plugin.json   ← required
    └── __init__.py   ← optional Python code
```

---

## plugin.json Reference

```json
{
  "id": "my_plugin",
  "name": "My Plugin",
  "version": "1.0.0",
  "description": "One sentence what this does.",
  "author": "Your Name",
  "danger_level": "safe",

  "intents": {
    "MY_CUSTOM_COMMAND": {
      "description": "What this command does",
      "kind": "powershell",
      "template": "Write-Host 'Hello {name}'",
      "slots": ["name"],
      "reversible": true,
      "danger_level": "safe"
    }
  },

  "phrasings": {
    "MY_CUSTOM_COMMAND": [
      "greet {name}",
      "say hello to {name}",
      "hello {name}"
    ]
  }
}
```

### Danger Levels

| Level | Effect |
|---|---|
| `safe` | Dispatched automatically |
| `caution` | Pauses and asks the user to press **Enter** or **click the avatar** to confirm |
| `destructive` | Same confirmation gate as `caution`, displayed in red |

### Intent kinds

| kind | What happens |
|---|---|
| `powershell` | Runs `template` as a PowerShell command after slot-filling |
| `chat` | TOKI replies with `template` as plain text |
| `generate` | Routes to generator.py (file generation) |

---

## `__init__.py` API

```python
def register(manager) -> None:
    # manager.add_intent(name, definition, phrasings=[])
    # manager.add_phrasing(intent_name, phrasing_str)
    # manager.add_hook("on_startup", callable)
    pass

def on_startup() -> None:
    pass
```

---

## Notes

- A plugin that fails to load is **skipped silently** — it never crashes TOKI.
- Plugins are graph-only intents (like WCL commands). They are matched by the  
  TF-IDF graph router, never shown to the LLM classifier.
- After adding a plugin, **rebuild the graph database** so your phrasings are indexed:
  ```
  py -3.12 migrate_to_kuzu.py
  ```
