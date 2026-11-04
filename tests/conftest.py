import sys
from pathlib import Path

# make backend modules importable however pytest is started
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
