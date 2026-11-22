class Nollpost < Formula
  desc "Zero-token multi-inbox Apple Mail triage board and Playwright unsubscribe runner"
  homepage "https://github.com/Hessius/nollpost"
  url "https://github.com/Hessius/nollpost/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "9bd3c5edcc0d0bc955ae63d879e3a28358c0be449b5823a4e120343c297ad355"
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

    head_ver = (buildpath/"pyproject.toml").read[/^version\s*=\s*"([^"]+)"/, 1]
    app_ver = if build.head?
      head_ver || "0.2.0"
    else
      version.to_s
    end
    plist = (buildpath/"macos/NollpostMenuBar/Info.plist").read
    plist = plist.gsub(%r{(<key>CFBundleShortVersionString</key>\s*<string>)[^<]*(</string>)}, "\\1#{app_ver}\\2")
    plist = plist.gsub(%r{(<key>CFBundleVersion</key>\s*<string>)[^<]*(</string>)}, "\\1#{app_ver}\\2")
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
