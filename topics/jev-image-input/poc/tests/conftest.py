"""Allow the documented pytest commands to run from the workspace root."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
