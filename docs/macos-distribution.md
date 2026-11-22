# macOS Distribution, Homebrew Tap, and Code Signing Guide

Nollpost provides two macOS distribution paths beyond `uv tool` / `pipx`:

1. **Homebrew Formula ([`Formula/nollpost.rb`](../Formula/nollpost.rb))** covering both Apple Silicon (`arm64`) and Intel (`x86_64`) Macs:
   - Creates an isolated Python 3.13 virtualenv in `libexec`.
   - Installs Playwright Chromium into `$(brew --prefix)/var/nollpost/ms-playwright` during `post_install` and wraps `bin/nollpost` with `PLAYWRIGHT_BROWSERS_PATH`.
   - Compiles the lightweight Swift/AppKit menu-bar launcher (`Nollpost.app`) against Cocoa & ApplicationServices and embeds `Info.plist` + AppleEvents entitlements.
2. **Standalone Universal Menu-Bar App (`Nollpost.app`)** built via [`scripts/build_macos_app.py`](../scripts/build_macos_app.py):
   - Compiles both `arm64` and `x86_64` slices and combines them with `lipo` into a single Universal binary.
   - Starts `nollpost serve --no-open`, waits for loopback readiness, opens the authenticated Review Board in your browser, runs `nollpost doctor`, and surfaces macOS **Automation** (`AEDeterminePermissionToAutomateTarget`) and **Accessibility** (`AXIsProcessTrustedWithOptions`) prompts cleanly.

---

## Setting Up the `Hessius/homebrew-tap` Repository

Before `brew install hessius/tap/nollpost` resolves as a short tap name:

1. Create the public GitHub repository `Hessius/homebrew-tap` containing `Formula/nollpost.rb`.
2. Tag a release in `Hessius/nollpost` (for example `v0.1.0`):
   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```
3. Run [`scripts/update_homebrew_formula.py`](../scripts/update_homebrew_formula.py) against the release tarball (or trigger the `.github/workflows/release-homebrew.yml` workflow) and copy the rendered `nollpost.rb` into `Hessius/homebrew-tap/Formula/nollpost.rb`:
   ```bash
   curl -L https://github.com/Hessius/nollpost/archive/refs/tags/v0.1.0.tar.gz -o /tmp/nollpost-v0.1.0.tar.gz
   python3 scripts/update_homebrew_formula.py --version 0.1.0 --tarball /tmp/nollpost-v0.1.0.tar.gz
   ```
4. Until `Hessius/homebrew-tap` and `v0.1.0` are published, users can install directly from `HEAD` via Homebrew:
   ```bash
   brew tap hessius/nollpost https://github.com/Hessius/nollpost.git
   brew install --HEAD hessius/nollpost/nollpost
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
