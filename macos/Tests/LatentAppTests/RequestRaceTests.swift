import Foundation
import LatentCore
import Synchronization
import Testing
@testable import LatentApp

private struct CancellableWork: @unchecked Sendable { let item: DispatchWorkItem }

private final class DelayedLibraryProtocol: URLProtocol, @unchecked Sendable {
    private let work = Mutex<CancellableWork?>(nil)
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let url = request.url!
        if url.path == "/health" {
            let body = """
            {"status":"ok","service":"latent","protocol_version":1,"service_version":"0.1.0",
             "instance_id":"f699d3ac-607a-4c96-9bf9-14cd7206feaa","pid":42,"data_id":"fixture"}
            """
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(body.utf8))
            client?.urlProtocolDidFinishLoading(self)
            return
        }
        let query = URLComponents(url: url, resolvingAgainstBaseURL: false)!.queryItems ?? []
        let date = query.first(where: { $0.name == "date" })?.value
        let isNextPage = query.first(where: { $0.name == "offset" })?.value == "250"
        let id = isNextPage ? 99 : date == "2025-01-01" ? 1 : 2
        let more = id == 1
        let body = """
        {"total":\(more ? 500 : 1),"has_more":\(more),"next_offset":\(more ? "250" : "null"),"assets":[
          {"id":\(id),"name":"\(id).ARW","remote_path":"/\(id).ARW","size_bytes":4000,
           "capture_at":"2025:01:01 12:00:00","camera_model":null,"lens_model":null,
           "preview_width":1616,"preview_height":1080,"contact_url":"/media/c.jpg",
           "preview_url":"/media/p.jpg","preview_available":true}
        ]}
        """
        let item = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.client?.urlProtocol(self, didReceive: HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!,
                                     cacheStoragePolicy: .notAllowed)
            self.client?.urlProtocol(self, didLoad: Data(body.utf8))
            self.client?.urlProtocolDidFinishLoading(self)
        }
        work.withLock { $0 = CancellableWork(item: item) }
        DispatchQueue.global().asyncAfter(deadline: .now() + (isNextPage ? 0.2 : 0.025), execute: item)
    }
    override func stopLoading() { work.withLock { $0?.item.cancel() } }
}

@MainActor @Suite(.serialized)
struct RequestRaceTests {
    @Test func aCancelledPageCannotContaminateAnotherDate() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [DelayedLibraryProtocol.self]
        let client = try LibraryClient(baseURL: URL(string: "http://localhost:8766")!, session: URLSession(configuration: configuration))
        let model = LibraryModel(client: client, preferences: MemoryPreferences())
        try await model.service.connect()
        defer { model.shutdownService() }
        model.navigate(to: .library(date: "2025-01-01"))
        try await Task.sleep(for: .milliseconds(80))
        #expect(model.photos.map(\.id) == [1])
        model.loadMore()
        try await Task.sleep(for: .milliseconds(20))
        model.navigate(to: .library(date: "2025-01-02"))
        try await Task.sleep(for: .milliseconds(300))
        #expect(model.photos.map(\.id) == [2])
        #expect(model.selectedID == 2)
        #expect(model.nextOffset == nil)
        #expect(!model.isLoading && !model.isPaging)
    }

    @Test func rapidNavigationOnlyShowsTheLatestDate() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [DelayedLibraryProtocol.self]
        let client = try LibraryClient(baseURL: URL(string: "http://localhost:8766")!, session: URLSession(configuration: configuration))
        let model = LibraryModel(client: client, preferences: MemoryPreferences())
        try await model.service.connect()
        defer { model.shutdownService() }
        model.navigate(to: .library(date: "2025-01-01"))
        model.navigate(to: .library(date: "2025-01-02"))
        try await Task.sleep(for: .milliseconds(120))
        #expect(model.source == .library(date: "2025-01-02"))
        #expect(model.photos.map(\.id) == [2])
        #expect(model.errorMessage == nil)
    }
}
