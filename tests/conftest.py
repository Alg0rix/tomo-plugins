"""Keep integration tests away from the operator's Tomo data."""
import os
import tempfile
from pathlib import Path

root = Path(tempfile.mkdtemp(prefix="tomo-official-tests-"))
os.environ.update(TOMO_HOME=str(root / "home"), TOMO_WORK=str(root / "work"),
                  TOMO_DB_PATH=str(root / "store.db"), TOMO_SKILLS_EXTERNAL_DIRS="",
                  TOMO_TEST_FAST_SQLITE="1")
