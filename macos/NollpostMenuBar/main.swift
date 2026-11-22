import ApplicationServices
import Cocoa
import Foundation

private enum AutomationStatus: Sendable {
    case granted
    case denied
    case consentRequired
    case mailNotRunning
    case error(OSStatus)

    var label: String {
        switch self {
        case .granted:
            return "Granted"
        case .denied:
            return "Denied (enable Nollpost -> Mail in Automation)"
        case .consentRequired:
            return "Prompt required"
        case .mailNotRunning:
            return "Mail.app is not running"
        case .error(let code):
            return "Error (OSStatus \(code))"
        }
    }
}

private enum ServerState: Equatable {
    case stopped
    case starting
    case running(URL)
    case failed(String)

    var menuSummary: String {
        switch self {
        case .stopped:
            return "Review Board: Stopped"
        case .starting:
            return "Review Board: Starting..."
        case .running(let url):
            let port = url.port ?? 0
            return "Review Board: Running (127.0.0.1:\(port))"
        case .failed(let reason):
            return "Review Board: Failed (\(reason))"
        }
    }
}

@main
@MainActor
final class NollpostMenuApp: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var stateMenuItem: NSMenuItem!
    private var openBoardMenuItem: NSMenuItem!
    private var startStopMenuItem: NSMenuItem!
    private var restartMenuItem: NSMenuItem!

    private var serverProcess: Process?
    private var outputPipe: Pipe?
    private var serverState: ServerState = .stopped {
        didSet { updateMenuState() }
    }
    private var lineBuffer = ""
    private var recentLogLines: [String] = []
    private var pendingBrowserOpen = false
    private var readyCheckTimer: Timer?
    private var logHandle: FileHandle?

    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let delegate = NollpostMenuApp()
        app.delegate = delegate
        app.run()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildStatusMenu()
        openLogFile()
        updateMenuState()
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopServerProcess(waitForExit: true)
        try? logHandle?.close()
    }

    private func buildStatusMenu() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            if let sym = NSImage(systemSymbolName: "envelope.badge", accessibilityDescription: "Nollpost") {
                sym.isTemplate = true
                button.image = sym
            } else {
                button.title = "Nollpost"
            }
            button.toolTip = "Nollpost Morning Mail Triage"
        }

        let menu = NSMenu()
        stateMenuItem = NSMenuItem(title: serverState.menuSummary, action: nil, keyEquivalent: "")
        stateMenuItem.isEnabled = false
        menu.addItem(stateMenuItem)
        menu.addItem(.separator())

        openBoardMenuItem = NSMenuItem(title: "Open Review Board", action: #selector(openReviewBoard(_:)), keyEquivalent: "o")
        openBoardMenuItem.target = self
        menu.addItem(openBoardMenuItem)

        startStopMenuItem = NSMenuItem(title: "Start Server", action: #selector(toggleServer(_:)), keyEquivalent: "")
        startStopMenuItem.target = self
        menu.addItem(startStopMenuItem)

        restartMenuItem = NSMenuItem(title: "Restart Server", action: #selector(restartServer(_:)), keyEquivalent: "r")
        restartMenuItem.target = self
        menu.addItem(restartMenuItem)
        menu.addItem(.separator())

        let permItem = NSMenuItem(title: "Check & Request Permissions...", action: #selector(checkAndPromptPermissions(_:)), keyEquivalent: "p")
        permItem.target = self
        menu.addItem(permItem)

        let docItem = NSMenuItem(title: "Run Diagnostics (nollpost doctor)", action: #selector(runDoctorCommand(_:)), keyEquivalent: "d")
        docItem.target = self
        menu.addItem(docItem)

        let autoPrefsItem = NSMenuItem(title: "Open Automation Settings...", action: #selector(openAutomationSettings(_:)), keyEquivalent: "")
        autoPrefsItem.target = self
        menu.addItem(autoPrefsItem)

        let accessPrefsItem = NSMenuItem(title: "Open Accessibility Settings...", action: #selector(openAccessibilitySettings(_:)), keyEquivalent: "")
        accessPrefsItem.target = self
        menu.addItem(accessPrefsItem)
        menu.addItem(.separator())

        let logItem = NSMenuItem(title: "Open Server Log", action: #selector(openLogInConsole(_:)), keyEquivalent: "l")
        logItem.target = self
        menu.addItem(logItem)

        let quitItem = NSMenuItem(title: "Quit Nollpost", action: #selector(quitApp(_:)), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)

        statusItem.menu = menu
    }

    private func updateMenuState() {
        stateMenuItem?.title = serverState.menuSummary
        switch serverState {
        case .running:
            startStopMenuItem?.title = "Stop Server"
            restartMenuItem?.isEnabled = true
            openBoardMenuItem?.title = "Open Review Board"
        case .starting:
            startStopMenuItem?.title = "Stop Server"
            restartMenuItem?.isEnabled = false
            openBoardMenuItem?.title = "Open Review Board (Starting...)"
        case .stopped, .failed:
            startStopMenuItem?.title = "Start Server"
            restartMenuItem?.isEnabled = false
            openBoardMenuItem?.title = "Start & Open Review Board"
        }
    }

    private var logFileURL: URL {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/Nollpost", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("menubar.log")
    }

    private func openLogFile() {
        let path = logFileURL.path
        if !FileManager.default.fileExists(atPath: path) {
            FileManager.default.createFile(atPath: path, contents: nil)
        }
        logHandle = FileHandle(forWritingAtPath: path)
        _ = try? logHandle?.seekToEnd()
    }

    private func appendLog(_ text: String) {
        if let data = text.data(using: .utf8) {
            try? logHandle?.write(contentsOf: data)
        }
        for line in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let s = String(line).trimmingCharacters(in: .whitespacesAndNewlines)
            if !s.isEmpty {
                recentLogLines.append(s)
                if recentLogLines.count > 40 {
                    recentLogLines.removeFirst(recentLogLines.count - 40)
                }
            }
        }
    }

    private func resolveNollpostExecutable() -> URL? {
        let env = ProcessInfo.processInfo.environment
        let fm = FileManager.default
        if let explicit = env["NOLLPOST_BIN"], !explicit.isEmpty, fm.isExecutableFile(atPath: explicit) {
            return URL(fileURLWithPath: explicit)
        }

        var candidates: [URL] = []
        let bundleURL = Bundle.main.bundleURL.resolvingSymlinksInPath()
        candidates.append(bundleURL.appendingPathComponent("Contents/Resources/bin/nollpost"))
        candidates.append(bundleURL.appendingPathComponent("../bin/nollpost").standardizedFileURL)
        candidates.append(bundleURL.appendingPathComponent("../../bin/nollpost").standardizedFileURL)
        candidates.append(URL(fileURLWithPath: "/opt/homebrew/bin/nollpost"))
        candidates.append(URL(fileURLWithPath: "/usr/local/bin/nollpost"))
        candidates.append(fm.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin/nollpost"))

        if let pathEnv = env["PATH"] {
            for dir in pathEnv.split(separator: ":") {
                candidates.append(URL(fileURLWithPath: String(dir)).appendingPathComponent("nollpost"))
            }
        }

        for candidate in candidates {
            if fm.isExecutableFile(atPath: candidate.path) {
                return candidate
            }
        }
        return nil
    }

    private func childEnvironment(for executable: URL) -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let binDir = executable.deletingLastPathComponent().path
        let basePath = env["PATH"] ?? "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        let requiredDirs = [binDir, "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
        var parts = basePath.split(separator: ":").map(String.init)
        for dir in requiredDirs.reversed() where !parts.contains(dir) {
            parts.insert(dir, at: 0)
        }
        env["PATH"] = parts.joined(separator: ":")
        env["PYTHONUNBUFFERED"] = "1"
        return env
    }

    private nonisolated static func queryMailAutomationPermission(prompt: Bool) async -> AutomationStatus {
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let desc = NSAppleEventDescriptor(bundleIdentifier: "com.apple.mail")
                guard let aeDesc = desc.aeDesc else {
                    continuation.resume(returning: .error(-1))
                    return
                }
                let wild = AEEventClass(typeWildCard)
                let status = AEDeterminePermissionToAutomateTarget(aeDesc, wild, AEEventID(typeWildCard), prompt)
                let result: AutomationStatus
                switch status {
                case noErr:
                    result = .granted
                case OSStatus(errAEEventNotPermitted):
                    result = .denied
                case -1744:
                    result = .consentRequired
                case OSStatus(procNotFound):
                    result = .mailNotRunning
                default:
                    result = .error(status)
                }
                continuation.resume(returning: result)
            }
        }
    }

    private func checkAccessibilityPermission(prompt: Bool) -> Bool {
        let key = "AXTrustedCheckOptionPrompt" as CFString
        let options = [key: prompt] as CFDictionary
        return AXIsProcessTrustedWithOptions(options)
    }

    private func ensureMailRunningIfNeeded() {
        let running = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.mail")
        if running.isEmpty, let mailURL = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.mail") {
            let cfg = NSWorkspace.OpenConfiguration()
            cfg.activates = false
            NSWorkspace.shared.openApplication(at: mailURL, configuration: cfg, completionHandler: nil)
        }
    }

    private func promptIfAutomationMissingBeforeLaunch() async -> Bool {
        let auto = await Self.queryMailAutomationPermission(prompt: true)
        switch auto {
        case .granted:
            return true
        case .mailNotRunning:
            ensureMailRunningIfNeeded()
            return true
        case .denied, .consentRequired, .error:
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "Apple Mail Automation Permission Required"
            alert.informativeText = """
            Status: \(auto.label)

            Nollpost needs Automation access to control Mail.app (for Archive-only triage). Open System Settings -> Privacy & Security -> Automation and enable Mail under Nollpost.

            Note: macOS Accessibility permission is only required if using Gmail's default "ui_menu" Archive strategy.
            """
            alert.addButton(withTitle: "Open Automation Settings")
            alert.addButton(withTitle: "Continue Anyway")
            alert.addButton(withTitle: "Cancel")
            NSApp.activate(ignoringOtherApps: true)
            let response = alert.runModal()
            if response == .alertFirstButtonReturn {
                openPrivacyPane("Privacy_Automation")
                return false
            }
            return response == .alertSecondButtonReturn
        }
    }

    @objc private func openReviewBoard(_ sender: Any?) {
        switch serverState {
        case .running(let url):
            NSWorkspace.shared.open(url)
            return
        case .starting:
            pendingBrowserOpen = true
            return
        case .stopped, .failed:
            Task { @MainActor [weak self] in
                guard let self else { return }
                guard await self.promptIfAutomationMissingBeforeLaunch() else { return }
                self.startServer(openBrowserWhenReady: true)
            }
        }
    }

    @objc private func toggleServer(_ sender: Any?) {
        switch serverState {
        case .running, .starting:
            stopServerProcess(waitForExit: true)
            serverState = .stopped
        case .stopped, .failed:
            startServer(openBrowserWhenReady: false)
        }
    }

    @objc private func restartServer(_ sender: Any?) {
        stopServerProcess(waitForExit: true)
        startServer(openBrowserWhenReady: true)
    }

    private func startServer(openBrowserWhenReady: Bool) {
        guard let exe = resolveNollpostExecutable() else {
            showMissingBinaryAlert()
            return
        }
        stopServerProcess(waitForExit: true)

        recentLogLines.removeAll()
        lineBuffer = ""
        pendingBrowserOpen = openBrowserWhenReady
        serverState = .starting

        let proc = Process()
        proc.executableURL = exe
        proc.arguments = ["serve", "--no-open"]
        proc.environment = childEnvironment(for: exe)

        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe
        outputPipe = pipe

        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            let chunk = String(decoding: data, as: UTF8.self)
            Task { @MainActor [weak self] in
                self?.handleServerOutput(chunk)
            }
        }

        proc.terminationHandler = { [weak self] finished in
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.outputPipe?.fileHandleForReading.readabilityHandler = nil
                self.outputPipe = nil
                self.readyCheckTimer?.invalidate()
                self.readyCheckTimer = nil
                if self.serverProcess === finished {
                    self.serverProcess = nil
                    if finished.terminationStatus != 0 && finished.terminationReason != .uncaughtSignal {
                        let tail = self.recentLogLines.suffix(6).joined(separator: "\n")
                        self.serverState = .failed("exit \(finished.terminationStatus)")
                        self.showAlert(
                            style: .critical,
                            title: "Nollpost Server Exited Unexpectedly",
                            message: tail.isEmpty ? "Check menubar.log for details." : tail
                        )
                    } else if case .failed = self.serverState {
                        // Keep failure status visible
                    } else {
                        self.serverState = .stopped
                    }
                }
            }
        }

        do {
            appendLog("\n--- Launching \(exe.path) serve --no-open ---\n")
            try proc.run()
            serverProcess = proc
        } catch {
            serverState = .failed(error.localizedDescription)
            showAlert(
                style: .critical,
                title: "Could Not Start Nollpost",
                message: error.localizedDescription
            )
        }
    }

    private func handleServerOutput(_ chunk: String) {
        appendLog(chunk)
        lineBuffer += chunk
        while let range = lineBuffer.range(of: "\n") {
            let line = String(lineBuffer[..<range.lowerBound])
            lineBuffer.removeSubrange(..<range.upperBound)
            parseServerLine(line)
        }
    }

    private func parseServerLine(_ line: String) {
        let prefix = "Review Board URL: "
        guard let range = line.range(of: prefix) else { return }
        let raw = String(line[range.upperBound...]).trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: raw) else { return }
        waitUntilServerReachable(url: url, attemptsRemaining: 60)
    }

    private func waitUntilServerReachable(url: URL, attemptsRemaining: Int) {
        readyCheckTimer?.invalidate()
        guard let proc = serverProcess, proc.isRunning else { return }
        guard let staticURL = URL(string: "/static/index.html", relativeTo: url)?.absoluteURL else {
            finishServerReady(url)
            return
        }
        var req = URLRequest(url: staticURL)
        req.timeoutInterval = 0.5
        URLSession.shared.dataTask(with: req) { [weak self] _, response, _ in
            Task { @MainActor [weak self] in
                guard let self, let proc = self.serverProcess, proc.isRunning else { return }
                if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                    self.finishServerReady(url)
                    return
                }
                if attemptsRemaining <= 0 {
                    self.pendingBrowserOpen = false
                    self.serverState = .failed("startup timed out")
                    self.showAlert(
                        style: .warning,
                        title: "Nollpost Startup Timed Out",
                        message: "The local Review Board server did not become reachable in time. Check menubar.log for details."
                    )
                    return
                }
                self.readyCheckTimer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: false) { [weak self] _ in
                    Task { @MainActor [weak self] in
                        self?.waitUntilServerReachable(url: url, attemptsRemaining: attemptsRemaining - 1)
                    }
                }
            }
        }.resume()
    }

    private func finishServerReady(_ url: URL) {
        serverState = .running(url)
        if pendingBrowserOpen {
            pendingBrowserOpen = false
            NSWorkspace.shared.open(url)
        }
    }

    private func stopServerProcess(waitForExit: Bool) {
        readyCheckTimer?.invalidate()
        readyCheckTimer = nil
        pendingBrowserOpen = false
        outputPipe?.fileHandleForReading.readabilityHandler = nil
        outputPipe = nil
        guard let proc = serverProcess else { return }
        serverProcess = nil
        if proc.isRunning {
            proc.terminate()
            if waitForExit {
                let deadline = Date().addingTimeInterval(2.5)
                while proc.isRunning && Date() < deadline {
                    Thread.sleep(forTimeInterval: 0.05)
                }
                if proc.isRunning {
                    kill(proc.processIdentifier, SIGKILL)
                }
            }
        }
    }

    @objc private func checkAndPromptPermissions(_ sender: Any?) {
        Task { @MainActor [weak self] in
            guard let self else { return }
            let auto = await Self.queryMailAutomationPermission(prompt: true)
            let axTrusted = self.checkAccessibilityPermission(prompt: true)

            let alert = NSAlert()
            alert.alertStyle = .informational
            alert.messageText = "macOS Permissions for Nollpost"
            alert.informativeText = """
            Mail.app Automation (required): \(auto.label)
            Accessibility UI Scripting (only for Gmail ui_menu mode): \(axTrusted ? "Granted" : "Not granted")

            Tip: If Mail.app was not running when checked, start Mail.app and click Check & Request Permissions again, or approve Nollpost under System Settings -> Privacy & Security.
            """
            alert.addButton(withTitle: "Done")
            alert.addButton(withTitle: "Open Automation Settings")
            alert.addButton(withTitle: "Open Accessibility Settings")
            NSApp.activate(ignoringOtherApps: true)
            let res = alert.runModal()
            if res == .alertSecondButtonReturn {
                self.openPrivacyPane("Privacy_Automation")
            } else if res == .alertThirdButtonReturn {
                self.openPrivacyPane("Privacy_Accessibility")
            }
        }
    }

    @objc private func runDoctorCommand(_ sender: Any?) {
        guard let exe = resolveNollpostExecutable() else {
            showMissingBinaryAlert()
            return
        }
        let env = childEnvironment(for: exe)
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let proc = Process()
            proc.executableURL = exe
            proc.arguments = ["doctor", "--json"]
            proc.environment = env
            let outPipe = Pipe()
            proc.standardOutput = outPipe
            proc.standardError = outPipe
            do {
                try proc.run()
                let data = outPipe.fileHandleForReading.readDataToEndOfFile()
                proc.waitUntilExit()
                let formatted = Self.formatDoctorJSON(data)
                let ok = proc.terminationStatus == 0
                Task { @MainActor [weak self] in
                    self?.showAlert(
                        style: ok ? .informational : .warning,
                        title: ok ? "Nollpost Doctor: All Critical Checks Passed" : "Nollpost Doctor: Action Needed",
                        message: formatted
                    )
                }
            } catch {
                let errMsg = error.localizedDescription
                Task { @MainActor [weak self] in
                    self?.showAlert(style: .critical, title: "Diagnostics Failed", message: errMsg)
                }
            }
        }
    }

    private nonisolated static func formatDoctorJSON(_ data: Data) -> String {
        guard
            let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let checks = obj["checks"] as? [[String: Any]]
        else {
            return String(data: data, encoding: .utf8) ?? "Could not parse doctor output."
        }
        return checks.map { c in
            let st = (c["status"] as? String ?? "?").uppercased()
            let name = c["name"] as? String ?? "check"
            let detail = c["detail"] as? String ?? ""
            let rem = c["remediation"] as? String ?? ""
            return rem.isEmpty ? "[\(st)] \(name): \(detail)" : "[\(st)] \(name): \(detail)\n  -> \(rem)"
        }.joined(separator: "\n")
    }

    @objc private func openAutomationSettings(_ sender: Any?) {
        openPrivacyPane("Privacy_Automation")
    }

    @objc private func openAccessibilitySettings(_ sender: Any?) {
        openPrivacyPane("Privacy_Accessibility")
    }

    private func openPrivacyPane(_ anchor: String) {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(anchor)") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func openLogInConsole(_ sender: Any?) {
        NSWorkspace.shared.open(logFileURL)
    }

    @objc private func quitApp(_ sender: Any?) {
        NSApp.terminate(sender)
    }

    private func showMissingBinaryAlert() {
        serverState = .failed("nollpost CLI not found")
        showAlert(
            style: .critical,
            title: "Nollpost CLI Executable Not Found",
            message: "Could not locate `nollpost` in Homebrew (/opt/homebrew/bin, /usr/local/bin), ~/.local/bin, or PATH.\n\nInstall via:\n  brew install hessius/tap/nollpost"
        )
    }

    private func showAlert(style: NSAlert.Style, title: String, message: String) {
        let alert = NSAlert()
        alert.alertStyle = style
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }
}
