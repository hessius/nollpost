import ApplicationServices
import Cocoa
import Foundation
import ServiceManagement

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
    case idleStopped
    case starting
    case running(URL)
    case failed(String)

    var menuSummary: String {
        switch self {
        case .stopped:
            return "Review Board: Stopped"
        case .idleStopped:
            return "Review Board: Stopped (idle)"
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

private struct AutoStatusSnapshot: Decodable {
    let unseenCount: Int
    let lastRunAt: String?
    let lastStatus: String?
    let lastError: String?

    enum CodingKeys: String, CodingKey {
        case unseenCount = "unseen_count"
        case lastRunAt = "last_run_at"
        case lastStatus = "last_status"
        case lastError = "last_error"
    }
}

@main
@MainActor
final class NollpostMenuApp: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var statusItem: NSStatusItem!
    private var stateMenuItem: NSMenuItem!
    private var autoStatusMenuItem: NSMenuItem!
    private var updateNoticeMenuItem: NSMenuItem!
    private var openBoardMenuItem: NSMenuItem!
    private var runAutoNowMenuItem: NSMenuItem!
    private var startStopMenuItem: NSMenuItem!
    private var restartMenuItem: NSMenuItem!
    private var idleModeMenuItem: NSMenuItem!
    private var pauseAutoMenuItem: NSMenuItem!
    private var launchAtLoginMenuItem: NSMenuItem!

    private var serverProcess: Process?
    private var outputPipe: Pipe?
    private var serverState: ServerState = .stopped {
        didSet { updateMenuState() }
    }
    private var latestAvailableVersion: String?
    private var idleAutoStopped = false
    private let idleDisabledKey = "idleAutoStopDisabled"
    private let autoPausedKey = "autoTriagePaused"
    private var lineBuffer = ""
    private var recentLogLines: [String] = []
    private var pendingBrowserOpen = false
    private var readyCheckTimer: Timer?
    private var startupIconTimer: Timer?
    private var startupAnimStart: Date?
    private var autoTriageTimer: Timer?
    private var scheduledAutoIntervalMinutes: Int = 60
    private var isAutoRunInFlight = false
    private var unseenAutoCount: Int = 0
    private var lastAutoRunAtISO: String?
    private var lastAutoStatusText: String = "never"
    private var dataDirSource: DispatchSourceFileSystemObject?
    private var dataDirFD: Int32 = -1
    private var configDirSource: DispatchSourceFileSystemObject?
    private var configDirFD: Int32 = -1
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
        reloadAutoStatus()
        startAutoStatusWatcher()
        startConfigDirWatcher()
        scheduleAutoTriageTimer()
        NSWorkspace.shared.notificationCenter.addObserver(
            self,
            selector: #selector(handleSystemDidWake(_:)),
            name: NSWorkspace.didWakeNotification,
            object: nil
        )
        updateMenuState()
        updateLaunchAtLoginState()
        if !isUpdateCheckDisabledByUser() {
            checkForUpdates(interactive: false)
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        NSWorkspace.shared.notificationCenter.removeObserver(self)
        autoTriageTimer?.invalidate()
        autoTriageTimer = nil
        dataDirSource?.cancel()
        dataDirSource = nil
        configDirSource?.cancel()
        configDirSource = nil
        stopStartupIconAnimation()
        stopServerProcess(waitForExit: true)
        try? logHandle?.close()
    }

    private nonisolated static func easeInOut(_ x: CGFloat) -> CGFloat {
        let clamped = max(0.0, min(1.0, x))
        return 0.5 - 0.5 * cos(.pi * clamped)
    }

    private nonisolated static func windowProgress(_ u: CGFloat, start: CGFloat, end: CGFloat) -> CGFloat {
        if u <= start { return 0.0 }
        if u >= end { return 1.0 }
        return easeInOut((u - start) / (end - start))
    }

    private nonisolated static func strokePath(_ path: NSBezierPath, length: CGFloat, fraction: CGFloat) {
        if fraction <= 0.001 {
            return
        }
        if fraction < 0.999 {
            var pattern: [CGFloat] = [length * fraction, (length + 8.0) * 2.0]
            path.setLineDash(&pattern, count: 2, phase: 0.0)
        }
        path.stroke()
    }

    private nonisolated static func makeSnabelNollMenuIcon(
        strokeFractions: (zero: CGFloat, swirl: CGFloat, slash: CGFloat)? = nil,
        showBadgeDot: Bool = false
    ) -> NSImage {
        let fractions = strokeFractions ?? (zero: 1.0, swirl: 1.0, slash: 1.0)
        let img = NSImage(size: NSSize(width: 18, height: 18), flipped: true) { rect in
            let scale: CGFloat = (rect.height - 2.0) / 400.0
            let tx = rect.midX - 256.0 * scale
            let ty = rect.midY - 256.0 * scale
            var xform = AffineTransform(translationByX: tx, byY: ty)
            xform.scale(scale)

            NSColor.black.setStroke()

            let innerZero = NSBezierPath()
            innerZero.move(to: NSPoint(x: 319.0, y: 256.0))
            innerZero.curve(to: NSPoint(x: 255.0, y: 352.0), controlPoint1: NSPoint(x: 319.0, y: 314.6), controlPoint2: NSPoint(x: 292.1, y: 352.0))
            innerZero.curve(to: NSPoint(x: 191.0, y: 256.0), controlPoint1: NSPoint(x: 217.9, y: 352.0), controlPoint2: NSPoint(x: 191.0, y: 314.6))
            innerZero.curve(to: NSPoint(x: 255.0, y: 160.0), controlPoint1: NSPoint(x: 191.0, y: 197.4), controlPoint2: NSPoint(x: 217.9, y: 160.0))
            innerZero.curve(to: NSPoint(x: 319.0, y: 256.0), controlPoint1: NSPoint(x: 292.1, y: 160.0), controlPoint2: NSPoint(x: 319.0, y: 197.4))
            innerZero.close()
            innerZero.transform(using: xform)
            innerZero.lineWidth = 36.0 * scale
            innerZero.lineCapStyle = .butt
            innerZero.lineJoinStyle = .round
            strokePath(innerZero, length: 514.0 * scale, fraction: fractions.zero)

            let swirl = NSBezierPath()
            swirl.move(to: NSPoint(x: 319.0, y: 256.0))
            swirl.curve(to: NSPoint(x: 361.0, y: 352.0), controlPoint1: NSPoint(x: 319.0, y: 322.0), controlPoint2: NSPoint(x: 331.0, y: 352.0))
            swirl.curve(to: NSPoint(x: 407.0, y: 244.0), controlPoint1: NSPoint(x: 391.0, y: 352.0), controlPoint2: NSPoint(x: 407.0, y: 312.0))
            swirl.curve(to: NSPoint(x: 259.0, y: 74.0), controlPoint1: NSPoint(x: 407.0, y: 148.8), controlPoint2: NSPoint(x: 341.9, y: 74.0))
            swirl.curve(to: NSPoint(x: 105.0, y: 256.0), controlPoint1: NSPoint(x: 172.8, y: 74.0), controlPoint2: NSPoint(x: 105.0, y: 154.1))
            swirl.curve(to: NSPoint(x: 261.0, y: 438.0), controlPoint1: NSPoint(x: 105.0, y: 357.9), controlPoint2: NSPoint(x: 173.6, y: 438.0))
            swirl.curve(to: NSPoint(x: 343.7, y: 410.3), controlPoint1: NSPoint(x: 290.2, y: 438.0), controlPoint2: NSPoint(x: 318.9, y: 428.4))
            swirl.transform(using: xform)
            swirl.lineWidth = 36.0 * scale
            swirl.lineCapStyle = .butt
            swirl.lineJoinStyle = .round
            strokePath(swirl, length: 1116.0 * scale, fraction: fractions.swirl)

            let slash = NSBezierPath()
            slash.move(to: NSPoint(x: 298.1, y: 182.2))
            slash.line(to: NSPoint(x: 211.9, y: 329.8))
            slash.transform(using: xform)
            slash.lineWidth = 26.0 * scale
            slash.lineCapStyle = .butt
            strokePath(slash, length: 171.0 * scale, fraction: fractions.slash)

            if showBadgeDot {
                let haloRect = NSRect(x: 12.6, y: 0.4, width: 5.0, height: 5.0)
                NSGraphicsContext.current?.compositingOperation = .clear
                NSBezierPath(ovalIn: haloRect).fill()
                NSGraphicsContext.current?.compositingOperation = .sourceOver
                NSColor.black.setFill()
                let dotRect = NSRect(x: 13.4, y: 1.2, width: 3.4, height: 3.4)
                NSBezierPath(ovalIn: dotRect).fill()
            }

            return true
        }
        img.isTemplate = true
        return img
    }

    private func buildStatusMenu() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = statusItem.button {
            let icon = Self.makeSnabelNollMenuIcon()
            icon.accessibilityDescription = "Nollpost"
            button.image = icon
            button.toolTip = "Nollpost Morning Mail Triage"
        }

        let menu = NSMenu()
        stateMenuItem = NSMenuItem(title: serverState.menuSummary, action: nil, keyEquivalent: "")
        stateMenuItem.isEnabled = false
        menu.addItem(stateMenuItem)

        autoStatusMenuItem = NSMenuItem(title: "Auto-Triage: Idle", action: nil, keyEquivalent: "")
        autoStatusMenuItem.isEnabled = false
        menu.addItem(autoStatusMenuItem)

        updateNoticeMenuItem = NSMenuItem(title: "", action: #selector(showUpdatePrompt(_:)), keyEquivalent: "")
        updateNoticeMenuItem.target = self
        updateNoticeMenuItem.isHidden = true
        menu.addItem(updateNoticeMenuItem)
        menu.addItem(.separator())

        openBoardMenuItem = NSMenuItem(title: "Open Review Board", action: #selector(openReviewBoard(_:)), keyEquivalent: "o")
        openBoardMenuItem.target = self
        menu.addItem(openBoardMenuItem)

        runAutoNowMenuItem = NSMenuItem(title: "Run Auto-Triage Now", action: #selector(runAutoTriageNow(_:)), keyEquivalent: "t")
        runAutoNowMenuItem.target = self
        menu.addItem(runAutoNowMenuItem)

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

        idleModeMenuItem = NSMenuItem(title: "Auto-Stop When Idle (45m)", action: #selector(toggleIdleAutoStop(_:)), keyEquivalent: "")
        idleModeMenuItem.target = self
        menu.addItem(idleModeMenuItem)

        pauseAutoMenuItem = NSMenuItem(title: "Pause Auto-Triage", action: #selector(togglePauseAutoTriage(_:)), keyEquivalent: "")
        pauseAutoMenuItem.target = self
        menu.addItem(pauseAutoMenuItem)

        launchAtLoginMenuItem = NSMenuItem(title: "Open at Login", action: #selector(toggleLaunchAtLogin(_:)), keyEquivalent: "")
        launchAtLoginMenuItem.target = self
        menu.addItem(launchAtLoginMenuItem)

        let updateCheckItem = NSMenuItem(title: "Check for Updates...", action: #selector(checkForUpdatesManually(_:)), keyEquivalent: "u")
        updateCheckItem.target = self
        menu.addItem(updateCheckItem)

        let quitItem = NSMenuItem(title: "Quit Nollpost", action: #selector(quitApp(_:)), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)

        menu.delegate = self
        statusItem.menu = menu
    }

    func menuWillOpen(_ menu: NSMenu) {
        reloadAutoStatus()
        rescheduleAutoTimerIfNeeded()
        updateIdleAutoStopState()
        updateAutoTriageMenuState()
        updateLaunchAtLoginState()
    }

    private func isIdleAutoStopEnabled() -> Bool {
        !UserDefaults.standard.bool(forKey: idleDisabledKey)
    }

    private func configuredIdleTimeoutMinutes() -> Int {
        guard let raw = userSectionConfigValue(for: "idle_timeout_minutes"), let mins = Int(raw) else {
            return 45
        }
        return max(0, mins)
    }

    private func updateIdleAutoStopState() {
        let enabled = isIdleAutoStopEnabled()
        let mins = configuredIdleTimeoutMinutes()
        idleModeMenuItem?.isEnabled = mins > 0
        idleModeMenuItem?.state = (enabled && mins > 0) ? .on : .off
        if mins <= 0 {
            idleModeMenuItem?.title = "Auto-Stop When Idle (Off in config.toml)"
        } else {
            idleModeMenuItem?.title = "Auto-Stop When Idle (\(mins)m)"
        }
    }

    @objc private func toggleIdleAutoStop(_ sender: Any?) {
        guard configuredIdleTimeoutMinutes() > 0 else { return }
        let currentlyEnabled = isIdleAutoStopEnabled()
        UserDefaults.standard.set(currentlyEnabled, forKey: idleDisabledKey)
        updateIdleAutoStopState()
        if let proc = serverProcess, proc.isRunning {
            let newStateLabel = currentlyEnabled ? "disabled" : "enabled"
            let alert = NSAlert()
            alert.alertStyle = .informational
            alert.messageText = "Idle Auto-Stop \(newStateLabel.capitalized)"
            alert.informativeText = "The Review Board server is currently running. Restart it now to apply this change, or wait until the next start."
            alert.addButton(withTitle: "Restart Server Now")
            alert.addButton(withTitle: "Apply Next Start")
            NSApp.activate(ignoringOtherApps: true)
            if alert.runModal() == .alertFirstButtonReturn {
                stopServerProcess(waitForExit: true)
                startServer(openBrowserWhenReady: false)
            }
        }
    }

    private func updateLaunchAtLoginState() {
        let status = SMAppService.mainApp.status
        switch status {
        case .enabled:
            launchAtLoginMenuItem?.state = .on
            launchAtLoginMenuItem?.title = "Open at Login"
        case .requiresApproval:
            launchAtLoginMenuItem?.state = .on
            launchAtLoginMenuItem?.title = "Open at Login (Approval Required)"
        case .notRegistered, .notFound:
            launchAtLoginMenuItem?.state = .off
            launchAtLoginMenuItem?.title = "Open at Login"
        @unknown default:
            launchAtLoginMenuItem?.state = .off
            launchAtLoginMenuItem?.title = "Open at Login"
        }
    }

    @objc private func toggleLaunchAtLogin(_ sender: Any?) {
        let currentStatus = SMAppService.mainApp.status
        do {
            if currentStatus == .enabled || currentStatus == .requiresApproval {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
                if SMAppService.mainApp.status == .requiresApproval {
                    promptOpenLoginItemsSettings(
                        title: "Approve Nollpost in Login Items",
                        message: "macOS requires your approval in System Settings -> General -> Login Items to open Nollpost automatically at login."
                    )
                }
            }
        } catch {
            promptOpenLoginItemsSettings(
                title: "Could Not Update Login Item",
                message: "\(error.localizedDescription)\n\nIf Nollpost.app is running from Homebrew Cellar, copying it to /Applications (`ditto \"$(brew --prefix nollpost)/Nollpost.app\" /Applications/Nollpost.app`) or adding it in System Settings -> General -> Login Items is recommended."
            )
        }
        updateLaunchAtLoginState()
    }

    private func promptOpenLoginItemsSettings(title: String, message: String) {
        let alert = NSAlert()
        alert.alertStyle = .informational
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "Open Login Items Settings")
        alert.addButton(withTitle: "OK")
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn {
            SMAppService.openSystemSettingsLoginItems()
        }
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
        case .stopped, .idleStopped, .failed:
            startStopMenuItem?.title = "Start Server"
            restartMenuItem?.isEnabled = false
            openBoardMenuItem?.title = "Start & Open Review Board"
        }
        updateAutoTriageMenuState()
        updateStartupIconAnimation()
    }

    private func updateMenuBarIconAndTooltip() {
        guard serverState != .starting, let button = statusItem?.button else {
            return
        }
        let icon = Self.makeSnabelNollMenuIcon(showBadgeDot: unseenAutoCount > 0)
        icon.accessibilityDescription = unseenAutoCount > 0
            ? "Nollpost (\(unseenAutoCount) unseen auto-actions)"
            : "Nollpost"
        button.image = icon
        button.toolTip = unseenAutoCount > 0
            ? "Nollpost — \(unseenAutoCount) auto-action(s) since last review"
            : "Nollpost Morning Mail Triage"
    }

    private func updateStartupIconAnimation() {
        if serverState == .starting {
            startStartupIconAnimation()
        } else {
            stopStartupIconAnimation()
        }
    }

    private func startStartupIconAnimation() {
        if NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            return
        }
        if startupIconTimer != nil {
            return
        }
        startupAnimStart = Date()
        tickStartupIconAnimation()
        startupIconTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 30.0, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.tickStartupIconAnimation()
            }
        }
    }

    private func tickStartupIconAnimation() {
        guard serverState == .starting, let start = startupAnimStart, let button = statusItem?.button else {
            stopStartupIconAnimation()
            return
        }
        let elapsed = Date().timeIntervalSince(start)
        let cycle = elapsed / 4.86
        let phase = cycle.truncatingRemainder(dividingBy: 2.0)
        let u = CGFloat(phase <= 1.0 ? phase : (2.0 - phase))
        let zero = Self.windowProgress(u, start: 0.04, end: 0.30)
        let swirl = Self.windowProgress(u, start: 0.29, end: 0.72)
        let slash = Self.windowProgress(u, start: 0.73, end: 0.86)
        let icon = Self.makeSnabelNollMenuIcon(strokeFractions: (zero: zero, swirl: swirl, slash: slash))
        icon.accessibilityDescription = "Nollpost"
        button.image = icon
    }

    private func stopStartupIconAnimation() {
        startupIconTimer?.invalidate()
        startupIconTimer = nil
        startupAnimStart = nil
        if let button = statusItem?.button {
            let icon = Self.makeSnabelNollMenuIcon(showBadgeDot: unseenAutoCount > 0)
            icon.accessibilityDescription = "Nollpost"
            button.image = icon
        }
        updateMenuBarIconAndTooltip()
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

    private func ensureMailRunningIfNeeded() async {
        let running = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.mail")
        if running.isEmpty, let mailURL = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.mail") {
            let cfg = NSWorkspace.OpenConfiguration()
            cfg.activates = false
            _ = try? await NSWorkspace.shared.openApplication(at: mailURL, configuration: cfg)
            try? await Task.sleep(nanoseconds: 500_000_000)
        }
    }

    private func promptIfAutomationMissingBeforeLaunch() async -> Bool {
        let auto = await Self.queryMailAutomationPermission(prompt: true)
        switch auto {
        case .granted:
            return true
        case .mailNotRunning:
            await ensureMailRunningIfNeeded()
            return true
        case .denied, .consentRequired, .error:
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "Apple Mail Automation Permission Required"
            alert.informativeText = """
            Status: \(auto.label)

            Nollpost needs Automation access to control Mail.app (for Archive-only triage). Open System Settings -> Privacy & Security -> Automation and enable Mail under Nollpost.

            Note: macOS Accessibility permission is only required if you switch Gmail's strategy to "ui_menu" (the default "background" strategy needs Automation only).
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

    private func urlWithRefreshParam(_ url: URL) -> URL {
        guard var comps = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            return url
        }
        var items = comps.queryItems ?? []
        if !items.contains(where: { $0.name == "refresh" }) {
            items.append(URLQueryItem(name: "refresh", value: "1"))
        }
        comps.queryItems = items
        return comps.url ?? url
    }

    @objc private func openReviewBoard(_ sender: Any?) {
        switch serverState {
        case .running(let url):
            NSWorkspace.shared.open(urlWithRefreshParam(url))
            return
        case .starting:
            pendingBrowserOpen = true
            return
        case .stopped, .idleStopped, .failed:
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
        case .stopped, .idleStopped, .failed:
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
        idleAutoStopped = false
        pendingBrowserOpen = openBrowserWhenReady
        serverState = .starting

        var serverArgs = ["serve", "--no-open"]
        if !isIdleAutoStopEnabled() {
            serverArgs.append(contentsOf: ["--idle-timeout", "0"])
        }

        let proc = Process()
        proc.executableURL = exe
        proc.arguments = serverArgs
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
                guard let self, self.serverProcess === finished else { return }
                // Give any in-flight readabilityHandler chunk a brief moment to enqueue onto MainActor
                try? await Task.sleep(nanoseconds: 50_000_000)
                guard self.serverProcess === finished else { return }
                self.outputPipe?.fileHandleForReading.readabilityHandler = nil
                self.outputPipe = nil
                self.readyCheckTimer?.invalidate()
                self.readyCheckTimer = nil
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
                } else if self.idleAutoStopped || self.recentLogLines.contains(where: { $0.contains("Stopping Nollpost Review Board") && $0.contains("(idle mode)") }) {
                    self.idleAutoStopped = false
                    self.serverState = .idleStopped
                } else {
                    self.serverState = .stopped
                }
            }
        }

        do {
            appendLog("\n--- Launching \(exe.path) \(serverArgs.joined(separator: " ")) ---\n")
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
        if line.contains("Stopping Nollpost Review Board") && line.contains("(idle mode)") {
            idleAutoStopped = true
            return
        }
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

    private nonisolated static let tapFormulaURLString =
        "https://raw.githubusercontent.com/Hessius/homebrew-tap/main/Formula/nollpost.rb"
    private nonisolated static let upgradeCommand = "brew upgrade hessius/tap/nollpost"

    private func installedBundleVersion() -> String {
        (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "0.3.0"
    }

    private func resolvedConfigPath() -> String {
        let env = ProcessInfo.processInfo.environment
        let fm = FileManager.default
        let home = fm.homeDirectoryForCurrentUser.path
        if let raw = env["NOLLPOST_CONFIG"]?.trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty {
            return (raw as NSString).expandingTildeInPath
        }
        let nollpostCfg = "\(home)/.config/nollpost/config.toml"
        if fm.fileExists(atPath: nollpostCfg) {
            return nollpostCfg
        }
        if let rawLegacy = env["MAIL_TRIAGE_CONFIG"]?.trimmingCharacters(in: .whitespacesAndNewlines), !rawLegacy.isEmpty {
            return (rawLegacy as NSString).expandingTildeInPath
        }
        let legacyCfg = "\(home)/.config/mail-triage/config.toml"
        if fm.fileExists(atPath: legacyCfg) {
            return legacyCfg
        }
        return nollpostCfg
    }

    private func sectionConfigValue(section targetSection: String, key targetKey: String) -> String? {
        guard let text = try? String(contentsOfFile: resolvedConfigPath(), encoding: .utf8) else {
            return nil
        }
        var currentSection = ""
        var inMultiline = false
        for rawLine in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let trimmed = rawLine.trimmingCharacters(in: .whitespaces)
            if trimmed.contains("\"\"\"") {
                let parts = trimmed.components(separatedBy: "\"\"\"").count - 1
                if parts % 2 == 1 { inMultiline.toggle() }
                continue
            }
            if inMultiline || trimmed.isEmpty || trimmed.hasPrefix("#") {
                continue
            }
            if trimmed.hasPrefix("["), let closeIdx = trimmed.firstIndex(of: "]") {
                currentSection = String(trimmed[trimmed.index(after: trimmed.startIndex)..<closeIdx])
                    .trimmingCharacters(in: .whitespaces)
                continue
            }
            guard currentSection == targetSection else { continue }
            let uncommented = trimmed.split(separator: "#", maxSplits: 1, omittingEmptySubsequences: false)[0]
                .trimmingCharacters(in: .whitespaces)
            guard let eqIdx = uncommented.firstIndex(of: "=") else { continue }
            let key = String(uncommented[..<eqIdx]).trimmingCharacters(in: .whitespaces)
            if key == targetKey {
                return String(uncommented[uncommented.index(after: eqIdx)...])
                    .trimmingCharacters(in: .whitespaces)
            }
        }
        return nil
    }

    private func userSectionConfigValue(for targetKey: String) -> String? {
        sectionConfigValue(section: "user", key: targetKey)
    }

    private func resolvedDataDirURL() -> URL {
        let env = ProcessInfo.processInfo.environment
        let fm = FileManager.default
        if let raw = env["NOLLPOST_DATA_DIR"]?.trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty {
            return URL(fileURLWithPath: (raw as NSString).expandingTildeInPath, isDirectory: true)
        }
        let appSupport = fm.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support", isDirectory: true)
        let primary = appSupport.appendingPathComponent("nollpost", isDirectory: true)
        let legacy = appSupport.appendingPathComponent("mail-triage", isDirectory: true)
        if !fm.fileExists(atPath: primary.path) && fm.fileExists(atPath: legacy.path) {
            return legacy
        }
        return primary
    }

    private var autoStatusFileURL: URL {
        resolvedDataDirURL().appendingPathComponent("auto_status.json")
    }

    private func isAutoTriageEnabledInConfig() -> Bool {
        sectionConfigValue(section: "automation", key: "enabled")?.lowercased() != "false"
    }

    private func configuredAutoIntervalMinutes() -> Int {
        guard let raw = sectionConfigValue(section: "automation", key: "interval_minutes"), let mins = Int(raw) else {
            return 60
        }
        return max(1, min(1440, mins))
    }

    private func isAutoTriagePaused() -> Bool {
        UserDefaults.standard.bool(forKey: autoPausedKey)
    }

    private func startAutoStatusWatcher() {
        let dirURL = resolvedDataDirURL()
        try? FileManager.default.createDirectory(at: dirURL, withIntermediateDirectories: true)
        let fd = open(dirURL.path, O_EVTONLY)
        guard fd >= 0 else { return }
        dataDirFD = fd
        let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: .write, queue: .main)
        source.setEventHandler { [weak self] in
            Task { @MainActor [weak self] in
                self?.reloadAutoStatus()
            }
        }
        source.setCancelHandler { [fd] in
            close(fd)
        }
        dataDirSource = source
        source.resume()
    }

    private func startConfigDirWatcher() {
        let cfgDir = URL(fileURLWithPath: resolvedConfigPath()).deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: cfgDir, withIntermediateDirectories: true)
        let fd = open(cfgDir.path, O_EVTONLY)
        guard fd >= 0 else { return }
        configDirFD = fd
        let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: .write, queue: .main)
        source.setEventHandler { [weak self] in
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.updateIdleAutoStopState()
                self.rescheduleAutoTimerIfNeeded()
                self.updateAutoTriageMenuState()
            }
        }
        source.setCancelHandler { [fd] in
            close(fd)
        }
        configDirSource = source
        source.resume()
    }

    private func reloadAutoStatus() {
        if let data = try? Data(contentsOf: autoStatusFileURL),
           let snap = try? JSONDecoder().decode(AutoStatusSnapshot.self, from: data) {
            unseenAutoCount = max(0, snap.unseenCount)
            lastAutoRunAtISO = snap.lastRunAt
            lastAutoStatusText = snap.lastStatus ?? "ok"
        } else {
            unseenAutoCount = 0
        }
        updateAutoTriageMenuState()
        updateMenuBarIconAndTooltip()
    }

    private func updateAutoTriageMenuState() {
        let interval = configuredAutoIntervalMinutes()
        let paused = isAutoTriagePaused()
        let cfgEnabled = isAutoTriageEnabledInConfig()
        pauseAutoMenuItem?.state = paused ? .on : .off
        pauseAutoMenuItem?.isEnabled = cfgEnabled
        runAutoNowMenuItem?.isEnabled = !isAutoRunInFlight

        if isAutoRunInFlight {
            autoStatusMenuItem?.title = "Auto-Triage: Running..."
        } else if !cfgEnabled {
            autoStatusMenuItem?.title = "Auto-Triage: Disabled in Settings"
        } else if paused {
            autoStatusMenuItem?.title = unseenAutoCount > 0
                ? "Auto-Triage: Paused (\(unseenAutoCount) unseen)"
                : "Auto-Triage: Paused"
        } else if unseenAutoCount > 0 {
            autoStatusMenuItem?.title = "Auto-Triage: \(unseenAutoCount) new in summary (Every \(interval)m)"
        } else if lastAutoStatusText == "error" {
            autoStatusMenuItem?.title = "Auto-Triage: Last run failed (Every \(interval)m)"
        } else {
            autoStatusMenuItem?.title = "Auto-Triage: Active (Every \(interval)m)"
        }
    }

    private func scheduleAutoTriageTimer() {
        autoTriageTimer?.invalidate()
        autoTriageTimer = nil
        let intervalMins = configuredAutoIntervalMinutes()
        scheduledAutoIntervalMinutes = intervalMins
        guard isAutoTriageEnabledInConfig() && !isAutoTriagePaused() else { return }
        let secs = TimeInterval(intervalMins * 60)
        autoTriageTimer = Timer.scheduledTimer(withTimeInterval: secs, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                await self?.runAutoTriage(interactive: false)
            }
        }
    }

    private func rescheduleAutoTimerIfNeeded() {
        let currentMins = configuredAutoIntervalMinutes()
        let shouldBeRunning = isAutoTriageEnabledInConfig() && !isAutoTriagePaused()
        if (autoTriageTimer != nil) != shouldBeRunning || currentMins != scheduledAutoIntervalMinutes {
            scheduleAutoTriageTimer()
        }
    }

    @objc private func handleSystemDidWake(_ notification: Notification) {
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            guard let self else { return }
            self.reloadAutoStatus()
            guard self.isAutoTriageEnabledInConfig() && !self.isAutoTriagePaused() else { return }
            let intervalSecs = TimeInterval(self.configuredAutoIntervalMinutes() * 60)
            let due: Bool
            if let iso = self.lastAutoRunAtISO,
               let dt = ISO8601DateFormatter().date(from: iso) {
                due = Date().timeIntervalSince(dt) >= intervalSecs
            } else {
                due = true
            }
            if due {
                await self.runAutoTriage(interactive: false)
            }
        }
    }

    @objc private func togglePauseAutoTriage(_ sender: Any?) {
        let nextPaused = !isAutoTriagePaused()
        UserDefaults.standard.set(nextPaused, forKey: autoPausedKey)
        scheduleAutoTriageTimer()
        updateAutoTriageMenuState()
    }

    @objc private func runAutoTriageNow(_ sender: Any?) {
        Task { @MainActor [weak self] in
            await self?.runAutoTriage(interactive: true)
        }
    }

    private func runAutoTriage(interactive: Bool) async {
        guard !isAutoRunInFlight else { return }
        isAutoRunInFlight = true
        updateAutoTriageMenuState()
        defer {
            isAutoRunInFlight = false
            reloadAutoStatus()
            rescheduleAutoTimerIfNeeded()
        }

        if !interactive {
            guard isAutoTriageEnabledInConfig() && !isAutoTriagePaused() else { return }
            let mailRunning = !NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.mail").isEmpty
            guard mailRunning else { return }
            let autoPerm = await Self.queryMailAutomationPermission(prompt: false)
            guard case .granted = autoPerm else { return }
        } else {
            guard await promptIfAutomationMissingBeforeLaunch() else { return }
        }

        switch serverState {
        case .running(let url):
            await runAutoViaLiveServer(boardURL: url, interactive: interactive)
        case .starting:
            if interactive {
                showAlert(style: .informational, title: "Server Starting", message: "The Review Board server is starting up. Try again in a moment.")
            }
        case .stopped, .idleStopped, .failed:
            await runAutoViaCLI(interactive: interactive)
        }
    }

    private func handleInteractiveAutoReport(_ data: Data, fallbackError: String? = nil) {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            let detail = fallbackError?.trimmingCharacters(in: .whitespacesAndNewlines)
            showAlert(
                style: .warning,
                title: "Auto-Triage Failed",
                message: (detail?.isEmpty == false ? detail! : nil) ?? "Could not parse auto-triage output. Check menubar.log for details."
            )
            return
        }
        let status = (obj["status"] as? String ?? "").lowercased()
        let warnings = (obj["warnings"] as? [String] ?? []).filter { !$0.isEmpty }
        let warningSuffix = warnings.isEmpty ? "" : "\n\nWarnings:\n• " + warnings.joined(separator: "\n• ")

        if status == "error" {
            let errText = obj["error"] as? String ?? "Unknown error during auto-triage."
            showAlert(style: .warning, title: "Auto-Triage Failed", message: errText + warningSuffix)
        } else if status == "skipped" {
            let reason = obj["reason"] as? String ?? "skipped"
            let msg: String
            switch reason {
            case "mail_not_running":
                msg = "Apple Mail does not appear to be running."
            case "busy":
                msg = "Another Nollpost scan or batch run is currently in progress. Please retry in a moment."
            case "disabled":
                msg = "Auto-triage is disabled in Settings."
            default:
                msg = "Auto-triage was skipped (\(reason))."
            }
            showAlert(style: .informational, title: "Auto-Triage Skipped", message: msg + warningSuffix)
        } else if status == "ok" {
            let executed = obj["executed"] as? Int ?? 0
            let summary = obj["summary"] as? [String: Any] ?? [:]
            let archived = summary["archived"] as? Int ?? 0
            let declined = summary["declined"] as? Int ?? 0
            let failed = summary["failed"] as? Int ?? 0

            let alert = NSAlert()
            alert.alertStyle = .informational
            if executed > 0 {
                alert.messageText = "Auto-Triage Complete"
                alert.informativeText = "Archived \(archived), declined \(declined), failed \(failed).\n\nOpen the Review Board to review the summary or undo any action." + warningSuffix
            } else {
                let rules = obj["active_rules"] as? [String: Any] ?? [:]
                let senderCount = rules["sender_auto_archive"] as? Int ?? 0
                let hasAnyToggle = (rules["auto_decline_consulting"] as? Bool ?? false)
                    || (rules["auto_archive_transactional"] as? Bool ?? false)
                    || (rules["auto_archive_bot_noise"] as? Bool ?? false)
                    || (rules["auto_archive_always_archive"] as? Bool ?? false)
                    || (rules["auto_archive_recent_unsubs"] as? Bool ?? false)
                if senderCount == 0 && !hasAnyToggle {
                    alert.messageText = "Auto-Triage: No Active Rules"
                    alert.informativeText = "0 emails matched because all category autopilot toggles (such as Auto-decline Consulting Mismatches) are currently off and no senders are marked Auto (Archive & Summarize) yet.\n\nOpen the Review Board to enable them in Settings or on any email row." + warningSuffix
                } else {
                    alert.messageText = "Auto-Triage Complete"
                    alert.informativeText = "0 unread Inbox emails matched your active auto-triage rules." + warningSuffix
                }
            }
            alert.addButton(withTitle: "Open Review Board")
            alert.addButton(withTitle: "OK")
            NSApp.activate(ignoringOtherApps: true)
            if alert.runModal() == .alertFirstButtonReturn {
                openReviewBoard(nil)
            }
        } else {
            let detail = fallbackError?.trimmingCharacters(in: .whitespacesAndNewlines)
            showAlert(
                style: .warning,
                title: "Auto-Triage Failed",
                message: (detail?.isEmpty == false ? detail! : nil) ?? "Unexpected auto-triage status '\(status)'."
            )
        }
    }

    private func runAutoViaLiveServer(boardURL: URL, interactive: Bool) async {
        guard var comps = URLComponents(url: boardURL, resolvingAgainstBaseURL: false) else { return }
        let token = comps.queryItems?.first(where: { $0.name == "key" })?.value ?? ""
        comps.path = "/api/auto/run"
        comps.queryItems = [
            URLQueryItem(name: interactive ? "force" : "background", value: interactive ? "true" : "1"),
        ]
        guard let endpoint = comps.url else { return }
        var req = URLRequest(url: endpoint)
        req.httpMethod = "POST"
        req.timeoutInterval = 180.0
        if !token.isEmpty {
            req.setValue(token, forHTTPHeaderField: "X-Triage-Key")
        }
        do {
            let (data, response) = try await URLSession.shared.data(for: req)
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            let bodyStr = String(decoding: data, as: UTF8.self)
            appendLog("\n--- POST /api/auto/run (\(code)): \(bodyStr) ---\n")
            if interactive {
                if !(200..<300).contains(code) {
                    showAlert(style: .warning, title: "Auto-Triage Failed", message: "Server returned HTTP \(code).")
                } else {
                    handleInteractiveAutoReport(data, fallbackError: bodyStr)
                }
            }
        } catch {
            appendLog("\n--- POST /api/auto/run error: \(error.localizedDescription) ---\n")
            if interactive {
                showAlert(style: .warning, title: "Auto-Triage Failed", message: error.localizedDescription)
            }
        }
    }

    private func runAutoViaCLI(interactive: Bool) async {
        guard let exe = resolveNollpostExecutable() else {
            if interactive { showMissingBinaryAlert() }
            return
        }
        let env = childEnvironment(for: exe)
        let cliArgs = interactive ? ["auto", "--force", "--json"] : ["auto", "--json"]
        let result: (Int32, Data, Data) = await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let proc = Process()
                proc.executableURL = exe
                proc.arguments = cliArgs
                proc.environment = env
                let outPipe = Pipe()
                let errPipe = Pipe()
                proc.standardOutput = outPipe
                proc.standardError = errPipe
                do {
                    try proc.run()
                    var outData = Data()
                    var errData = Data()
                    let group = DispatchGroup()
                    group.enter()
                    DispatchQueue.global(qos: .utility).async {
                        outData = outPipe.fileHandleForReading.readDataToEndOfFile()
                        group.leave()
                    }
                    group.enter()
                    DispatchQueue.global(qos: .utility).async {
                        errData = errPipe.fileHandleForReading.readDataToEndOfFile()
                        group.leave()
                    }
                    group.wait()
                    proc.waitUntilExit()
                    continuation.resume(returning: (proc.terminationStatus, outData, errData))
                } catch {
                    let msg = Data(error.localizedDescription.utf8)
                    continuation.resume(returning: (-1, Data(), msg))
                }
            }
        }
        let outputText = String(decoding: result.1, as: UTF8.self)
        let stderrText = String(decoding: result.2, as: UTF8.self)
        let errSuffix = stderrText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "" : "\n[stderr] \(stderrText)"
        appendLog("\n--- nollpost \(cliArgs.joined(separator: " ")) (exit \(result.0)): \(outputText)\(errSuffix) ---\n")
        if interactive {
            let combinedFallback = [stderrText, outputText]
                .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                .filter { !$0.isEmpty }
                .joined(separator: "\n")
            if !result.1.isEmpty,
               (try? JSONSerialization.jsonObject(with: result.1) as? [String: Any]) != nil {
                handleInteractiveAutoReport(result.1, fallbackError: combinedFallback)
            } else if result.0 != 0 {
                showAlert(
                    style: .warning,
                    title: "Auto-Triage Failed",
                    message: combinedFallback.isEmpty ? "Process exited with status \(result.0)." : combinedFallback
                )
            } else {
                handleInteractiveAutoReport(result.1, fallbackError: combinedFallback)
            }
        }
    }

    private func isUpdateCheckDisabledByUser() -> Bool {
        let env = ProcessInfo.processInfo.environment
        if let flag = env["NOLLPOST_NO_UPDATE_CHECK"]?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased(),
           ["1", "true", "yes"].contains(flag) {
            return true
        }
        return userSectionConfigValue(for: "check_updates")?.lowercased() == "false"
    }

    private nonisolated static func parseFormulaVersion(_ formulaText: String) -> String? {
        let patterns = [
            #"archive/refs/tags/v([0-9]+(?:\.[0-9]+)*)\.tar\.gz"#,
            #"^\s*version\s+"v?([0-9]+(?:\.[0-9]+)*)""#,
        ]
        for pat in patterns {
            if let regex = try? NSRegularExpression(pattern: pat, options: [.anchorsMatchLines]),
               let match = regex.firstMatch(in: formulaText, range: NSRange(formulaText.startIndex..., in: formulaText)),
               match.numberOfRanges > 1,
               let range = Range(match.range(at: 1), in: formulaText) {
                return String(formulaText[range])
            }
        }
        return nil
    }

    private nonisolated static func parseVersionParts(_ raw: String) -> [Int] {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard
            let regex = try? NSRegularExpression(pattern: #"^[vV]?(\d+(?:\.\d+)*)"#),
            let match = regex.firstMatch(in: trimmed, range: NSRange(trimmed.startIndex..., in: trimmed)),
            match.numberOfRanges > 1,
            let range = Range(match.range(at: 1), in: trimmed)
        else {
            return []
        }
        return trimmed[range].split(separator: ".").compactMap { Int($0) }
    }

    private nonisolated static func isNewerVersion(_ latest: String, current: String) -> Bool {
        let l = parseVersionParts(latest)
        let c = parseVersionParts(current)
        guard !l.isEmpty && !c.isEmpty else { return false }
        let count = max(l.count, c.count)
        for idx in 0..<count {
            let lv = idx < l.count ? l[idx] : 0
            let cv = idx < c.count ? c[idx] : 0
            if lv != cv { return lv > cv }
        }
        return false
    }

    private func checkForUpdates(interactive: Bool) {
        let env = ProcessInfo.processInfo.environment
        let targetRaw = (env["NOLLPOST_UPDATE_URL"]?.trimmingCharacters(in: .whitespacesAndNewlines)).flatMap {
            $0.isEmpty ? nil : $0
        } ?? Self.tapFormulaURLString
        guard let url = URL(string: targetRaw) else {
            if interactive {
                showAlert(style: .warning, title: "Update Check Failed", message: "Invalid update URL: \(targetRaw)")
            }
            return
        }
        let current = installedBundleVersion()

        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 3.0
        cfg.timeoutIntervalForResource = 5.0
        let session = URLSession(configuration: cfg)

        var req = URLRequest(url: url)
        req.timeoutInterval = 3.0
        req.setValue("nollpost-menubar/\(current)", forHTTPHeaderField: "User-Agent")
        session.dataTask(with: req) { [weak self] data, response, error in
            session.finishTasksAndInvalidate()
            let statusCode = (response as? HTTPURLResponse)?.statusCode ?? 0
            let isHttpOk = (200..<300).contains(statusCode)
            let parsedVersion = isHttpOk
                ? data.map { String(decoding: $0, as: UTF8.self) }.flatMap { Self.parseFormulaVersion($0) }
                : nil
            let errText: String? = error?.localizedDescription ?? (!isHttpOk
                ? "HTTP \(statusCode) from tap formula"
                : (parsedVersion == nil ? "Could not parse version from Homebrew formula" : nil))
            Task { @MainActor [weak self] in
                guard let self else { return }
                if let latest = parsedVersion, Self.isNewerVersion(latest, current: current) {
                    self.latestAvailableVersion = latest
                    self.updateNoticeMenuItem?.title = "Update Available: v\(latest) (brew upgrade)..."
                    self.updateNoticeMenuItem?.isHidden = false
                    if interactive {
                        self.presentUpdateAlert(latest: latest, current: current)
                    }
                } else {
                    self.latestAvailableVersion = nil
                    self.updateNoticeMenuItem?.isHidden = true
                    if interactive {
                        if let errText {
                            self.showAlert(style: .warning, title: "Update Check Failed", message: errText)
                        } else {
                            self.showAlert(
                                style: .informational,
                                title: "Nollpost is Up to Date",
                                message: "Installed version: v\(current)"
                            )
                        }
                    }
                }
            }
        }.resume()
    }

    private func presentUpdateAlert(latest: String, current: String) {
        let alert = NSAlert()
        alert.alertStyle = .informational
        alert.messageText = "Nollpost v\(latest) Available"
        alert.informativeText = """
        You are currently running v\(current).

        Upgrade via Homebrew:
          \(Self.upgradeCommand)
        """
        alert.addButton(withTitle: "Copy Upgrade Command")
        alert.addButton(withTitle: "View Release Notes")
        alert.addButton(withTitle: "Close")
        NSApp.activate(ignoringOtherApps: true)
        let response = alert.runModal()
        if response == .alertFirstButtonReturn {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(Self.upgradeCommand, forType: .string)
        } else if response == .alertSecondButtonReturn,
                  let url = URL(string: "https://github.com/Hessius/nollpost/releases/tag/v\(latest)") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func checkForUpdatesManually(_ sender: Any?) {
        checkForUpdates(interactive: true)
    }

    @objc private func showUpdatePrompt(_ sender: Any?) {
        guard let latest = latestAvailableVersion else { return }
        presentUpdateAlert(latest: latest, current: installedBundleVersion())
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
