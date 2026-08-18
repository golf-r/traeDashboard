"""Packaging tests: the wheel must ship static assets.

Docker relies on ``pip install .``, so the frontend (static/index.html,
app.js, style.css) must be inside the built wheel. This guards against
accidentally dropping the package-data declaration.
"""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _build_wheel(dist: Path) -> Path:
    dist.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                ".",
                "--no-deps",
                "--no-build-isolation",
                "-w",
                str(dist),
            ],
            cwd=str(ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise AssertionError(
            "pip wheel failed:\n"
            f"stdout:\n{exc.stdout}\n"
            f"stderr:\n{exc.stderr}"
        ) from exc
    finally:
        # pip wheel leaves build/ + src/*.egg-info/ in the source tree;
        # remove them so the test doesn't dirty the workspace.
        _rmtree_if_exists(ROOT / "build")
        for p in ROOT.glob("src/*.egg-info"):
            _rmtree_if_exists(p)
    wheels = sorted(dist.glob("*.whl"))
    assert wheels, "no wheel was built"
    return wheels[0]


def _rmtree_if_exists(path: Path) -> None:
    if path.exists():
        import shutil

        shutil.rmtree(path)


def test_wheel_ships_static_frontend(tmp_path: Path) -> None:
    wheel = _build_wheel(tmp_path / "dist")
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
    assert any(n.endswith("trae_dashboard/static/index.html") for n in names)
    assert any(n.endswith("trae_dashboard/static/app.js") for n in names)
    assert any(n.endswith("trae_dashboard/static/style.css") for n in names)
