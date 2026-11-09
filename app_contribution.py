#!/usr/bin/env python3
"""Run the Vault owner from this Package's shipped source."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from agent_session_vault.app_review import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
