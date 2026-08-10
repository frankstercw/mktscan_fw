# conftest.py
import sys
from pathlib import Path

# Ensure mktscan package is importable from tests/
sys.path.insert(0, str(Path(__file__).parent.parent))
