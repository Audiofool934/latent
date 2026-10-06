import Foundation
import LatentCore
import Synchronization
import Testing
@testable import LatentApp

private final class ArchiveProtocol: URLProtocol, @unchecked Sendable {
    static let queries = Mutex<[[String: String]]>([])
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let url = request.url!
        let body: String
        switch url.path {
        case "/health": body = """
            {"status":"ok","service":"latent","protocol_version":1,"service_version":"fixture",
             "instance_id":"f699d3ac-607a-4c96-9bf9-14cd7206feaa","pid":42,"data_id":"archive-fixture"}
            """
        case "/api/library": body = """
            {"dates":[{"capture_date":"2026-01-12","asset_count":1},
                      {"capture_date":"2026-01-01","asset_count":1}],
             "cached_assets":2,"workspace":{"writable":true}}
            """
        case "/api/sequences": body = "{\"sequences\":[]}"
        case "/api/sequences/smart": body = """
            {"sequence":{"id":"smart","name":"Top picks","note":"","item_count":2,
             "smart_filters":{"rating_min":4,"flag":"pick"},"next_offset":1,
             "items":[{"id":"smart-1","name":"first.jpg","asset":{"id":1,"name":"first.jpg",
             "remote_path":"/first.jpg","size_bytes":1024,"contact_url":"/media/first.jpg",
             "preview_url":"/media/first.jpg","preview_available":false}}]}}
            """
        default:
            let query = Dictionary(uniqueKeysWithValues: (URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? [])
                .map { ($0.name, $0.value ?? "") })
            Self.queries.withLock { $0.append(query) }
            let second = query["offset"] == "1"
            body = """
                {"assets":[{"id":\(second ? 2 : 1),"name":"fixture.jpg","remote_path":"/fixture.jpg",
                 "size_bytes":1024,"contact_url":"/media/fixture.jpg","preview_url":"/media/fixture.jpg",
                 "preview_available":false}],"total":2,"has_more":\(!second),"next_offset":\(second ? "null" : "1")}
                """
        }
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: url, statusCode: 200,
            httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@MainActor @Suite(.serialized)
struct ArchiveNavigationTests {
    @Test func importFiltersAndPagingStayInsideTheReviewedBatch() async throws {
        let (model, preferences, suite) = try await fixture()
        defer { model.shutdownService(); preferences.removePersistentDomain(forName: suite) }
        let source = GallerySource.importBatch(id: "batch-fixture", name: "r2 + r5")
        model.navigate(to: source)
        try await waitUntil { !model.isLoading }
        model.applySearchFilters(PhotoFilters(ratingMin: 4, flag: "pick"))
        try await waitUntil { !model.isLoading }
        #expect(model.source == source)
        await model.loadMore()?.value
        let query = ArchiveProtocol.queries.withLock { $0.last! }
        #expect(query["import_id"] == "batch-fixture")
        #expect(query["rating_min"] == "4" && query["flag"] == "pick")
        #expect(query["offset"] == "1" && query["collapse_timelapses"] == nil)
    }

    @Test func combinedFiltersSurvivePagingAndTimelineNavigationClearsThem() async throws {
        let (model, preferences, suite) = try await fixture()
        defer { model.shutdownService(); preferences.removePersistentDomain(forName: suite) }
        let filters = PhotoFilters(dateFrom: "2026-01-01", dateTo: "2026-12-31", ratingMin: 4, flag: "pick")
        model.applySearchFilters(filters)
        try await waitUntil { !model.isLoading }
        await model.loadMore()?.value
        let last = ArchiveProtocol.queries.withLock { $0.last! }
        #expect(last["rating_min"] == "4" && last["flag"] == "pick")
        #expect(last["date_from"] == "2026-01-01" && last["date_to"] == "2026-12-31")
        #expect(model.photos.map(\.id) == [1, 2] && model.searchFilters == filters)
        model.navigate(to: .library(date: "2024-02"))
        #expect(model.searchFilters.isEmpty)
        #expect(model.filtersForSearch.dateFrom == "2024-02-01")
        #expect(model.filtersForSearch.dateTo == "2024-02-29")
    }

    @Test func smartSequencePagingUsesSavedFiltersInsteadOfUnfilteredLibrary() async throws {
        let (model, preferences, suite) = try await fixture()
        defer { model.shutdownService(); preferences.removePersistentDomain(forName: suite) }
        model.navigate(to: .sequence(id: "smart"))
        try await waitUntil { !model.isLoading }
        #expect(model.subtitle == "1 of 2 photos")
        #expect(model.activeSequence?.smartFilters?.ratingMin == 4)
        await model.loadMore()?.value
        let last = ArchiveProtocol.queries.withLock { $0.last! }
        #expect(last["rating_min"] == "4" && last["flag"] == "pick" && last["offset"] == "1")
        #expect(model.photos.map(\.id) == [1, 2])
        #expect(!model.canMoveSelected(by: 1))
    }

    @Test func timeLapseCollapseIsOptInAndNeverLeaksIntoSmartSequencePaging() async throws {
        let (model, preferences, suite) = try await fixture()
        defer { model.shutdownService(); preferences.removePersistentDomain(forName: suite) }
        #expect(ArchiveProtocol.queries.withLock { $0.last?["collapse_timelapses"] } == nil)
        model.setCollapseTimelapses(true)
        try await waitUntil { !model.isLoading }
        await model.loadMore()?.value
        #expect(ArchiveProtocol.queries.withLock { $0.last?["collapse_timelapses"] } == "1")
        model.navigate(to: .sequence(id: "smart"))
        try await waitUntil { !model.isLoading }
        await model.loadMore()?.value
        #expect(ArchiveProtocol.queries.withLock { $0.last?["collapse_timelapses"] } == nil)
        #expect(preferences.bool(forKey: "collapseTimelapses.archive-fixture"))
        model.shutdownService()
        let restored = LibraryModel(client: model.client, preferences: preferences)
        defer { restored.shutdownService() }
        await restored.start()
        try await waitUntil { !restored.isLoading }
        #expect(restored.collapseTimelapses)
        #expect(ArchiveProtocol.queries.withLock { $0.last?["collapse_timelapses"] } == "1")
    }

    @Test func timeLapseExpansionPagesAllFramesAndReturnsToItsTimelineScope() async throws {
        let (model, preferences, suite) = try await fixture()
        defer { model.shutdownService(); preferences.removePersistentDomain(forName: suite) }
        model.navigate(to: .library(date: "2026-01"))
        try await waitUntil { !model.isLoading }
        model.setCollapseTimelapses(true)
        try await waitUntil { !model.isLoading }
        model.openTimelapse("group-fixture")
        try await waitUntil { !model.isLoading }
        #expect(model.source == .timelapse(id: "group-fixture"))
        model.previewSelection()
        #expect(model.canMovePreview(by: 1))
        await model.loadMore()?.value
        let query = ArchiveProtocol.queries.withLock { $0.last! }
        #expect(query["timelapse_group"] == "group-fixture")
        #expect(query["collapse_timelapses"] == nil && query["date"] == nil)
        #expect(model.photos.map(\.id) == [1, 2])
        model.closeTimelapse()
        try await waitUntil { !model.isLoading }
        #expect(model.source == .library(date: "2026-01"))
    }

    private func fixture() async throws -> (LibraryModel, UserDefaults, String) {
        ArchiveProtocol.queries.withLock { $0 = [] }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ArchiveProtocol.self]
        let client = try LibraryClient(baseURL: URL(string: "http://localhost:8766")!, session: URLSession(configuration: configuration))
        let suite = "LatentFilterTests.\(UUID().uuidString)"
        let preferences = UserDefaults(suiteName: suite)!
        let model = LibraryModel(client: client, preferences: preferences)
        await model.start()
        try await waitUntil { !model.isLoading }
        return (model, preferences, suite)
    }

    @Test(arguments: ["2026", "2026-01"])
    func periodSelectionSurvivesPagingAndRestart(_ period: String) async throws {
        ArchiveProtocol.queries.withLock { $0 = [] }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ArchiveProtocol.self]
        let client = try LibraryClient(baseURL: URL(string: "http://localhost:8766")!, session: URLSession(configuration: configuration))
        let suite = "LatentArchiveNavigationTests.\(UUID().uuidString)"
        let preferences = UserDefaults(suiteName: suite)!
        let model = LibraryModel(client: client, preferences: preferences)
        defer { model.shutdownService(); preferences.removePersistentDomain(forName: suite) }
        await model.start()
        try await waitUntil { !model.isLoading }
        model.navigate(to: .library(date: period))
        try await waitUntil { !model.isLoading }
        await model.loadMore()?.value
        #expect(model.photos.map(\.id) == [1, 2] && model.nextOffset == nil)
        #expect(model.selectedDate == period && !model.libraryNavigationSelected)
        #expect(model.title == PhotoFormatting.archivePeriod(period))
        #expect(ArchiveProtocol.queries.withLock { Array($0.suffix(2)).map { $0["date"] } } == [period, period])
        #expect(preferences.string(forKey: "lastCaptureDate") == period)
        model.shutdownService()
        let restored = LibraryModel(client: client, preferences: preferences)
        defer { restored.shutdownService() }
        await restored.start()
        try await waitUntil { !restored.isLoading }
        #expect(restored.source == .library(date: period))
        #expect(ArchiveProtocol.queries.withLock { $0.last?["date"] } == period)
    }

    private func waitUntil(_ condition: () -> Bool) async throws {
        let deadline = ContinuousClock.now.advanced(by: .seconds(2))
        while !condition() {
            guard ContinuousClock.now < deadline else { throw ServiceConnectionError("Archive fixture did not settle") }
            try await Task.sleep(for: .milliseconds(2))
        }
    }
}
