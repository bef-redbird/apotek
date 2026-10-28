"""Local pharmacist workspace; configure with REDBIRD_ASSESSMENT_CONFIG."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from bef_redbird.assessment.ui import main  # noqa: E402

main(
    Path(os.environ["REDBIRD_ASSESSMENT_CONFIG"])
    if os.environ.get("REDBIRD_ASSESSMENT_CONFIG")
    else None
)
