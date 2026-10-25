from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.deploy.helpers import ResticSetup, make_restic_setup


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[ResticSetup]:
    restic_setup = make_restic_setup(tmp_path)
    yield restic_setup
    restic_setup.writer.close()
