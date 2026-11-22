# macOS Distribution & Homebrew Tap Automation

Nollpost is distributed for macOS via **Homebrew** ([`Hessius/homebrew-tap`](https://github.com/Hessius/homebrew-tap)), supporting both Apple Silicon (`arm64`) and Intel (`x86_64`) Macs, alongside `uv tool` / `pipx` for CLI users. There is no standalone prepackaged `.dmg` or `.zip` bundle.

When installed via Homebrew ([`Formula/nollpost.rb`](../Formula/nollpost.rb)):
1. An isolated Python 3.13 virtualenv is created in `libexec`.
2. Playwright Chromium is installed cleanly into `$(brew --prefix)/var/nollpost/ms-playwright` via `post_install_steps`, and `bin/nollpost` sets `PLAYWRIGHT_BROWSERS_PATH` automatically.
3. The lightweight Swift/AppKit menu-bar launcher (`Nollpost.app`) is compiled natively for the Mac's CPU architecture, ad-hoc signed locally with `com.apple.security.automation.apple-events` entitlements (no Developer ID certificate needed), and installed in `$(brew --prefix nollpost)/Nollpost.app`.

---

## Installing & Upgrading via Homebrew

```bash
brew tap hessius/tap
brew trust hessius/tap   # required once on Homebrew 7.0+
brew install hessius/tap/nollpost
```

To launch the optional menu-bar wrapper:

```bash
open "$(brew --prefix nollpost)/Nollpost.app"
```

Both the **Review Board** web UI and the **`Nollpost.app` menu-bar launcher** check `Hessius/homebrew-tap` (6-hour cache, zero user/inbox telemetry; disable via `check_updates = false` in `config.toml`, **Settings**, or `NOLLPOST_NO_UPDATE_CHECK=1`) and surface a notice when a newer release is available:

```bash
brew upgrade hessius/tap/nollpost
```

---

## Automating Releases to `Hessius/homebrew-tap`

Pushing any `v*` tag (after bumping `version` in `pyproject.toml`, `src/nollpost/__init__.py`, and `macos/NollpostMenuBar/Info.plist`) triggers [`.github/workflows/release-homebrew.yml`](../.github/workflows/release-homebrew.yml):

1. Computes the SHA256 of GitHub's `archive/refs/tags/v<version>.tar.gz` tarball and renders `Formula/nollpost.rb` with [`scripts/update_homebrew_formula.py`](../scripts/update_homebrew_formula.py).
2. Creates a GitHub Release with generated release notes if one does not already exist for the tag.
3. Automatically commits and pushes the updated `Formula/nollpost.rb` to `Hessius/homebrew-tap` when the `HOMEBREW_TAP_TOKEN` repository secret is configured.

### One-Time Setup for `HOMEBREW_TAP_TOKEN`
1. Create a GitHub fine-grained Personal Access Token scoped strictly to `Hessius/homebrew-tap` with **Contents: Read and write** permission.
2. Add it as a repository secret named `HOMEBREW_TAP_TOKEN` in **Hessius/nollpost → Settings → Secrets and variables → Actions**.
3. Release a new version with:
   ```bash
   git tag -a v0.2.0 -m "v0.2.0"
   git push origin v0.2.0
   ```
