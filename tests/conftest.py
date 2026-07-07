"""Shared pytest fixtures."""
from __future__ import annotations
import pytest
from pathlib import Path


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """A clean temporary directory for test data."""
    return tmp_path
