"""Separate local viewer for explicitly selected immutable E1 or E2 results."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

if "--prospective" in sys.argv:
    from bef_redbird.research.prospective_dashboard import main as prospective_main

    position = sys.argv.index("--prospective")
    prospective_main(Path(sys.argv[position + 1]) if position + 1 < len(sys.argv) else None)
elif "--e6-volatility" in sys.argv:
    from bef_redbird.research.e6_volatility_dashboard import main as volatility_main

    position = sys.argv.index("--e6-volatility")
    volatility_main(Path(sys.argv[position + 1]) if position + 1 < len(sys.argv) else None)
elif "--e6" in sys.argv:
    from bef_redbird.research.e6_dashboard import main as e6_main

    position = sys.argv.index("--e6")
    e6_main(Path(sys.argv[position + 1]) if position + 1 < len(sys.argv) else None)
elif "--e5" in sys.argv:
    from bef_redbird.research.e5_dashboard import main as e5_main

    position = sys.argv.index("--e5")
    e5_main(Path(sys.argv[position + 1]) if position + 1 < len(sys.argv) else None)
elif "--e4" in sys.argv:
    from bef_redbird.research.e4_dashboard import main as e4_main

    position = sys.argv.index("--e4")
    e4_main(Path(sys.argv[position + 1]) if position + 1 < len(sys.argv) else None)
elif "--e3" in sys.argv:
    from bef_redbird.research.e3_dashboard import main as e3_main

    position = sys.argv.index("--e3")
    e3_main(Path(sys.argv[position + 1]) if position + 1 < len(sys.argv) else None)
elif "--e2" in sys.argv:
    from bef_redbird.research.e2_dashboard import main as e2_main

    position = sys.argv.index("--e2")
    e2_main(Path(sys.argv[position + 1]) if position + 1 < len(sys.argv) else None)
else:
    from bef_redbird.research.dashboard import main as e1_main

    e1_main()
