"""Runtime release version, loaded from the Python project metadata."""
from pathlib import Path
import tomllib

APPLICATION_VERSION = tomllib.loads(
    (Path(__file__).resolve().parents[1] / 'pyproject.toml').read_text(encoding='utf-8')
)['project']['version']
