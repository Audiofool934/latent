import AppKit
import LatentCore
import Synchronization
import Testing
@testable import LatentApp

private final class HistoryProtocol: URLProtocol, @unchecked Sendable {
    static let paths = Mutex<[String]>([])
    static let missing = Mutex(false)
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let url = request.url!
        Self.paths.withLock { $0.append(url.path) }
        let body: String
        var status = 200
        switch url.path {
        case "/health": body = """
            {"status":"ok","service":"latent","protocol_version":1,"service_version":"fixture",
             "instance_id":"f699d3ac-607a-4c96-9bf9-14cd7206feaa","pid":42,"data_id":"history-fixture"}
            """
        case "/api/search/history": body = """
            {"entries":[
              {"id":"saved-text","kind":"text","query":"blue snow","filters":{"rating_min":4},
               "order":"variety","used_at":"2026-10-05T12:00:00.000000+00:00","reusable":true},
              {"id":"saved-image","kind":"image","query":"green","image_name":"reference.jpg","filters":{},
               "order":"closest","used_at":"2026-10-05T12:00:00.000000+00:00","reusable":true}
            ]}
            """
        case "/api/search/history/saved-image/image": body = "reference bytes"
        case _ where url.path.hasSuffix("/replay"):
            if Self.missing.withLock({ $0 }) {
                status = 404; body = "{\"error\":\"Saved search is no longer available\"}"
            } else {
                body = "{\"total\":0,\"results\":[],\"history_id\":\"\(url.path.contains("saved-image") ? "saved-image" : "saved-text")\",\"query_cached\":true}"
            }
        default: body = "{\"total\":0,\"results\":[],\"history_id\":\"saved-text\",\"query_cached\":false}"
        }
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: url, statusCode: status,
            httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@MainActor @Suite(.serialized)
struct SearchHistoryTests {
    @Test func replayAndFilterOrOrderChangesUseOnlyCachedQueries() async throws {
        let model = try await fixture()
        defer { model.shutdownService() }
        model.navigate(to: .search(query: "blue snow", order: .closest))
        try await settled(model)
        #expect(!model.searchQueryWasCached)
        model.setSearchOrder(.variety)
        try await settled(model)
        model.applySearchFilters(PhotoFilters(ratingMin: 4))
        try await settled(model)
        #expect(model.searchQueryWasCached)
        await model.refreshSearchHistory()
        model.replaySearch(model.searchHistory[0])
        try await settled(model)
        #expect(model.searchText == "blue snow" && model.searchFilters.ratingMin == 4)
        #expect(model.searchOrder == .variety)
        model.replaySearch(model.searchHistory[1])
        try await settled(model)
        #expect(model.referenceImage?.name == "reference.jpg")
        #expect(model.searchText == "green" && model.searchFilters.isEmpty)
        #expect(model.referenceImage?.jpeg == Data("reference bytes".utf8))
        let paths = HistoryProtocol.paths.withLock { $0 }
        #expect(paths.filter { $0 == "/api/search" }.count == 1)
        #expect(!paths.contains("/api/search/image"))
        #expect(paths.filter { $0.hasSuffix("/replay") }.count == 4)
    }

    @Test func aRemovedSearchNeverFallsBackToAPaidRequest() async throws {
        let model = try await fixture()
        defer { model.shutdownService() }
        await model.refreshSearchHistory()
        HistoryProtocol.missing.withLock { $0 = true }
        model.replaySearch(model.searchHistory[0])
        try await settled(model)
        #expect(model.errorMessage != nil)
        #expect(!HistoryProtocol.paths.withLock { $0.contains("/api/search") || $0.contains("/api/search/image") })
    }

    @Test func infoVisibilitySurvivesReopeningTheLibrary() async throws {
        let model = try await fixture()
        defer { model.shutdownService() }
        model.showingPhotoInfo = true
        let restored = LibraryModel(client: model.client, preferences: model.preferences)
        defer { restored.shutdownService() }
        #expect(restored.showingPhotoInfo)
    }

    @Test func infoPositionUsesTheChosenDisplayAndRecoversWhenItDisconnects() {
        let main = NSRect(x: 0, y: 0, width: 1512, height: 900)
        let secondary = NSRect(x: -1920, y: 0, width: 1920, height: 1080)
        let chosen = NSRect(x: -1600, y: 400, width: 312, height: 358)
        #expect(PhotoInfoPanel.Coordinator.visibleFrame(chosen, screens: [main, secondary], fallback: main) == chosen)
        let restored = PhotoInfoPanel.Coordinator.visibleFrame(chosen, screens: [main], fallback: main)
        #expect(main.contains(restored))
        let edge = NSRect(x: 1400, y: 800, width: 312, height: 358)
        #expect(main.contains(PhotoInfoPanel.Coordinator.visibleFrame(edge, screens: [main], fallback: main)))
    }

    @Test func movedInfoPositionIsSavedAndRestoredByTheNativeWindow() async throws {
        let model = try await fixture()
        defer { model.shutdownService() }
        let owner = NSWindow(contentRect: NSRect(x: 180, y: 180, width: 900, height: 620),
            styleMask: [.titled], backing: .buffered, defer: false)
        owner.isReleasedWhenClosed = false
        let anchor = PhotoInfoPanel.AnchorView(frame: NSRect(x: 0, y: 0, width: 900, height: 620))
        owner.contentView = anchor
        owner.orderFront(nil)
        defer { owner.close() }
        let coordinator = PhotoInfoPanel.Coordinator(model: model)
        coordinator.requested = true
        coordinator.attach(anchor)
        defer { coordinator.close() }
        let panel = try #require(owner.childWindows?.first as? PhotoInfoPanel.Panel)
        let screen = try #require(owner.screen?.visibleFrame)
        let chosen = NSRect(x: screen.minX + 40, y: screen.minY + 40, width: 312, height: 358)
        panel.setFrame(chosen, display: true)
        try await Task.sleep(for: .milliseconds(20))
        let saved = try #require(model.preferences.string(forKey: "photoInfoPanelFrame"))
        #expect(NSRectFromString(saved) == chosen)
        coordinator.close()

        let reopened = PhotoInfoPanel.Coordinator(model: model)
        reopened.requested = true
        reopened.attach(anchor)
        defer { reopened.close() }
        let restored = try #require(owner.childWindows?.first as? PhotoInfoPanel.Panel)
        #expect(restored.frame == chosen)
        reopened.resetIfNeeded(UUID())
        #expect(model.preferences.string(forKey: "photoInfoPanelFrame") == nil)
        #expect(restored.frame != chosen)
    }

    @Test func draggingTheInspectorMovesItsWindowWithoutTakingKeyboardFocus() throws {
        let panel = PhotoInfoPanel.Panel(contentRect: NSRect(x: 600, y: 300, width: 312, height: 358),
            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.isReleasedWhenClosed = false
        defer { panel.close() }
        let handle = PhotoInfoDragHandle.DragView(frame: NSRect(x: 0, y: 330, width: 280, height: 28))
        let content = NSView(frame: NSRect(x: 0, y: 0, width: 312, height: 358))
        content.addSubview(handle)
        panel.contentView = content
        let down = try #require(NSEvent.mouseEvent(with: .leftMouseDown, location: NSPoint(x: 100, y: 340),
            modifierFlags: [], timestamp: 1, windowNumber: panel.windowNumber, context: nil,
            eventNumber: 1, clickCount: 1, pressure: 1))
        handle.mouseDown(with: down)
        let drag = try #require(NSEvent.mouseEvent(with: .leftMouseDragged, location: NSPoint(x: -650, y: 540),
            modifierFlags: [], timestamp: 2, windowNumber: panel.windowNumber, context: nil,
            eventNumber: 2, clickCount: 1, pressure: 1))
        handle.mouseDragged(with: drag)
        #expect(panel.frame.origin == NSPoint(x: -150, y: 500))
        #expect(panel.frame.size == NSSize(width: 312, height: 358))
        #expect(!panel.canBecomeKey && !panel.canBecomeMain)
        #expect(handle.acceptsFirstMouse(for: down))
    }

    private func fixture() async throws -> LibraryModel {
        HistoryProtocol.paths.withLock { $0 = [] }
        HistoryProtocol.missing.withLock { $0 = false }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [HistoryProtocol.self]
        let client = try LibraryClient(baseURL: URL(string: "http://localhost:8766")!, session: URLSession(configuration: configuration))
        let model = LibraryModel(client: client, preferences: MemoryPreferences())
        try await model.service.connect()
        return model
    }

    private func settled(_ model: LibraryModel) async throws {
        let deadline = ContinuousClock.now.advanced(by: .seconds(2))
        while model.isPreparingReference || model.isLoading {
            guard ContinuousClock.now < deadline else { throw ServiceConnectionError("Search history did not settle") }
            try await Task.sleep(for: .milliseconds(2))
        }
    }
}
