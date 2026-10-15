"""Streamlit entry point. Run with ``streamlit run app.py``."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from bef_redbird.ui import main

main()
