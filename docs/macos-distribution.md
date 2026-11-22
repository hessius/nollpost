# macOS Distribution, Homebrew Tap, and Code Signing Guide

Nollpost provides two macOS distribution paths beyond `uv tool` / `pipx`:

1. **Homebrew Formula ([`Formula/nollpost.rb`](../Formula/nollpost.rb))** covering both Apple Silicon (`arm64`) and Intel (`x86_64`) Macs:
   - Creates an isolated Python 3.13 virtualenv in `libexec`.
   - Installs Playwright Chromium into `$(brew --prefix)/var/nollpost/ms-playwright` during `post_install` and wraps `bin/nollpost` with `PLAYWRIGHT_BROWSERS_PATH`.
   - Compiles the lightweight Swift/AppKit menu-bar launcher (`Nollpost.app`) against Cocoa & ApplicationServices and embeds `Info.plist` + AppleEvents entitlements.
2. **Menu-Bar Wrapper App (`Nollpost.app`)** built via [`scripts/build_macos_app.py`](../scripts/build_macos_app.py):
   - Compiles a thin Swift/AppKit Universal binary (`arm64` + `x86_64` via `lipo`). It does **not** bundle Python or Playwright Chromium by itself; it locates and runs an existing `nollpost` CLI installation (installed via Homebrew, `uv tool`, or `pipx`).
   - Starts `nollpost serve --no-open`, waits for loopback readiness, opens the authenticated Review Board in your browser, runs `nollpost doctor`, and surfaces macOS **Automation** (`AEDeterminePermissionToAutomateTarget`) and **Accessibility** (`AXIsProcessTrustedWithOptions`) prompts cleanly.

---

## Updating `Hessius/homebrew-tap` on New Releases

[`Hessius/homebrew-tap`](https://github.com/Hessius/homebrew-tap) hosts `Formula/nollpost.rb`:

```bash
brew tap hessius/tap
brew trust hessius/tap   # required once on Homebrew 7.0+
brew install hessius/tap/nollpost
```

When releasing a new version:

1. Tag a release in `Hessius/nollpost` (for example `v0.1.0`):
   ```bash
   git tag -a v0.1.0 -m "v0.1.0"
   git push origin v0.1.0
   ```
2. Run [`scripts/update_homebrew_formula.py`](../scripts/update_homebrew_formula.py) against the GitHub release tarball (or copy the `nollpost.rb` artifact produced by `.github/workflows/release-homebrew.yml`) into `Hessius/homebrew-tap/Formula/nollpost.rb`:
   ```bash
   SHA=$(curl -fsSL https://github.com/Hessius/nollpost/archive/refs/tags/v0.1.0.tar.gz | shasum -a 256 | awk '{print $1}')
   python3 scripts/update_homebrew_formula.py --version 0.1.0 --sha256 "$SHA"
   ```

---

## Building, Signing, and Notarizing `Nollpost.app`

Building locally (ad-hoc signed for local testing on the current machine):

```bash
python3 scripts/build_macos_app.py --arch universal --output dist
```

Building with an Apple **Developer ID Application** certificate and notarization credentials (required for Gatekeeper trust across external Macs):

1. Store your App Store Connect / Notary API credentials in Keychain once:
   ```bash
   xcrun notarytool store-credentials "nollpost-notary" \
     --apple-id "<APPLE_ID_EMAIL>" \
     --team-id "<TEAM_ID>" \
     --password "<APP_SPECIFIC_PASSWORD>"
   ```
2. Build, sign with Hardened Runtime (`com.apple.security.automation.apple-events`), submit for notarization, staple the ticket, and archive `dist/Nollpost-<version>-macOS.zip`:
   ```bash
   python3 scripts/build_macos_app.py \
     --arch universal \
     --identity "Developer ID Application: <Your Name> (<TEAM_ID>)" \
     --notarize-profile "nollpost-notary" \
     --output dist
   ```

> **Note:** An `Apple Development` certificate can sign for local testing, whereas public distribution outside Developer Macs requires a `Developer ID Application` certificate plus `notarytool` notarization.
