"""What every test shares: the toolkit draws to memory, so no window appears while the tests run, wherever they run."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
