"""End-to-end tests for trae-dashboard.

Exercises the full vertical slice:
    CLI `init` writes config.yaml
        -> CLI `fetch` populates SQLite
        -> FastAPI app exposes /api/health, /api/accounts, /api/accounts/<email>/history

CLI subprocesses use an injected httpx transport (see tests/_e2e_transport.py)
so no network access is required and no production code is mocked.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

# Make the project root importable for subprocess runs.
ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_cli(*args: str, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run the trae-dashboard CLI as a subprocess and return the result."""
    full_env = os.environ.copy()
    full_env["TRAE_APP_ID"] = full_env.get("TRAE_APP_ID", "test_id")
    full_env["TRAE_APP_SECRET"] = full_env.get("TRAE_APP_SECRET", "test_secret")
    # tests/_e2e_transport.py injects a MockTransport when this env var is set
    full_env["TRAE_E2E_TRANSPORT"] = "1"
    # PYTHONPATH=tests lets sitecustomize pick up the transport injector
    existing_pp = full_env.get("PYTHONPATH", "")
    full_env["PYTHONPATH"] = (
        f"{TESTS_DIR}{os.pathsep}{existing_pp}" if existing_pp else str(TESTS_DIR)
    )
    # And the src/ tree, so subprocesses can `import trae_dashboard`
    full_env["PYTHONPATH"] = (
        f"{full_env['PYTHONPATH']}{os.pathsep}{ROOT / 'src'}"
    )
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "trae_dashboard", *args],
        cwd=str(cwd),
        env=full_env,
        capture_output=True,
        text=True,
    )


def _copy_repo_config_example(dest: Path) -> Path:
    """Copy the repo's config.example.yaml to <dest>/config.yaml and return its path."""
    src = ROOT / "config.example.yaml"
    target = dest / "config.yaml"
    target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def _write_env(dest: Path) -> Path:
    """Write a .env file in <dest> with TRAE_APP_ID/SECRET set."""
    p = dest / ".env"
    p.write_text(
        "TRAE_APP_ID=test_id\nTRAE_APP_SECRET=test_secret\n",
        encoding="utf-8",
    )
    return p


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def e2e_workspace(tmp_path, monkeypatch):
    """A scratch workspace with config + .env. Relative paths in the CLI
    (data/dashboard.db) resolve to the temp dir.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRAE_APP_ID", "test_id")
    monkeypatch.setenv("TRAE_APP_SECRET", "test_secret")

    cfg_path = _copy_repo_config_example(tmp_path)
    _write_env(tmp_path)

    return {
        "root": tmp_path,
        "config": cfg_path,
        "env": tmp_path / ".env",
        "data_dir": tmp_path / "data",
        "db": tmp_path / "data" / "dashboard.db",
    }


# ---------------------------------------------------------------------------
# test_init_writes_config
# ---------------------------------------------------------------------------

def test_init_writes_config(tmp_data_dir, monkeypatch):
    """`python -m trae_dashboard init --config X` writes a config.yaml."""
    monkeypatch.setenv("TRAE_APP_ID", "test_id")
    monkeypatch.setenv("TRAE_APP_SECRET", "test_secret")

    src_example = ROOT / "config.example.yaml"
    target_example = tmp_data_dir / "config.example.yaml"
    target_example.write_text(src_example.read_text(encoding="utf-8"), encoding="utf-8")

    target_cfg = tmp_data_dir / "config.yaml"
    result = _run_cli("init", "--config", str(target_cfg), cwd=tmp_data_dir)
    assert result.returncode == 0, (
        f"CLI init failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert target_cfg.exists(), f"config not written: {target_cfg}"
    contents = target_cfg.read_text(encoding="utf-8")
    assert "openapi_base:" in contents
    assert "accounts:" in contents


# ---------------------------------------------------------------------------
# test_fetch_writes_to_sqlite
# ---------------------------------------------------------------------------

def test_fetch_writes_to_sqlite(e2e_workspace):
    """`fetch` populates the SQLite DB from the API (transport-injected)."""
    cfg_path = e2e_workspace["config"]
    db_path = e2e_workspace["db"]
    assert not db_path.exists(), "DB should not exist before fetch"

    result = _run_cli("fetch", "--config", str(cfg_path), cwd=e2e_workspace["root"])
    assert result.returncode == 0, (
        f"CLI fetch failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )

    assert db_path.exists(), f"DB not created at {db_path}"

    conn = sqlite3.connect(str(db_path))
    try:
        snap_count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        model_count = conn.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0]
        account_count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    finally:
        conn.close()

    assert snap_count >= 1, f"expected >=1 snapshot, got {snap_count}"
    assert model_count >= 1, f"expected model_usage rows, got {model_count}"
    assert account_count >= 1, f"expected accounts rows, got {account_count}"


# ---------------------------------------------------------------------------
# test_api_returns_accounts_after_fetch
# ---------------------------------------------------------------------------

def test_api_returns_accounts_after_fetch(e2e_workspace):
    """After fetch, /api/accounts returns at least one account summary."""
    fastapi = pytest.importorskip("fastapi")

    from trae_dashboard.api import create_app  # noqa: E402
    from trae_dashboard.config import load_config  # noqa: E402
    from trae_dashboard.storage import Storage  # noqa: E402

    cfg_path = e2e_workspace["config"]
    fetch = _run_cli("fetch", "--config", str(cfg_path), cwd=e2e_workspace["root"])
    assert fetch.returncode == 0, (
        f"fetch failed\nSTDOUT: {fetch.stdout}\nSTDERR: {fetch.stderr}"
    )

    cfg = load_config(cfg_path)
    storage = Storage(e2e_workspace["db"])
    storage.init()
    app = create_app(cfg=cfg, storage=storage)

    try:
        from fastapi.testclient import TestClient  # type: ignore
    except Exception:  # pragma: no cover
        from starlette.testclient import TestClient  # type: ignore

    with TestClient(app) as client:
        r = client.get("/api/accounts")
        assert r.status_code == 200, f"status={r.status_code} body={r.text}"
        accounts = r.json()
        assert isinstance(accounts, list)
        assert len(accounts) >= 1, f"expected >=1 account, got {accounts}"
        first = accounts[0]
        for key in ("email", "input_tokens", "output_tokens", "amount_total"):
            assert key in first, f"missing {key} in {first}"


# ---------------------------------------------------------------------------
# test_api_account_history
# ---------------------------------------------------------------------------

def test_api_account_history(e2e_workspace):
    """`/api/accounts/<email>/history` returns per-model rows for the cycle."""
    pytest.importorskip("fastapi")
    try:
        from fastapi.testclient import TestClient  # type: ignore
    except Exception:  # pragma: no cover
        from starlette.testclient import TestClient  # type: ignore

    from trae_dashboard.api import create_app  # noqa: E402
    from trae_dashboard.config import load_config  # noqa: E402
    from trae_dashboard.storage import Storage  # noqa: E402

    cfg_path = e2e_workspace["config"]
    fetch = _run_cli("fetch", "--config", str(cfg_path), cwd=e2e_workspace["root"])
    assert fetch.returncode == 0, f"fetch failed: {fetch.stderr}"

    cfg = load_config(cfg_path)
    storage = Storage(e2e_workspace["db"])
    storage.init()
    app = create_app(cfg=cfg, storage=storage)

    with TestClient(app) as client:
        r = client.get("/api/accounts/user01@company.com/history")
        assert r.status_code == 200, f"status={r.status_code} body={r.text}"
        items = r.json()
        assert isinstance(items, list)
        if items:
            sample = items[0]
            for key in ("cycle_start", "cycle_end", "model_name", "input_tokens", "output_tokens"):
                assert key in sample, f"missing {key} in {sample}"


# ---------------------------------------------------------------------------
# test_health_endpoint
# ---------------------------------------------------------------------------

def test_health_endpoint(tmp_data_dir, monkeypatch):
    """`GET /api/health` returns `{ok: true}` from a freshly created app."""
    pytest.importorskip("fastapi")
    try:
        from fastapi.testclient import TestClient  # type: ignore
    except Exception:  # pragma: no cover
        from starlette.testclient import TestClient  # type: ignore

    from trae_dashboard.api import create_app  # noqa: E402
    from trae_dashboard.config import Account, Config  # noqa: E402
    from trae_dashboard.storage import Storage  # noqa: E402

    db = tmp_data_dir / "h.db"
    storage = Storage(db)
    storage.init()
    cfg = Config(
        openapi_base="x",
        auth_endpoint="/a",
        app_id="id",
        app_secret="sec",
        accounts=[Account("a@x.com")],
    )
    app = create_app(cfg=cfg, storage=storage)
    try:
        with TestClient(app) as client:
            r = client.get("/api/health")
            assert r.status_code == 200, f"status={r.status_code} body={r.text}"
            body = r.json()
            assert body == {"ok": True}
    finally:
        storage.close()
