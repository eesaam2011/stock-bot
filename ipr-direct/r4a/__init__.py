"""R4-A offline-reviewed runtime assembly; imports never open a connection."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'reference_app'))
