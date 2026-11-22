"""Build, sign, optionally notarize, and archive the Nollpost macOS menu-bar .app bundle."""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
MACOS_SRC_DIR = REPO_ROOT / "macos" / "NollpostMenuBar"


def read_project_version(pyproject_path: Path = REPO_ROOT / "pyproject.toml") -> str:
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def render_info_plist(template_path: Path, version: str) -> bytes:
    """Load Info.plist template and stamp CFBundleShortVersionString / CFBundleVersion."""
    with template_path.open("rb") as fh:
        plist = plistlib.load(fh)
    plist["CFBundleShortVersionString"] = version
    plist["CFBundleVersion"] = version
    return plistlib.dumps(plist, fmt=plistlib.FMT_XML)


def _swift_compile(source: Path, target_triple: str, out_bin: Path) -> None:
    cmd = [
        "/usr/bin/swiftc",
        "-O",
        "-parse-as-library",
        "-target",
        target_triple,
        "-framework",
        "Cocoa",
        "-framework",
        "ApplicationServices",
        "-framework",
        "ServiceManagement",
        str(source),
        "-o",
        str(out_bin),
    ]
    subprocess.run(cmd, check=True)


def build_app_bundle(
    *,
    output_dir: Path,
    arch: str = "universal",
    version: Optional[str] = None,
    sign_identity: str = "-",
    notarize_profile: str = "",
    make_zip: bool = True,
) -> tuple[Path, Optional[Path]]:
    """Build and codesign `Nollpost.app` in `output_dir`, returning `(app_path, zip_path)`."""
    if sys.platform != "darwin":
        raise RuntimeError("Building Nollpost.app requires macOS with /usr/bin/swiftc and codesign")

    app_version = version or read_project_version()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    app_path = output_dir / "Nollpost.app"
    if app_path.exists():
        shutil.rmtree(app_path)

    contents = app_path / "Contents"
    macos_dir = contents / "MacOS"
    resources_dir = contents / "Resources"
    macos_dir.mkdir(parents=True, exist_ok=True)
    resources_dir.mkdir(parents=True, exist_ok=True)

    swift_src = MACOS_SRC_DIR / "main.swift"
    bin_path = macos_dir / "Nollpost"

    if arch == "universal":
        arm_bin = output_dir / ".Nollpost.arm64.tmp"
        x64_bin = output_dir / ".Nollpost.x86_64.tmp"
        try:
            _swift_compile(swift_src, "arm64-apple-macos13.0", arm_bin)
            _swift_compile(swift_src, "x86_64-apple-macos13.0", x64_bin)
            subprocess.run(
                ["/usr/bin/lipo", "-create", str(arm_bin), str(x64_bin), "-output", str(bin_path)],
                check=True,
            )
        finally:
            arm_bin.unlink(missing_ok=True)
            x64_bin.unlink(missing_ok=True)
    elif arch == "arm64":
        _swift_compile(swift_src, "arm64-apple-macos13.0", bin_path)
    elif arch == "x86_64":
        _swift_compile(swift_src, "x86_64-apple-macos13.0", bin_path)
    elif arch == "host":
        host_arch = "arm64" if os.uname().machine == "arm64" else "x86_64"
        _swift_compile(swift_src, f"{host_arch}-apple-macos13.0", bin_path)
    else:
        raise ValueError(f"Unsupported arch {arch!r}; expected universal, arm64, x86_64, or host")

    (contents / "Info.plist").write_bytes(render_info_plist(MACOS_SRC_DIR / "Info.plist", app_version))

    entitlements = MACOS_SRC_DIR / "Nollpost.entitlements"
    sign_cmd = [
        "/usr/bin/codesign",
        "--force",
        "--deep",
        "--entitlements",
        str(entitlements),
    ]
    if sign_identity and sign_identity != "-":
        sign_cmd.extend(["--options", "runtime", "--timestamp"])
    sign_cmd.extend(["--sign", sign_identity or "-", str(app_path)])
    subprocess.run(sign_cmd, check=True)
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app_path)], check=True)

    zip_path: Optional[Path] = None
    if make_zip:
        zip_path = output_dir / f"Nollpost-{app_version}-macOS.zip"
        zip_path.unlink(missing_ok=True)
        subprocess.run(
            ["/usr/bin/ditto", "-c", "-k", "--keepParent", str(app_path), str(zip_path)],
            check=True,
        )

    if notarize_profile:
        if sign_identity in ("", "-"):
            raise ValueError("--notarize-profile requires a Developer ID Application signing identity (--identity)")
        submit_target = zip_path
        if submit_target is None:
            submit_target = output_dir / f"Nollpost-{app_version}-notarize.zip"
            subprocess.run(
                ["/usr/bin/ditto", "-c", "-k", "--keepParent", str(app_path), str(submit_target)],
                check=True,
            )
        try:
            subprocess.run(
                [
                    "/usr/bin/xcrun",
                    "notarytool",
                    "submit",
                    str(submit_target),
                    "--keychain-profile",
                    notarize_profile,
                    "--wait",
                ],
                check=True,
            )
            subprocess.run(["/usr/bin/xcrun", "stapler", "staple", str(app_path)], check=True)
            if zip_path is not None:
                zip_path.unlink(missing_ok=True)
                subprocess.run(
                    ["/usr/bin/ditto", "-c", "-k", "--keepParent", str(app_path), str(zip_path)],
                    check=True,
                )
        finally:
            if zip_path is None:
                submit_target.unlink(missing_ok=True)

    return app_path, zip_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and sign Nollpost.app menu-bar launcher")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "dist", help="Output directory (default: dist)")
    parser.add_argument(
        "--arch",
        choices=["universal", "arm64", "x86_64", "host"],
        default="universal",
        help="Target architecture(s)",
    )
    parser.add_argument("--version", default="", help="Override bundle version (default: pyproject.toml version)")
    parser.add_argument(
        "--identity",
        default=os.environ.get("NOLLPOST_SIGN_IDENTITY", "-"),
        help="Codesign identity (default: NOLLPOST_SIGN_IDENTITY env or '-' ad-hoc)",
    )
    parser.add_argument(
        "--notarize-profile",
        default=os.environ.get("NOLLPOST_NOTARY_PROFILE", ""),
        help="Keychain profile name for `xcrun notarytool` (optional)",
    )
    parser.add_argument("--no-zip", action="store_true", help="Skip creating distributable .zip archive")
    args = parser.parse_args(argv)

    app_path, zip_path = build_app_bundle(
        output_dir=args.output,
        arch=args.arch,
        version=args.version or None,
        sign_identity=args.identity,
        notarize_profile=args.notarize_profile,
        make_zip=not args.no_zip,
    )
    print(f"Built {app_path}")
    if zip_path is not None:
        print(f"Packaged {zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
