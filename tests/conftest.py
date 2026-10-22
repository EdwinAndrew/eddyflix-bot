import sys
from pathlib import Path

# Run from the repo root or anywhere: the bot's modules are top-level files.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
