from __future__ import annotations

import pytest

from kimi_bridge.compatibility import (
    SUPPORTED_KIMI_CODE_VERSIONS,
    kimi_code_version_sort_key,
)


@pytest.fixture
def unlisted_kimi_code_version() -> str:
    """Return a valid version that cannot be in the current manifest."""

    next_major = (
        max(
            kimi_code_version_sort_key(version)[0]
            for version in SUPPORTED_KIMI_CODE_VERSIONS
        )
        + 1
    )
    version = f"{next_major}.0.0"
    assert version not in SUPPORTED_KIMI_CODE_VERSIONS
    return version
