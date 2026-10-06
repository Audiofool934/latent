import Foundation
import LatentCore
import Synchronization
import Testing
@testable import LatentApp

private struct StarredFixture: Sendable {
    struct Asset: Sendable {
        let id: Int
        var rating = 5
        var caption = ""
        var updated: Int

        var json: [String: Any] {
            ["id": id, "name": "\(id).ARW", "remote_path": "/fixture/\(id).ARW",
             "provider": "fixture", "fingerprint": "fingerprint-\(id)",
             "size_bytes": 1024, "capture_at": "2026:01:01 12:00:00",
             "contact_url": "/media/\(id)/contact.jpg", "preview_url": "/media/\(id)/preview.jpg",
             "preview_available": false, "rating": rating, "caption": caption]
        }
    }

    var assets = (1...251).map { Asset(id: $0, updated: 300 - $0) }
    var clock = 1_000
    var pageOffsets: [Int] = []
    var delayPages = false
    var cancelledPages = 0
    var heldPages: [StarredWork] = []

    mutating func response(for request: URLRequest) -> Data {
        let object: [String: Any]
        switch request.url!.path {
        case "/health":
            object = ["status": "ok", "service": "latent", "protocol_version": 1,
                      "service_version": "fixture", "instance_id": "f699d3ac-607a-4c96-9bf9-14cd7206feaa",
                      "pid": 42, "data_id": "fixture"]
        case "/api/annotations":
            let body = request.httpBody ?? {
                let stream = request.httpBodyStream!
                stream.open()
                defer { stream.close() }
                var bytes = [UInt8](repeating: 0, count: 1_024)
                var data = Data()
                while stream.hasBytesAvailable {
                    let count = stream.read(&bytes, maxLength: bytes.count)
                    if count <= 0 { break }
                    data.append(contentsOf: bytes.prefix(count))
                }
                return data
            }()
            let patch = try! JSONSerialization.jsonObject(with: body) as! [String: Any]
            let ids = Set(patch["asset_ids"] as! [Int])
            clock += 1
            for index in assets.indices where ids.contains(assets[index].id) {
                if let rating = patch["rating"] as? Int { assets[index].rating = rating }
                if let caption = patch["caption"] as? String { assets[index].caption = caption }
                assets[index].updated = clock
            }
            object = ["assets": assets.filter { ids.contains($0.id) }.map(\.json)]
        case "/api/starred":
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems ?? []
            let offset = Int(query.first { $0.name == "offset" }?.value ?? "0")!
            pageOffsets.append(offset)
            let ordered = assets.filter { $0.rating > 0 }.sorted {
                if $0.rating != $1.rating { return $0.rating > $1.rating }
                if $0.updated != $1.updated { return $0.updated > $1.updated }
                return $0.id < $1.id
            }
            let page = Array(ordered.dropFirst(offset).prefix(250))
            let next = offset + page.count
            object = ["assets": page.map(\.json), "total": ordered.count,
                      "has_more": next < ordered.count,
                      "next_offset": next < ordered.count ? next : NSNull()]
        default:
            object = ["assets": [], "total": 0, "has_more": false, "next_offset": NSNull()]
        }
        return try! JSONSerialization.data(withJSONObject: object)
    }
}

private struct StarredWork: @unchecked Sendable { let item: DispatchWorkItem }

private final class StarredProtocol: URLProtocol, @unchecked Sendable {
    static let fixture = Mutex(StarredFixture())
    private let work = Mutex<StarredWork?>(nil)
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let (data, delayed) = Self.fixture.withLock { fixture in
            (fixture.response(for: request), fixture.delayPages && request.url!.query?.contains("offset=250") == true)
        }
        let item = DispatchWorkItem { [weak self] in
            guard let self else { return }
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 200,
                                                                httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        }
        work.withLock { $0 = StarredWork(item: item) }
        if delayed {
            Self.fixture.withLock { $0.heldPages.append(StarredWork(item: item)) }
        } else {
            DispatchQueue.global().async(execute: item)
        }
    }

    override func stopLoading() {
        work.withLock { $0?.item.cancel() }
        if request.url!.query?.contains("offset=250") == true {
            Self.fixture.withLock { $0.cancelledPages += 1 }
        }
    }

    static func releasePages() {
        let held = fixture.withLock { fixture in
            let held = fixture.heldPages
            fixture.heldPages.removeAll()
            return held
        }
        for response in held where !response.item.isCancelled {
            DispatchQueue.global().async(execute: response.item)
        }
    }
}

@MainActor @Suite(.serialized)
struct StarredMutationTests {
    private func model() async throws -> LibraryModel {
        StarredProtocol.fixture.withLock { $0 = StarredFixture() }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StarredProtocol.self]
        let client = try LibraryClient(baseURL: URL(string: "http://localhost:8766")!, session: URLSession(configuration: configuration))
        let model = LibraryModel(client: client, preferences: UserDefaults(suiteName: "LatentStarredTests.\(UUID().uuidString)")!)
        do {
            try await model.service.connect()
            model.navigate(to: .starred)
            try await settle(model)
            try #require(model.photos.count == 250 && model.nextOffset == 250)
            return model
        } catch {
            model.shutdownService()
            StarredProtocol.releasePages()
            throw error
        }
    }

    private func settle(_ model: LibraryModel) async throws {
        try await waitUntil { !model.isLoading && !model.isPaging && !model.isSaving }
    }

    private func waitUntil(_ predicate: () -> Bool) async throws {
        let deadline = ContinuousClock.now.advanced(by: .seconds(2))
        while !predicate() {
            guard ContinuousClock.now < deadline else { throw ServiceConnectionError("Fixture did not settle") }
            try await Task.sleep(for: .milliseconds(2))
        }
    }

    @Test func clearingAStarRefreshesMembershipAndDoesNotSkipTheLastPhoto() async throws {
        let model = try await model()
        defer { model.shutdownService() }
        model.setSelection([1, 2], primary: 1)
        model.annotate(ids: [1], rating: 0)
        try await settle(model)
        #expect(model.photos.map(\.id) == Array(2...251))
        #expect(model.total == 250 && model.nextOffset == nil)
        #expect(model.selectedIDs == [2] && model.selectedID == 2)
    }

    @Test func loweringARatingRefreshesOrderBeforeTheNextPage() async throws {
        let model = try await model()
        defer { model.shutdownService() }
        model.annotate(ids: [1], rating: 1)
        try await settle(model)
        #expect(model.photos.map(\.id) == Array(2...251))
        model.loadMore()
        try await settle(model)
        #expect(model.photos.map(\.id) == Array(2...251) + [1])
        #expect(Set(model.photos.map(\.id)).count == 251)
        #expect(model.total == 251 && model.nextOffset == nil)
    }

    @Test func removingThePrimaryPhotoSelectsItsSurvivingNeighbor() async throws {
        let model = try await model()
        defer { model.shutdownService() }
        model.setSelection([125], primary: 125)
        model.annotate(ids: [125], rating: 0)
        try await settle(model)
        #expect(!model.photos.contains { $0.id == 125 })
        #expect(model.selectedID == 126 && model.selectedIDs == [126])
    }

    @Test func captionReorderingKeepsSurvivingSelection() async throws {
        let model = try await model()
        defer { model.shutdownService() }
        model.setSelection([249, 250], primary: 250)
        model.annotate(ids: [250], caption: "A remembered frame")
        try await settle(model)
        #expect(model.photos.first?.id == 250)
        #expect(model.photos.first?.caption == "A remembered frame")
        #expect(model.selectedIDs == [249, 250] && model.selectedID == 250)
        #expect(StarredProtocol.fixture.withLock { $0.pageOffsets } == [0, 0])
    }

    @Test func annotationCancelsAPageFromThePreviousStarredOrdering() async throws {
        let model = try await model()
        defer { model.shutdownService(); StarredProtocol.releasePages() }
        StarredProtocol.fixture.withLock { $0.delayPages = true }
        model.loadMore()
        try await waitUntil { StarredProtocol.fixture.withLock { $0.heldPages.count == 1 } }
        model.annotate(ids: [1], rating: 0)
        try await waitUntil { !model.isSaving }
        StarredProtocol.releasePages()
        try await settle(model)
        #expect(StarredProtocol.fixture.withLock { $0.cancelledPages } == 1)
        #expect(model.photos.map(\.id) == Array(2...251))
        #expect(!model.isPaging && model.nextOffset == nil)
    }
}
