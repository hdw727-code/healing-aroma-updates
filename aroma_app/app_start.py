"""Initial trusted launcher wrapper. Keep outside downloaded version slots."""
from pathlib import Path
import sys

# Embedded Python deliberately omits the script directory from sys.path.
# This is the initial installed launcher directory, never a downloaded slot.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app_start_stable import main

if __name__=='__main__':raise SystemExit(main())
