from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--build-dir",
        default=None,
        help="Path to build output directory (contains cfnlint/ and standard/)",
    )


@pytest.fixture(scope="session")
def build_dir(request):
    val = request.config.getoption("--build-dir")
    if not val:
        pytest.skip("--build-dir not provided")
    return Path(val)


@pytest.fixture(scope="session")
def cfnlint_dir(build_dir):
    d = build_dir / "cfnlint"
    if not d.exists():
        pytest.skip("cfnlint build not found")
    return d


@pytest.fixture(scope="session")
def standard_dir(build_dir):
    d = build_dir / "standard"
    if not d.exists():
        pytest.skip("standard build not found")
    return d


@pytest.fixture(scope="session")
def extensions_dir():
    d = Path(__file__).parent.parent.parent / "schemas" / "patches" / "extensions"
    if not d.exists():
        pytest.skip("extensions patches not found")
    return d
