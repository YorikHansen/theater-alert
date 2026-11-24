'''Theater Alert - Monitor seat availability at Theater Kiel.'''

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib


def _read_version() -> str:
    try:
        return version('theater-alert')
    except PackageNotFoundError:
        pyproject = Path(__file__).resolve().parents[2] / 'pyproject.toml'
        with pyproject.open('rb') as handle:
            data = tomllib.load(handle)
        return str(data['project']['version'])


__version__ = _read_version()