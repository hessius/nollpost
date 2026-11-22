"""Unit tests for repository PII/secret hygiene scanner and CI workflow presence."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_hygiene_module():
    spec = importlib.util.spec_from_file_location("check_hygiene", ROOT / "scripts" / "check_hygiene.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hygiene_scanner_detects_secrets_paths_and_runtime_files(tmp_path: Path):
    mod = _load_hygiene_module()

    bad_cfg = tmp_path / "config.toml"
    bad_cfg.write_text("", encoding="utf-8")
    bad_db = tmp_path / "state.db"
    bad_db.write_bytes(b"sqlite")
    bad_src = tmp_path / "src.py"
    bad_src.write_text(
        'PATH = "/Us' + 'ers/realperson/mail"\nKEY = "github_' + 'pat_1234567890abcdefghijklmno"\n',
        encoding="utf-8",
    )
    ok_src = tmp_path / "clean.py"
    ok_src.write_text('PATH = "/Users/example/test"\n', encoding="utf-8")
    ok_shot = tmp_path / "docs" / "screenshots" / "demo.png"
    ok_shot.parent.mkdir(parents=True)
    ok_shot.write_bytes(b"\x89PNG\r\n")
    bad_shot = tmp_path / "debug.png"
    bad_shot.write_bytes(b"\x89PNG\r\n")

    issues = mod.scan_paths([bad_cfg, bad_db, bad_src, ok_src, ok_shot, bad_shot], tmp_path)
    joined = "\n".join(issues)
    assert "config.toml" in joined
    assert "state.db" in joined
    assert "realperson" in joined
    assert "GitHub fine-grained PAT" in joined
    assert "debug.png" in joined
    assert "clean.py" not in joined
    assert "demo.png" not in joined


def test_repo_passes_hygiene_and_has_ci_workflow():
    mod = _load_hygiene_module()
    assert (ROOT / ".github" / "workflows" / "ci.yml").exists()
    assert mod.scan_repository(ROOT) == []
