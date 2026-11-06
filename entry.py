#!/usr/bin/env python3
"""Single entry point for the bundled, root-owned application."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
if __name__ == "__main__":
    command = sys.argv.pop(1) if len(sys.argv) > 1 else "cli"
    if command == "cli":
        from ezrealm.cli import main
    elif command == "web":
        from ezrealm.web import main
    elif command == "broker":
        from ezrealm.broker import main
    elif command == "scheduled":
        from ezrealm.linux import Controller
        def main():
            controller = Controller()
            if controller.status()["active"] == "active":
                controller.service("restart")
    else:
        raise SystemExit("Unknown command")
    main()
