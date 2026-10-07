import Foundation
import LatentCore
import Synchronization
import Testing
@testable import LatentApp

private struct StartupScenario: Sendable {
    var kind = "ready"
    var requests: [String] = []
}

private final class StartupProtocol: URLProtocol, @unchecked Sendable {
    static let scenario = Mutex(StartupScenario())
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let url = request.url!
        let kind = Self.scenario.withLock { value in
            value.requests.append("\(request.httpMethod ?? "GET") \(url.path)")
            return value.kind
        }
        let body: String
        switch url.path {
        case "/health":
            if kind == "legacy" { body = "{\"status\":\"ok\",\"cloud_access\":false}" }
            else {
                body = """
                {"status":"ok","service":"\(kind == "wrong-app" ? "other" : "latent")",
                 "protocol_version":\(kind == "wrong-version" ? 2 : 1),"service_version":"0.1.0",
                 "instance_id":"f699d3ac-607a-4c96-9bf9-14cd7206feaa","pid":42,"data_id":"fixture"}
                """
            }
        case "/api/library": body = "{\"dates\":[],\"cached_assets\":0,\"workspace\":{\"writable\":true}}"
        case "/api/sequences": body = "{\"sequences\":[]}"
        default: body = "{\"assets\":[],\"total\":0,\"has_more\":false,\"next_offset\":null}"
        }
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@MainActor @Suite(.serialized)
struct ServiceStartupTests {
    private func makeModel(kind: String) throws -> LibraryModel {
        StartupProtocol.scenario.withLock { $0 = StartupScenario(kind: kind) }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StartupProtocol.self]
        let client = try LibraryClient(baseURL: URL(string: "http://localhost:8766")!, session: URLSession(configuration: configuration))
        return LibraryModel(client: client, preferences: MemoryPreferences())
    }

    @Test func concurrentStartupLoadsTheLibraryOnce() async throws {
        let model = try makeModel(kind: "ready")
        defer { model.shutdownService() }
        async let first: Void = model.start()
        async let second: Void = model.start()
        _ = await (first, second)
        try await Task.sleep(for: .milliseconds(30))
        let requests = StartupProtocol.scenario.withLock { $0.requests }
        #expect(requests.filter { $0 == "GET /health" }.count == 1)
        #expect(requests.filter { $0 == "GET /api/library" }.count == 1)
        #expect(model.summary != nil)
        #expect(model.service.isConnected)
        #expect(model.errorMessage == nil)
    }

    @Test func wrongServiceLegacyAndVersionMismatchStopBeforeAnyLibraryRequest() async throws {
        for kind in ["wrong-app", "legacy", "wrong-version"] {
            let model = try makeModel(kind: kind)
            await model.start()
            #expect(model.summary == nil)
            #expect(!model.service.isConnected && !model.service.isConnecting)
            #expect(StartupProtocol.scenario.withLock { $0.requests } == ["GET /health"])
            #expect(!model.isLoading)
            model.shutdownService()
        }
    }

    @Test func disconnectedKeyboardAndRetainedSheetActionsIssueNoRequests() async throws {
        let model = try makeModel(kind: "wrong-version")
        defer { model.shutdownService() }
        await model.start()
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let photo = try decoder.decode(Photo.self, from: Data("""
        {"id":1,"name":"fixture.ARW","remote_path":"/fixture.ARW","size_bytes":1024,
         "capture_at":null,"camera_model":null,"lens_model":null,"preview_width":1,"preview_height":1,
         "contact_url":"/media/fixture.jpg","preview_url":"/media/fixture.jpg","preview_available":true}
        """.utf8))
        let sequence = try decoder.decode(PhotoSequence.self, from: Data("""
        {"id":"fixture","name":"Fixture","note":"","item_count":0,"items":[]}
        """.utf8))
        model.photos = [photo]
        model.selectedID = photo.id
        model.nextOffset = 250
        model.rateSelection(5)
        model.annotate(ids: [photo.id], caption: "Must stay local")
        model.beginEditing()
        model.addSelected(to: sequence)
        #expect(await !model.saveSequence(name: "No write", note: "", editing: sequence, adding: photo))
        model.loadMore()
        model.showSequences()
        model.previewSelection()
        try await Task.sleep(for: .milliseconds(40))
        #expect(StartupProtocol.scenario.withLock { $0.requests } == ["GET /health"])
        #expect(!model.isSaving && !model.editingBusy && !model.isPaging)
        #expect(model.previewPhoto == nil)
    }

    @Test func retryAfterServiceCorrectionLoadsTheLibrary() async throws {
        let model = try makeModel(kind: "wrong-version")
        defer { model.shutdownService() }
        await model.start()
        #expect(!model.service.isConnected)
        StartupProtocol.scenario.withLock { $0.kind = "ready" }
        await model.start()
        #expect(model.service.isConnected)
        #expect(model.summary != nil)
        #expect(model.errorMessage == nil)
    }
}
