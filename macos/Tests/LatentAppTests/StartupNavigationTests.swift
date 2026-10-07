import Foundation
import LatentCore
import Synchronization
import Testing
@testable import LatentApp

private struct StartupNavigationWork: @unchecked Sendable { let item: DispatchWorkItem }

private final class StartupNavigationProtocol: URLProtocol, @unchecked Sendable {
    static let requested = Mutex<[String]>([])
    static let pendingSummary = Mutex<[StartupNavigationWork]>([])
    static let summaryStatus = Mutex(200)
    static let sequenceList = Mutex("{\"sequences\":[]}")
    private let work = Mutex<StartupNavigationWork?>(nil)
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let path = request.url!.path
        let status = path == "/api/library" ? Self.summaryStatus.withLock { $0 } : 200
        let body: String
        switch path {
        case "/health": body = """
            {"status":"ok","service":"latent","protocol_version":1,"service_version":"fixture",
             "instance_id":"f699d3ac-607a-4c96-9bf9-14cd7206feaa","pid":42,"data_id":"fixture"}
            """
        case "/api/library": body = """
            {"dates":[{"capture_date":"2026-01-01","asset_count":1}],"cached_assets":1,
             "workspace":{"writable":true}}
            """
        case "/api/sequences": body = Self.sequenceList.withLock { $0 }
        case "/api/search": body = "{\"results\":[],\"total\":0}"
        default: body = "{\"assets\":[],\"total\":0,\"has_more\":false,\"next_offset\":null}"
        }
        Self.requested.withLock { $0.append(path) }
        let item = DispatchWorkItem { [weak self] in
            guard let self else { return }
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status,
                                                                httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(body.utf8))
            client?.urlProtocolDidFinishLoading(self)
        }
        work.withLock { $0 = StartupNavigationWork(item: item) }
        if path == "/api/library" {
            Self.pendingSummary.withLock { $0.append(StartupNavigationWork(item: item)) }
        } else {
            DispatchQueue.global().async(execute: item)
        }
    }

    override func stopLoading() { work.withLock { $0?.item.cancel() } }

    static func releaseSummary() {
        let responses = pendingSummary.withLock { pending in
            let responses = pending
            pending.removeAll()
            return responses
        }
        for response in responses where !response.item.isCancelled {
            DispatchQueue.global().async(execute: response.item)
        }
    }
}

@MainActor @Suite(.serialized)
struct StartupNavigationTests {
    private func makeModel() -> LibraryModel {
        StartupNavigationProtocol.requested.withLock { $0 = [] }
        StartupNavigationProtocol.summaryStatus.withLock { $0 = 200 }
        StartupNavigationProtocol.sequenceList.withLock { $0 = "{\"sequences\":[]}" }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StartupNavigationProtocol.self]
        let client = try! LibraryClient(baseURL: URL(string: "http://localhost:8766")!, session: URLSession(configuration: configuration))
        return LibraryModel(client: client, preferences: MemoryPreferences())
    }

    private func waitUntil(_ predicate: () -> Bool) async throws {
        let deadline = ContinuousClock.now.advanced(by: .seconds(2))
        while !predicate() {
            guard ContinuousClock.now < deadline else { throw ServiceConnectionError("Summary fixture was not requested") }
            try await Task.sleep(for: .milliseconds(1))
        }
    }

    @Test(arguments: [GallerySource.starred, .library(date: nil), .search(query: "quiet winter", order: .closest)])
    func startupPreservesNavigationChosenWhileSummaryLoads(_ destination: GallerySource) async throws {
        let model = makeModel()
        defer {
            model.shutdownService()
            StartupNavigationProtocol.releaseSummary()
        }
        async let startup: Void = model.start()
        try await waitUntil { StartupNavigationProtocol.pendingSummary.withLock { !$0.isEmpty } }
        model.navigate(to: destination)
        try await waitUntil { !model.isLoading }
        StartupNavigationProtocol.releaseSummary()
        await startup
        #expect(model.summary != nil)
        #expect(model.source == destination)
        #expect(!model.showingSequences)
        #expect(StartupNavigationProtocol.requested.withLock { $0.filter { $0 == "/api/assets" }.count }
                == (destination == .library(date: nil) ? 1 : 0))
    }

    @Test func startupDoesNotDismissTheSequenceOverviewChosenDuringLoading() async throws {
        let model = makeModel()
        defer {
            model.shutdownService()
            StartupNavigationProtocol.releaseSummary()
        }
        async let startup: Void = model.start()
        try await waitUntil { StartupNavigationProtocol.pendingSummary.withLock { !$0.isEmpty } }
        model.showSequences()
        StartupNavigationProtocol.releaseSummary()
        await startup
        #expect(model.summary != nil)
        #expect(model.showingSequences)
        #expect(!StartupNavigationProtocol.requested.withLock { $0.contains("/api/assets") })
    }

    @Test func metadataFailureAfterNavigationRemainsVisibleAndRetryKeepsThatSource() async throws {
        let model = makeModel()
        defer {
            model.shutdownService()
            StartupNavigationProtocol.releaseSummary()
        }
        StartupNavigationProtocol.summaryStatus.withLock { $0 = 500 }
        async let startup: Void = model.start()
        try await waitUntil { StartupNavigationProtocol.pendingSummary.withLock { !$0.isEmpty } }
        model.navigate(to: .starred)
        try await waitUntil { !model.isLoading }
        StartupNavigationProtocol.releaseSummary()
        await startup
        #expect(model.summary == nil && model.service.isConnected)
        #expect(model.source == .starred && model.errorMessage != nil)

        StartupNavigationProtocol.summaryStatus.withLock { $0 = 200 }
        async let retry: Void = model.start()
        try await waitUntil { StartupNavigationProtocol.pendingSummary.withLock { !$0.isEmpty } }
        StartupNavigationProtocol.releaseSummary()
        await retry
        try await waitUntil { !model.isLoading }
        #expect(model.summary != nil && model.errorMessage == nil)
        #expect(model.source == .starred)
    }

    @Test func startupCannotReplaceANewerSequenceList() async throws {
        let model = makeModel()
        defer {
            model.shutdownService()
            StartupNavigationProtocol.releaseSummary()
        }
        async let startup: Void = model.start()
        try await waitUntil {
            StartupNavigationProtocol.pendingSummary.withLock { !$0.isEmpty }
                && StartupNavigationProtocol.requested.withLock { $0.contains("/api/sequences") }
        }
        StartupNavigationProtocol.sequenceList.withLock { $0 = """
            {"sequences":[{"id":"new-sequence","name":"Winter notes","note":"","item_count":0}]}
            """ }
        model.showSequences()
        try await waitUntil { model.sequences.first?.id == "new-sequence" }
        StartupNavigationProtocol.releaseSummary()
        await startup
        #expect(model.showingSequences)
        #expect(model.sequences.map(\.id) == ["new-sequence"])
    }
}
