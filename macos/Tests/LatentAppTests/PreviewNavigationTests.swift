import Foundation
import LatentCore
import Synchronization
import Testing
@testable import LatentApp

private struct PreviewResponseWork: @unchecked Sendable { let item: DispatchWorkItem }

private struct PreviewFixture: Sendable {
    var ready = true
    var dataID = "preview-fixture"
    var holdPage = false
    var holdAnnotations = false
    var pageFailures = 0
    var transportFailures = 0
    var duplicatePage = false
    var emptyPage = false
    var firstPageIDs = Array(1...250)
    var requests: [String] = []
    var held: [PreviewResponseWork] = []

    func photo(_ id: Int) -> [String: Any] {
        ["id": id, "name": "SYNTHETIC_\(id).ARW", "remote_path": "/synthetic/\(id).ARW",
         "provider": "synthetic", "fingerprint": "fixture-\(id)", "size_bytes": 1024,
         "contact_url": "/media/\(id).jpg", "preview_url": "/media/\(id).jpg",
         "preview_available": false, "preview_width": 480, "preview_height": 320,
         "rating": 5, "caption": ""]
    }

    mutating func response(_ request: URLRequest) -> (Data, Int, Bool) {
        let url = request.url!
        let path = url.path
        requests.append(url.path + (url.query.map { "?" + $0 } ?? ""))
        let query = URLComponents(url: url, resolvingAgainstBaseURL: false)!.queryItems ?? []
        let next = query.first { $0.name == "offset" }?.value == "250"
        let otherDate = query.first { $0.name == "date" }?.value == "2026-08-30"
        if next && transportFailures > 0 {
            transportFailures -= 1
            return (Data(), -1, false)
        }
        var status = 200
        let body: [String: Any]
        if path == "/health" {
            body = ["status": "ok", "service": "latent", "protocol_version": ready ? 1 : 2,
                    "service_version": "fixture", "instance_id": "f699d3ac-607a-4c96-9bf9-14cd7206feaa",
                    "pid": 42, "data_id": dataID]
        } else if path == "/api/library" {
            body = ["dates": [], "cached_assets": 251, "workspace": ["writable": true]]
        } else if path == "/api/sequences" {
            body = ["sequences": []]
        } else if path == "/api/search" || path.hasSuffix("/similar") {
            body = ["results": [photo(1), photo(2)], "total": 500]
        } else if path == "/api/sequences/finite" {
            body = ["sequence": ["id": "finite", "name": "Finite results", "note": "", "item_count": 500,
                                  "items": [1, 2].map { ["id": "item-\($0)", "name": "Fixture", "asset": photo($0)] }]]
        } else if path == "/api/annotations" {
            var updated = photo(1)
            updated["rating"] = 0
            body = ["assets": [updated]]
        } else if next && pageFailures > 0 {
            pageFailures -= 1
            status = 500
            body = ["message": "Synthetic next-page failure"]
        } else {
            let ids = otherDate ? [999] : next ? (emptyPage ? [] : [duplicatePage ? 250 : 251]) : firstPageIDs
            body = ["assets": ids.map(photo), "total": otherDate ? 1 : 251,
                    "has_more": !next && !otherDate,
                    "next_offset": next || otherDate ? NSNull() : 250]
        }
        return (try! JSONSerialization.data(withJSONObject: body), status,
                (next && holdPage) || (path == "/api/annotations" && holdAnnotations))
    }
}

private final class PreviewProtocol: URLProtocol, @unchecked Sendable {
    static let fixture = Mutex(PreviewFixture())
    private let work = Mutex<PreviewResponseWork?>(nil)
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let (data, status, hold) = Self.fixture.withLock { $0.response(request) }
        let item = DispatchWorkItem { [weak self] in
            guard let self else { return }
            if status < 0 {
                client?.urlProtocol(self, didFailWithError: URLError(.networkConnectionLost))
                return
            }
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status,
                                                                httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        }
        work.withLock { $0 = PreviewResponseWork(item: item) }
        if hold { Self.fixture.withLock { $0.held.append(PreviewResponseWork(item: item)) } }
        else { DispatchQueue.global().async(execute: item) }
    }
    override func stopLoading() { work.withLock { $0?.item.cancel() } }

    static func release() {
        let held = fixture.withLock { value in
            let pending = value.held
            value.held = []
            value.holdPage = false
            value.holdAnnotations = false
            return pending
        }
        for response in held where !response.item.isCancelled {
            DispatchQueue.global().async(execute: response.item)
        }
    }
}

@MainActor @Suite(.serialized)
struct PreviewNavigationTests {
    private func makeModel(starred: Bool = false) async throws -> LibraryModel {
        PreviewProtocol.fixture.withLock { $0 = PreviewFixture() }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [PreviewProtocol.self]
        let client = try LibraryClient(baseURL: URL(string: "http://localhost:8766")!, session: URLSession(configuration: configuration))
        let model = LibraryModel(client: client, preferences: MemoryPreferences())
        try await model.service.connect()
        model.navigate(to: starred ? .starred : .library(date: "2026-08-29"))
        try await waitUntil { !model.isLoading }
        try #require(model.photos.count == 250 && model.nextOffset == 250)
        model.selectedID = 250
        model.previewSelection()
        return model
    }

    private func waitUntil(_ condition: () -> Bool) async throws {
        let deadline = ContinuousClock.now.advanced(by: .seconds(2))
        while !condition() {
            guard ContinuousClock.now < deadline else { throw ServiceConnectionError("Preview fixture did not settle") }
            try await Task.sleep(for: .milliseconds(2))
        }
    }

    private func cleanup(_ model: LibraryModel) {
        model.previewPhoto = nil
        model.shutdownService()
        PreviewProtocol.release()
    }

    @Test(arguments: [false, true])
    func nextRemainsAvailableWhenTheCurrentPageHasMorePhotos(starred: Bool) async throws {
        let model = try await makeModel(starred: starred)
        defer { cleanup(model) }
        let preview = PhotoPreview(model: model)
        #expect(preview.canMove(by: 1))
        #expect(preview.canMove(by: -1))
    }

    @Test(arguments: [false, true])
    func nextLoadsOnePageAndAdvancesToItsFirstPhoto(starred: Bool) async throws {
        let model = try await makeModel(starred: starred)
        defer { cleanup(model) }
        PhotoPreview(model: model).move(by: 1)
        try await waitUntil { model.previewPhoto?.id == 251 }
        #expect(model.selectedID == 251 && model.photos.count == 251)
        #expect(!PhotoPreview(model: model).canMove(by: 1))
        #expect(PreviewProtocol.fixture.withLock { $0.requests.filter { $0.contains("offset=250") }.count } == 1)
    }

    @Test func repeatedNextJoinsAnExistingGalleryPageAndAdvancesOnce() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        PreviewProtocol.fixture.withLock { $0.holdPage = true }
        model.loadMore()
        try await waitUntil { PreviewProtocol.fixture.withLock { !$0.held.isEmpty } }
        model.movePreview(by: 1)
        model.movePreview(by: 1)
        #expect(model.isPreviewPaging)
        #expect(!model.canMovePreview(by: 1) && !model.canMovePreview(by: -1))
        model.movePreview(by: -1)
        #expect(model.previewPhoto?.id == 250)
        #expect(PreviewProtocol.fixture.withLock { $0.requests.filter { $0.contains("offset=250") }.count } == 1)
        PreviewProtocol.release()
        try await waitUntil { !model.isPreviewPaging }
        #expect(model.previewPhoto?.id == 251 && model.selectedID == 251)
    }

    @Test func aFailedPageKeepsThePhotoAndCursorUntilExplicitRetry() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        PreviewProtocol.fixture.withLock { $0.pageFailures = 1 }
        model.movePreview(by: 1)
        try await waitUntil { !model.isPreviewPaging }
        #expect(model.previewPhoto?.id == 250 && model.selectedID == 250)
        #expect(model.previewPagingError?.contains("Synthetic next-page failure") == true)
        #expect(model.nextOffset == 250 && model.canMovePreview(by: 1))
        #expect(PreviewProtocol.fixture.withLock { $0.requests.filter { $0.contains("offset=250") }.count } == 1)
        model.movePreview(by: 1)
        try await waitUntil { !model.isPreviewPaging }
        #expect(model.previewPhoto?.id == 251 && model.previewPagingError == nil)
        #expect(model.errorMessage == nil)
        #expect(PreviewProtocol.fixture.withLock { $0.requests.filter { $0.contains("offset=250") }.count } == 2)
    }

    @Test func dismissalAndReopeningTheSamePhotoDoesNotRestoreAnOldAdvance() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        PreviewProtocol.fixture.withLock { $0.holdPage = true }
        model.movePreview(by: 1)
        try await waitUntil { PreviewProtocol.fixture.withLock { !$0.held.isEmpty } }
        let anchor = model.previewPhoto
        model.previewPhoto = nil
        model.previewPhoto = anchor
        #expect(!model.isPreviewPaging)
        PreviewProtocol.release()
        try await waitUntil { !model.isPaging }
        #expect(model.previewPhoto?.id == 250 && model.selectedID == 250)
        model.movePreview(by: 1)
        #expect(model.previewPhoto?.id == 251)
        #expect(PreviewProtocol.fixture.withLock { $0.requests.filter { $0.contains("offset=250") }.count } == 1)
    }

    @Test(arguments: [false, true])
    func transportRetryClearsOnlyItsOwnDisplayedError(preserveUnrelatedError: Bool) async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        PreviewProtocol.fixture.withLock { $0.transportFailures = 1 }
        model.movePreview(by: 1)
        try await waitUntil { !model.isPreviewPaging }
        #expect(model.service.isConnected)
        #expect(model.previewPhoto?.id == 250 && model.nextOffset == 250)
        #expect(model.previewPagingError?.contains("local library is unavailable") == true)
        #expect(model.previewPagingError == model.errorMessage)
        if preserveUnrelatedError { model.errorMessage = "An unrelated operation needs attention" }
        model.movePreview(by: 1)
        try await waitUntil { !model.isPreviewPaging }
        #expect(model.previewPhoto?.id == 251 && model.previewPagingError == nil)
        #expect(model.errorMessage == (preserveUnrelatedError ? "An unrelated operation needs attention" : nil))
        #expect(PreviewProtocol.fixture.withLock { $0.requests.filter { $0.contains("offset=250") }.count } == 2)
    }

    @Test func changingTheGalleryCannotAdvanceOrReplaceANewPreview() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        PreviewProtocol.fixture.withLock { $0.holdPage = true }
        model.movePreview(by: 1)
        try await waitUntil { PreviewProtocol.fixture.withLock { !$0.held.isEmpty } }
        model.navigate(to: .library(date: "2026-08-30"))
        try await waitUntil { !model.isLoading }
        model.previewSelection()
        PreviewProtocol.release()
        #expect(model.previewPhoto?.id == 999 && model.selectedID == 999)
        #expect(model.photos.map(\.id) == [999] && !model.isPreviewPaging)
    }

    @Test func refreshingTheSameStarredSourceInvalidatesThePendingAdvance() async throws {
        let model = try await makeModel(starred: true)
        defer { cleanup(model) }
        PreviewProtocol.fixture.withLock { $0.holdPage = true }
        model.movePreview(by: 1)
        try await waitUntil { PreviewProtocol.fixture.withLock { !$0.held.isEmpty } }
        PreviewProtocol.fixture.withLock { $0.firstPageIDs = Array(2...250) + [1] }
        model.navigate(to: .starred, preservingSelection: true, retainingPhotos: true)
        try await waitUntil { !model.isLoading }
        PreviewProtocol.release()
        #expect(model.previewPhoto?.id == 250 && !model.isPreviewPaging)
        model.movePreview(by: 1)
        #expect(model.previewPhoto?.id == 1)
    }

    @Test func aStarredMutationCannotTreatItsCancelledPageAsAnAdvance() async throws {
        let model = try await makeModel(starred: true)
        defer { cleanup(model) }
        PreviewProtocol.fixture.withLock { $0.holdPage = true; $0.holdAnnotations = true }
        model.movePreview(by: 1)
        try await waitUntil { PreviewProtocol.fixture.withLock { !$0.held.isEmpty } }
        model.annotate(ids: [1], rating: 0)
        try await waitUntil { !model.isPreviewPaging }
        #expect(model.previewPhoto?.id == 250 && model.selectedID == 250)
        #expect(model.nextOffset == nil && !model.canMovePreview(by: 1))
        #expect(model.previewPagingError?.contains("photo order changed") == true)
    }

    @Test(arguments: [GallerySource.search(query: "fixture", order: .closest),
                      .similar(id: 1, name: "Fixture"), .sequence(id: "finite")])
    func finiteResultsNeverFallBackToUnrelatedLibraryPaging(source: GallerySource) async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        model.navigate(to: source)
        try await waitUntil { !model.isLoading }
        try #require(model.photos.count == 2 && model.total == 500)
        model.selectedID = 2
        model.previewSelection()
        PreviewProtocol.fixture.withLock { $0.requests = [] }
        #expect(!model.canMovePreview(by: 1))
        model.movePreview(by: 1)
        #expect(model.previewPhoto?.id == 2 && !model.isPreviewPaging)
        #expect(PreviewProtocol.fixture.withLock { $0.requests.isEmpty })
    }

    @Test func anEmptyTerminalPageStopsAtTheCurrentPhotoWithoutALoop() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        PreviewProtocol.fixture.withLock { $0.emptyPage = true }
        model.movePreview(by: 1)
        try await waitUntil { !model.isPreviewPaging }
        #expect(model.previewPhoto?.id == 250 && model.photos.count == 250)
        #expect(model.nextOffset == nil && !model.canMovePreview(by: 1))
        #expect(model.previewPagingError?.contains("end of these results") == true)
        #expect(PreviewProtocol.fixture.withLock { $0.requests.filter { $0.contains("offset=250") }.count } == 1)
    }

    @Test func advancingResolvesTheAnchorAgainAfterPositionsChange() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        PreviewProtocol.fixture.withLock { $0.holdPage = true }
        model.movePreview(by: 1)
        try await waitUntil { PreviewProtocol.fixture.withLock { !$0.held.isEmpty } }
        model.photos.removeFirst()
        PreviewProtocol.release()
        try await waitUntil { !model.isPreviewPaging }
        #expect(model.previewPhoto?.id == 251 && model.selectedID == 251)
    }
}
