"""main.py — entry point. Checks dependencies, then launches the app."""

import sys


def check_deps():
    missing = []
    for mod in ("PyQt6", "requests"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        print(f"Missing dependencies: {', '.join(missing)}")
        print("Run: pip install -r requirements.txt")
        sys.exit(1)


if __name__ == "__main__":
    check_deps()
    from main_widget import main
    main()
