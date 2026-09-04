#!/usr/bin/env python3
"""Legacy wrapper for: linkedin-archiver recover."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from linkedin_archiver.cli import main

if __name__ == "__main__":
    main(["recover", *sys.argv[1:]])
