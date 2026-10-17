"""Test fixtures and configuration."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Test this checkout's sources: an editable install in a shared venv points
# at whichever worktree ran `pip install -e .`, so prepend the local src.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture(scope="session", autouse=True)
def test_environment():
    """Set up test environment."""
    # Ensure we're in the right directory
    original_cwd = os.getcwd()
    repo_root = Path(__file__).parent.parent
    os.chdir(repo_root)

    yield

    # Cleanup
    os.chdir(original_cwd)


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_env_vars():
    """Mock environment variables for testing."""
    return {
        "LINODE_OAUTH_CLIENT_ID": "test_oauth_client_id",
        "LINODE_API_TOKEN": "test_linode_token",
    }


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Keep specs and saved sign-ins out of the user's real cache directory."""
    monkeypatch.setenv("LINODE_MCP_CACHE_DIR", str(tmp_path / "cache"))
