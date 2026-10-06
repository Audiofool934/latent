import AppKit
import CryptoKit
import LatentCore
import SwiftUI

@main
struct LatentApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @State private var model: LibraryModel

    init() {
        let process = ProcessInfo.processInfo
        let endpoint = process.argument(after: "--server-url")
            ?? process.environment["LATENT_SERVER_URL"] ?? "http://127.0.0.1:8766"
        let preferences = process.argument(after: "--settings-suite").flatMap(UserDefaults.init(suiteName:)) ?? .standard
        let client: LibraryClient
        let launchConfiguration: ServiceLaunchConfiguration?
        do {
            guard let url = URL(string: endpoint) else { throw LibraryError.invalidServer }
            client = try LibraryClient(baseURL: url)
            launchConfiguration = try ServiceLaunchConfiguration.from(arguments: process.arguments,
                environment: process.environment,
                bundledConfigurationURL: Bundle.main.url(forResource: "LocalService", withExtension: "json"))
        } catch {
            // A launch configuration error should be visible rather than connecting to a different service.
            FileHandle.standardError.write(Data("Latent: \(error.localizedDescription)\n".utf8))
            exit(2)
        }
        let draftURL: URL
        if let path = process.argument(after: "--annotation-drafts") {
            guard path.hasPrefix("/") else {
                FileHandle.standardError.write(Data("Latent: --annotation-drafts requires an absolute path\n".utf8))
                exit(2)
            }
            draftURL = URL(fileURLWithPath: path)
        } else {
            let namespace = process.argument(after: "--settings-suite") ?? "standard"
            let key = SHA256.hash(data: Data(namespace.utf8)).map { String(format: "%02x", $0) }.joined()
            draftURL = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("Latent/pending-edits/\(key).json")
        }
        _model = State(initialValue: LibraryModel(client: client, preferences: preferences,
            service: ServiceConnection(client: client, configuration: launchConfiguration),
            annotationStore: FileAnnotationDraftStore(url: draftURL)))
    }

    var body: some Scene {
        Window("Latent", id: "library") {
            LibraryView(model: model)
                .tint(Color(nsColor: GalleryAppearance.accent))
                .frame(minWidth: 960, minHeight: 640)
                .task {
                    delegate.stopService = { model.shutdownService() }
                    delegate.hasUndurableEdits = { model.annotationQueue.hasUndurableEdits }
                    await model.start()
                }
        }
        .defaultSize(width: 1440, height: 1000)
        .windowResizability(.contentMinSize)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandGroup(after: .textEditing) {
                Button("Search Library") { model.focusSearchRequest = UUID() }
                    .keyboardShortcut("f", modifiers: .command)
            }
            CommandGroup(after: .toolbar) {
                Button("Import photos…") { model.openImports() }
                    .keyboardShortcut("i", modifiers: [.command, .shift])
                Button("AI search embeddings…") { model.openImports(tab: "embeddings") }
                Divider()
                Button("Locations…") { model.showingLocations = true }
                    .keyboardShortcut(",", modifiers: .command)
                Button("Time-lapse organizer…") { model.showingTimelapses = true }
                Divider()
                Button("Refresh Library") { model.refresh() }
                    .keyboardShortcut("r", modifiers: .command)
                Divider()
                Button("Larger Thumbnails") { model.resizeThumbnails(by: 30) }
                    .keyboardShortcut("+", modifiers: .command)
                Button("Smaller Thumbnails") { model.resizeThumbnails(by: -30) }
                    .keyboardShortcut("-", modifiers: .command)
                Divider()
                Button("Open Preview") { model.previewSelection() }
                    .disabled(model.selectedPhoto == nil)
                Button(model.showingPhotoInfo ? "Hide Photo Info" : "Show Photo Info") { model.showingPhotoInfo.toggle() }
                    .keyboardShortcut("i", modifiers: .command)
                    .disabled(model.selectedPhoto == nil || model.showingSequences || model.showingEditing)
                Button("Find Similar Photos") { model.findSimilar() }
                    .disabled(model.selectedPhoto == nil)
            }
        }
    }
}

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate {
    var stopService: (() -> Void)?
    var hasUndurableEdits: (() -> Bool)?

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard hasUndurableEdits?() == true else { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = "Some edits could not be kept on this Mac."
        alert.informativeText = "Keep Latent open and retry saving. Quitting now will lose edits that are only in memory."
        alert.addButton(withTitle: "Keep Latent Open")
        alert.addButton(withTitle: "Quit and Lose Unsaved Edits")
        return alert.runModal() == .alertSecondButtonReturn ? .terminateNow : .terminateCancel
    }

    func applicationWillTerminate(_ notification: Notification) { stopService?() }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Refresh pinned Dock tiles after an in-place development build.
        if let iconURL = Bundle.main.url(forResource: "AppIcon", withExtension: "icns"),
            let icon = NSImage(contentsOf: iconURL) {
            NSApplication.shared.applicationIconImage = icon
        }
        NSApplication.shared.setActivationPolicy(.regular)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}
