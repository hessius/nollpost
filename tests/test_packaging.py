"""Tests for macOS menu-bar bundle metadata, builder helpers, and Homebrew formula."""

import hashlib
import importlib.util
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FORMULA_PATH = REPO_ROOT / "Formula" / "nollpost.rb"
MACOS_DIR = REPO_ROOT / "macos" / "NollpostMenuBar"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_build_mod = _load_script("build_macos_app")
_formula_mod = _load_script("update_homebrew_formula")
build_app_bundle = _build_mod.build_app_bundle
read_project_version = _build_mod.read_project_version
render_info_plist = _build_mod.render_info_plist
compute_sha256 = _formula_mod.compute_sha256
render_formula = _formula_mod.render_formula


def test_macos_plist_and_entitlements_valid():
    plist = plistlib.loads((MACOS_DIR / "Info.plist").read_bytes())
    assert plist["CFBundleIdentifier"] == "dev.hessius.nollpost"
    assert plist["CFBundleExecutable"] == "Nollpost"
    assert plist["LSUIElement"] is True
    assert "Apple Mail" in plist["NSAppleEventsUsageDescription"]

    entitlements = plistlib.loads((MACOS_DIR / "Nollpost.entitlements").read_bytes())
    assert entitlements["com.apple.security.automation.apple-events"] is True

    stamped = plistlib.loads(render_info_plist(MACOS_DIR / "Info.plist", "1.2.3"))
    assert stamped["CFBundleShortVersionString"] == "1.2.3"
    assert stamped["CFBundleVersion"] == "1.2.3"
    assert read_project_version()


def test_homebrew_formula_structure_and_updater(tmp_path: Path):
    formula = FORMULA_PATH.read_text(encoding="utf-8")
    assert "class Nollpost < Formula" in formula
    assert "depends_on :macos" in formula
    assert 'depends_on "python@3.13"' in formula
    assert 'depends_on "uv"' in formula
    assert "PLAYWRIGHT_BROWSERS_PATH" in formula
    assert 'run "bin/playwright"' in formula
    assert '"install", "chromium"' in formula
    assert "post_install_steps do" in formula
    assert "Nollpost.app/Contents" in formula
    assert "nollpost app" in formula
    assert "ServiceManagement" in formula

    menu_swift = (MACOS_DIR / "main.swift").read_text(encoding="utf-8")
    assert "SMAppService.mainApp" in menu_swift
    assert "Open at Login" in menu_swift
    assert "startupIconTimer" in menu_swift
    assert "strokeFractions" in menu_swift

    sample = tmp_path / "sample.tar.gz"
    sample.write_bytes(b"nollpost-release-tarball")
    digest = compute_sha256(sample)
    assert digest == hashlib.sha256(b"nollpost-release-tarball").hexdigest()

    rendered = render_formula(version="v0.2.0", sha256=digest)
    assert 'url "https://github.com/Hessius/nollpost/archive/refs/tags/v0.2.0.tar.gz"' in rendered
    assert f'sha256 "{digest}"' in rendered
    normalized = re.sub(
        r'url "https://github\.com/Hessius/nollpost/archive/refs/tags/v[^"]+\.tar\.gz"',
        'url "https://github.com/Hessius/nollpost/archive/refs/tags/v0.2.0.tar.gz"',
        formula,
    )
    normalized = re.sub(r'sha256 "[0-9a-f]{64}"', f'sha256 "{digest}"', normalized)
    assert rendered == normalized

    with pytest.raises(ValueError, match="SHA256"):
        render_formula(version="0.2.0", sha256="not-hex")

    if shutil.which("ruby"):
        subprocess.run(["ruby", "-c", str(FORMULA_PATH)], check=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="swiftc/codesign only available on macOS")
def test_build_macos_app_bundle_compiles_universal_and_verifies_signature(tmp_path: Path):
    app_path, zip_path = build_app_bundle(
        output_dir=tmp_path / "out",
        arch="universal",
        version="0.1.0",
        sign_identity="-",
        make_zip=True,
    )
    assert app_path.is_dir()
    binary = app_path / "Contents" / "MacOS" / "Nollpost"
    assert binary.is_file() and (binary.stat().st_mode & 0o111)
    assert (app_path / "Contents" / "Info.plist").is_file()
    assert zip_path is not None and zip_path.is_file()

    archs = subprocess.check_output(["/usr/bin/lipo", "-archs", str(binary)], text=True).split()
    assert set(archs) == {"arm64", "x86_64"}

    verify = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0, verify.stderr
