"""Render and update Formula/nollpost.rb with release tarball URL and SHA256."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
FORMULA_PATH = REPO_ROOT / "Formula" / "nollpost.rb"

FORMULA_TEMPLATE = """class Nollpost < Formula
  desc "Zero-token multi-inbox Apple Mail triage board and Playwright unsubscribe runner"
  homepage "https://github.com/Hessius/nollpost"
  url "__URL__"
  sha256 "__SHA256__"
  license "MIT"
  head "https://github.com/Hessius/nollpost.git", branch: "main"

  depends_on :macos
  depends_on "python@3.13"
  depends_on "uv"

  def install
    python = formula_opt_bin("python@3.13")/"python3.13"
    uv = formula_opt_bin("uv")/"uv"

    ENV["UV_CACHE_DIR"] = (buildpath/".uv-cache").to_s
    system uv, "venv", "--python", python, libexec.to_s
    system uv, "pip", "install", "--python", (libexec/"bin/python").to_s, buildpath.to_s

    (bin/"nollpost").write <<~BASH
      #!/bin/bash
      export PLAYWRIGHT_BROWSERS_PATH="#{var}/nollpost/ms-playwright"
      exec "#{libexec}/bin/nollpost" "$@"
    BASH
    (bin/"nollpost").chmod 0755

    app_contents = prefix/"Nollpost.app/Contents"
    (app_contents/"MacOS").mkpath
    (app_contents/"Resources").mkpath

    target_arch = Hardware::CPU.intel? ? "x86_64" : "arm64"
    system "/usr/bin/swiftc", "-O", "-parse-as-library",
           "-target", "#{target_arch}-apple-macos13.0",
           "-framework", "Cocoa", "-framework", "ApplicationServices", "-framework", "ServiceManagement",
           (buildpath/"macos/NollpostMenuBar/main.swift").to_s,
           "-o", (app_contents/"MacOS/Nollpost").to_s

    head_ver = (buildpath/"pyproject.toml").read[/^version\\s*=\\s*"([^"]+)"/, 1]
    app_ver = if build.head?
      head_ver || "0.2.0"
    else
      version.to_s
    end
    plist = (buildpath/"macos/NollpostMenuBar/Info.plist").read
    plist = plist.gsub(%r{(<key>CFBundleShortVersionString</key>\\s*<string>)[^<]*(</string>)}, "\\\\1#{app_ver}\\\\2")
    plist = plist.gsub(%r{(<key>CFBundleVersion</key>\\s*<string>)[^<]*(</string>)}, "\\\\1#{app_ver}\\\\2")
    (app_contents/"Info.plist").write(plist)

    system "/usr/bin/codesign", "--force", "--deep",
           "--entitlements", (buildpath/"macos/NollpostMenuBar/Nollpost.entitlements").to_s,
           "--sign", "-", (prefix/"Nollpost.app").to_s
  end

  post_install_steps do
    mkdir_p "nollpost/ms-playwright", base: :var
    run "bin/playwright",
        args:         ["install", "chromium"],
        base:         :libexec,
        env:          { "PLAYWRIGHT_BROWSERS_PATH" => "{{var}}/nollpost/ms-playwright" },
        print_stdout: true
  end

  def caveats
    <<~EOS
      Nollpost Chromium binaries are stored in:
        #{var}/nollpost/ms-playwright

      Nollpost.app is built inside Homebrew's Cellar because Homebrew cannot write
      directly into /Applications during formula installation.

      Install Nollpost.app into /Applications and launch the menu-bar icon:
        nollpost app

      Run first-time setup and verify Apple Mail permissions:
        nollpost setup
        nollpost doctor
    EOS
  end

  test do
    assert_match "usage: nollpost", shell_output("#{bin}/nollpost --help")
    assert_predicate prefix/"Nollpost.app/Contents/MacOS/Nollpost", :executable?
  end
end
"""


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def render_formula(*, version: str, sha256: str, url: Optional[str] = None) -> str:
    clean_ver = version.lstrip("v").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[A-Za-z0-9.\-]*)?", clean_ver):
        raise ValueError(f"Invalid version string {version!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256.lower()):
        raise ValueError(f"Invalid SHA256 hex digest {sha256!r}")
    tarball_url = url or f"https://github.com/Hessius/nollpost/archive/refs/tags/v{clean_ver}.tar.gz"
    return FORMULA_TEMPLATE.replace("__URL__", tarball_url).replace("__SHA256__", sha256.lower())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render Formula/nollpost.rb for a release tag")
    parser.add_argument("--version", required=True, help="Release semver tag, e.g. 0.1.0 or v0.1.0")
    parser.add_argument("--sha256", default="", help="Tarball SHA256 hex digest")
    parser.add_argument("--tarball", type=Path, default=None, help="Local tarball to hash instead of --sha256")
    parser.add_argument("--url", default="", help="Override release tarball URL")
    parser.add_argument("--output", type=Path, default=FORMULA_PATH, help="Formula output path")
    args = parser.parse_args(argv)

    digest = args.sha256.strip()
    if args.tarball is not None:
        digest = compute_sha256(args.tarball)
    content = render_formula(version=args.version, sha256=digest, url=args.url or None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
