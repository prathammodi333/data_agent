"""Keep requests in independent tests from sharing the API rate bucket."""

import os
import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def isolated_api_rate_bucket():
    from api import _recent
    _recent.clear()
    yield
    _recent.clear()
