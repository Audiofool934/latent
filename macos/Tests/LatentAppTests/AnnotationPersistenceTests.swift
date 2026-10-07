import AppKit
import Foundation
import LatentCore
import Synchronization
import Testing
@testable import LatentApp

private func annotationPhoto(_ id: Int) -> Photo {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    return try! decoder.decode(Photo.self, from: Data("""
        {"id":\(id),"name":"\(id).ARW","remote_path":"/fixture/\(id).ARW","size_bytes":1024,
         "provider":"fixture","fingerprint":"\(String(repeating: "a", count: 64))",
         "contact_url":"/media/\(id).jpg","preview_url":"/media/\(id).jpg",
         "preview_available":false,"rating":0,"caption":""}
        """.utf8))
}

private struct AnnotationPatch: Decodable, Sendable {
    let assetIds: [Int]
    let rating: Int?
    let caption: String?
    let flag: PhotoFlag?
}

private struct AnnotationWork: @unchecked Sendable { let item: DispatchWorkItem; let path: String }

private struct AnnotationFixture: Sendable {
    var photos = Dictionary(uniqueKeysWithValues: (1...3).map { ($0, annotationPhoto($0)) })
    var patches: [AnnotationPatch] = []
    var held: [AnnotationWork] = []
    var holdsRemaining = 1
    var holdHealth = false
    var responseCodes: [Int] = []
    var dataID = "annotation-fixture"
    var holdSequence = false
    var holdStarredCount = 0
    var starredRequests = 0
    var holdAssetsCount = 0
    var assetResponseIDs = [3]
    var bodySizes: [Int] = []
    var malformedAcknowledgment = false
}

private final class AnnotationProtocol: URLProtocol, @unchecked Sendable {
    static let fixture = Mutex(AnnotationFixture())
    private let work = Mutex<AnnotationWork?>(nil)
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let (data, status, hold) = Self.fixture.withLock { fixture -> (Data, Int, Bool) in
            switch request.url!.path {
            case "/health":
                return (Data("""
                    {"status":"ok","service":"latent","protocol_version":1,"service_version":"fixture",
                     "instance_id":"f699d3ac-607a-4c96-9bf9-14cd7206feaa","pid":42,"data_id":"\(fixture.dataID)"}
                    """.utf8), 200, fixture.holdHealth)
            case "/api/annotations":
                let decoder = JSONDecoder()
                decoder.keyDecodingStrategy = .convertFromSnakeCase
                let body = Self.requestBody(request)
                fixture.bodySizes.append(body.count)
                let patch = try! decoder.decode(AnnotationPatch.self, from: body)
                fixture.patches.append(patch)
                let status = fixture.responseCodes.isEmpty ? 200 : fixture.responseCodes.removeFirst()
                let hold = fixture.holdsRemaining > 0
                if hold { fixture.holdsRemaining -= 1 }
                if status != 200 { return (Data("{\"message\":\"Fixture save failed\"}".utf8), status, hold) }
                for id in patch.assetIds {
                    guard var photo = fixture.photos[id] else { continue }
                    if let rating = patch.rating { photo.rating = rating }
                    if let caption = patch.caption { photo.caption = caption }
                    if let flag = patch.flag { photo.flag = flag }
                    fixture.photos[id] = photo
                }
                struct Response: Encodable { let assets: [Photo] }
                let encoder = JSONEncoder()
                encoder.keyEncodingStrategy = .convertToSnakeCase
                let assets = fixture.malformedAcknowledgment ? [] : patch.assetIds.compactMap { fixture.photos[$0] }
                return (try! encoder.encode(Response(assets: assets)), 200, hold)
            case "/api/sequences":
                if request.httpMethod == "POST" {
                    let body = try! JSONSerialization.jsonObject(with: Self.requestBody(request)) as! [String: Any]
                    let id = body["sequence_id"] as? String ?? "saved"
                    return (Data("{\"sequence\":{\"id\":\"\(id)\",\"name\":\"Fixture\",\"note\":\"\",\"item_count\":0}}".utf8), 200, fixture.holdSequence)
                }
                return (Data("{\"sequences\":[]}".utf8), 200, false)
            case "/api/starred":
                fixture.starredRequests += 1
                let hold = fixture.holdStarredCount > 0
                if hold { fixture.holdStarredCount -= 1 }
                let encoder = JSONEncoder()
                encoder.keyEncodingStrategy = .convertToSnakeCase
                let photos = fixture.photos.values.filter { ($0.rating ?? 0) > 0 }.sorted { $0.id < $1.id }
                let assets = String(data: try! encoder.encode(photos), encoding: .utf8)!
                return (Data("{\"assets\":\(assets),\"total\":\(photos.count),\"has_more\":false,\"next_offset\":null}".utf8), 200, hold)
            case "/api/sequences/saved/items/order":
                let encoder = JSONEncoder()
                encoder.keyEncodingStrategy = .convertToSnakeCase
                let items = [2, 1].map { id in
                    let photo = String(data: try! encoder.encode(fixture.photos[id]!), encoding: .utf8)!
                    return "{\"id\":\"item-\(id)\",\"name\":\"Photo\",\"asset\":\(photo)}"
                }.joined(separator: ",")
                return (Data("{\"sequence\":{\"id\":\"saved\",\"name\":\"Fixture\",\"note\":\"\",\"item_count\":2,\"items\":[\(items)]}}".utf8), 200, fixture.holdSequence)
            default:
                let encoder = JSONEncoder()
                encoder.keyEncodingStrategy = .convertToSnakeCase
                let photos = fixture.assetResponseIDs.compactMap { fixture.photos[$0] }
                let assets = String(data: try! encoder.encode(photos), encoding: .utf8)!
                let hold = fixture.holdAssetsCount > 0
                if hold { fixture.holdAssetsCount -= 1 }
                return (Data("{\"assets\":\(assets),\"total\":\(photos.count),\"has_more\":false,\"next_offset\":null}".utf8), 200, hold)
            }
        }
        let item = DispatchWorkItem { [weak self] in
            guard let self else { return }
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status,
                                                                httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        }
        work.withLock { $0 = AnnotationWork(item: item, path: request.url!.path) }
        if hold { Self.fixture.withLock { $0.held.append(AnnotationWork(item: item, path: request.url!.path)) } }
        else { DispatchQueue.global().async(execute: item) }
    }

    override func stopLoading() { work.withLock { $0?.item.cancel() } }

    static func release(path: String? = nil) {
        let held = fixture.withLock { fixture in
            let held = fixture.held.filter { path == nil || $0.path == path }
            fixture.held.removeAll { path == nil || $0.path == path }
            return held
        }
        for response in held where !response.item.isCancelled { DispatchQueue.global().async(execute: response.item) }
    }

    private static func requestBody(_ request: URLRequest) -> Data {
        if let data = request.httpBody { return data }
        let stream = request.httpBodyStream!
        stream.open()
        defer { stream.close() }
        var buffer = [UInt8](repeating: 0, count: 4096)
        var data = Data()
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            data.append(contentsOf: buffer.prefix(count))
        }
        return data
    }
}

@MainActor @Suite(.serialized)
struct AnnotationPersistenceTests {
    private func makeModel(store: any AnnotationDraftStore = MemoryAnnotationDraftStore(), resetFixture: Bool = true) async throws -> LibraryModel {
        if resetFixture { AnnotationProtocol.fixture.withLock { $0 = AnnotationFixture() } }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [AnnotationProtocol.self]
        let client = try LibraryClient(baseURL: URL(string: "http://localhost:8766")!, session: URLSession(configuration: configuration))
        let model = LibraryModel(client: client, preferences: MemoryPreferences(), annotationStore: store)
        do {
            try await model.service.connect()
            model.photos = (1...3).map(annotationPhoto)
            model.selectedID = 1
            return model
        } catch {
            model.shutdownService()
            throw error
        }
    }

    private func waitUntil(_ condition: () -> Bool) async throws {
        let deadline = ContinuousClock.now.advanced(by: .seconds(2))
        while !condition() {
            guard ContinuousClock.now < deadline else { throw ServiceConnectionError("Annotation fixture did not settle") }
            try await Task.sleep(for: .milliseconds(2))
        }
    }

    private func cleanup(_ model: LibraryModel) {
        model.shutdownService()
        AnnotationProtocol.release()
    }

    @Test func ratingsForDifferentPhotosSurviveAnEarlierPendingSave() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        model.rateSelection(2)
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        model.setSelection([2], primary: 2)
        model.rateSelection(5)
        AnnotationProtocol.release()
        try await waitUntil { !model.isSaving }
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[1]?.rating } == 2)
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[2]?.rating } == 5)
    }

    @Test func theLatestRatingForTheSamePhotoWinsWithoutLosingAnotherPhoto() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        model.rateSelection(1)
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        model.setSelection([2], primary: 2)
        model.rateSelection(4)
        model.setSelection([1], primary: 1)
        model.rateSelection(3)
        model.rateSelection(5)
        AnnotationProtocol.release()
        try await waitUntil { !model.isSaving }
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[1]?.rating } == 5)
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[2]?.rating } == 4)
    }

    @Test func pickThenRejectClearsFirstAndKeepsTheLatestFlagAndStars() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        model.flagSelection(.pick)
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        #expect(model.selectedPhoto?.flag == .pick)
        model.flagSelection(.reject)
        #expect(model.selectedPhoto?.flag == .unmarked)
        model.flagSelection(.reject)
        #expect(model.selectedPhoto?.flag == .reject)
        model.rateSelection(4)
        AnnotationProtocol.release()
        try await waitUntil { !model.isSaving }
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[1]?.flag } == .reject)
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[1]?.rating } == 4)
        #expect(!model.annotationQueue.hasPendingEdits)
    }

    @Test func previewUsesTheSameGradingAndNavigationKeysAndSpaceClosesIt() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        func key(_ text: String, code: UInt16 = 0) throws -> NSEvent {
            try #require(NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
                windowNumber: 0, context: nil, characters: text, charactersIgnoringModifiers: text,
                isARepeat: false, keyCode: code))
        }
        #expect(model.handleGalleryKey(try key(" ", code: 49)))
        #expect(model.previewPhoto?.id == 1)
        #expect(model.handleGalleryKey(try key("2", code: 19)))
        #expect(model.previewPhoto?.rating == 2)
        #expect(model.handleGalleryKey(try key("e")))
        #expect(model.previewPhoto?.id == 2)
        #expect(model.handleGalleryKey(try key("q")))
        #expect(model.previewPhoto?.flag == .pick)
        #expect(model.handleGalleryKey(try key("w")))
        #expect(model.previewPhoto?.id == 1)
        #expect(model.handleGalleryKey(try key(" ", code: 49)))
        #expect(model.previewPhoto == nil)
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        AnnotationProtocol.release()
        try await waitUntil { !model.isSaving }
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[1]?.rating } == 2)
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[2]?.flag } == .pick)
    }

    @Test func queuedCaptionsAndRatingsPreserveEachOther() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        model.rateSelection(4)
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        model.annotate(ids: [1], caption: "Quiet light")
        model.annotate(ids: [2], rating: 3)
        AnnotationProtocol.release()
        try await waitUntil { !model.isSaving }
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[1]?.rating } == 4)
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[1]?.caption } == "Quiet light")
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[2]?.rating } == 3)
    }

    @Test func navigationDoesNotRetargetOrDropQueuedRatings() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        model.rateSelection(2)
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        model.setSelection([2], primary: 2)
        model.rateSelection(5)
        model.navigate(to: .library(date: "2026-02-01"))
        try await waitUntil { !model.isLoading }
        AnnotationProtocol.release()
        try await waitUntil { !model.isSaving }
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[2]?.rating } == 5)
        #expect(model.source == .library(date: "2026-02-01"))
        #expect(model.photos.map(\.id) == [3] && model.photos.first?.rating == 0)
    }

    @Test func failuresPauseWithoutLosingNewerRatingsCaptionsOrClearingValues() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        AnnotationProtocol.fixture.withLock { $0.responseCodes = [500] }
        model.rateSelection(4)
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        model.annotate(ids: [1], rating: 0, caption: "")
        model.annotate(ids: [2], rating: 3, caption: "Keep B")
        AnnotationProtocol.release()
        try await waitUntil { !model.isSaving }
        #expect(model.annotationQueue.pendingCount == 2)
        #expect(model.annotationQueue.errorMessage != nil)
        model.annotate(ids: [2], rating: 5)
        model.navigate(to: .library(date: "2026-02-01"))
        try await waitUntil { !model.isLoading }
        #expect(AnnotationProtocol.fixture.withLock { $0.patches.count } == 1)
        #expect(model.annotationQueue.errorMessage != nil)
        model.retryAnnotations()
        try await waitUntil { !model.isSaving }
        #expect(model.annotationQueue.pendingCount == 0 && model.annotationQueue.errorMessage == nil)
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[1]?.rating } == 0)
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[1]?.caption } == "")
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[2]?.rating } == 5)
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[2]?.caption } == "Keep B")
        #expect(model.photos.map(\.id) == [3])
    }

    @Test func anOlderAcknowledgmentCannotRepaintTheLatestIntent() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        model.rateSelection(1)
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        model.rateSelection(5)
        model.annotate(ids: [1], caption: "Latest")
        #expect(model.photos.first?.rating == 5 && model.photos.first?.caption == "Latest")
        AnnotationProtocol.fixture.withLock { $0.holdsRemaining = 1 }
        AnnotationProtocol.release()
        try await waitUntil { AnnotationProtocol.fixture.withLock { $0.patches.count == 2 && !$0.held.isEmpty } }
        #expect(model.photos.first?.rating == 5 && model.photos.first?.caption == "Latest")
        #expect(model.annotationQueue.pendingCount == 1)
        AnnotationProtocol.release()
        try await waitUntil { !model.isSaving }
        #expect(model.annotationQueue.pendingCount == 0)
    }

    @Test func restartRestoresTheLatestDiskSnapshotPausedAfterAResponseWasLost() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("LatentAnnotation-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: directory) }
        let store = FileAnnotationDraftStore(url: directory.appendingPathComponent("drafts.json"))
        let first = try await makeModel(store: store)
        defer { cleanup(first) }
        first.rateSelection(1)
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        first.rateSelection(5)
        first.annotate(ids: [2], rating: 4, caption: "Remember")
        first.shutdownService()
        try await waitUntil { !first.isSaving }
        AnnotationProtocol.release()
        let restored = try await makeModel(store: store, resetFixture: false)
        defer { cleanup(restored) }
        #expect(restored.annotationQueue.pendingCount == 2 && !restored.isSaving)
        #expect(AnnotationProtocol.fixture.withLock { $0.patches.count } == 1)
        restored.annotate(ids: [2], rating: 3)
        #expect(!restored.isSaving)
        restored.retryAnnotations()
        try await waitUntil { !restored.isSaving }
        #expect(restored.annotationQueue.pendingCount == 0)
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[1]?.rating } == 5)
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[2]?.rating } == 3)
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[2]?.caption } == "Remember")
        let stored = try store.load()
        #expect(String(data: try #require(stored), encoding: .utf8) == "null")
    }

    @Test func restoredDraftCheckingDoesNotClaimTheHealthyLibraryIsDisconnected() async throws {
        let store = MemoryAnnotationDraftStore()
        let first = try await makeModel(store: store)
        defer { cleanup(first) }
        first.rateSelection(3)
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        first.shutdownService()
        try await waitUntil { !first.isSaving }
        AnnotationProtocol.release()

        let restored = LibraryModel(client: first.client, preferences: MemoryPreferences(),
                                    annotationStore: store)
        defer { cleanup(restored) }
        let writesBefore = AnnotationProtocol.fixture.withLock { $0.patches.count }
        AnnotationProtocol.fixture.withLock { $0.holdHealth = true }
        let connecting = Task { try await restored.service.connect() }
        defer { connecting.cancel() }
        try await waitUntil {
            restored.service.isConnecting && AnnotationProtocol.fixture.withLock { $0.held.contains { $0.path == "/health" } }
        }
        // Deliver the same checking and connected notifications as LibraryView.
        restored.serviceStateChanged()
        AnnotationProtocol.fixture.withLock { $0.holdHealth = false }
        AnnotationProtocol.release(path: "/health")
        try await connecting.value
        restored.serviceStateChanged()
        #expect(restored.service.isConnected)
        #expect(restored.annotationQueue.pendingCount == 1 && !restored.isSaving)
        #expect(restored.annotationQueue.errorMessage?.localizedCaseInsensitiveContains("disconnected") == false)
        #expect(restored.annotationQueue.errorMessage?.localizedCaseInsensitiveContains("retry") == true)
        #expect(AnnotationProtocol.fixture.withLock { $0.patches.count } == writesBefore)

        restored.retryAnnotations()
        try await waitUntil { !restored.isSaving }
        #expect(restored.annotationQueue.pendingCount == 0)
        #expect(AnnotationProtocol.fixture.withLock { $0.patches.count } == writesBefore + 1)
    }

    @Test func aDifferentLibraryReceivesNoRestoredOrNewMutations() async throws {
        let store = MemoryAnnotationDraftStore()
        let first = try await makeModel(store: store)
        defer { cleanup(first) }
        first.rateSelection(4)
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        first.shutdownService()
        try await waitUntil { !first.isSaving }
        AnnotationProtocol.release()
        let saved = store.data
        AnnotationProtocol.fixture.withLock { $0.dataID = "other-library" }
        let other = try await makeModel(store: store, resetFixture: false)
        defer { cleanup(other) }
        other.retryAnnotations()
        other.rateSelection(5)
        #expect(AnnotationProtocol.fixture.withLock { $0.patches.count } == 1)
        #expect(other.annotationQueue.pendingCount == 1)
        #expect(other.annotationQueue.errorMessage?.contains("another library") == true)
        #expect(store.data == saved)
    }

    @Test func diskFailureKeepsIntentInMemoryAndBlocksDispatchUntilExplicitRetry() async throws {
        let store = FailingAnnotationStore()
        let model = try await makeModel(store: store)
        defer { cleanup(model) }
        store.failNextSave = true
        model.rateSelection(5)
        #expect(model.annotationQueue.hasUndurableEdits && model.annotationQueue.pendingCount == 1)
        #expect(!model.isSaving && model.photos.first?.rating == 5)
        #expect(AnnotationProtocol.fixture.withLock { $0.patches.isEmpty })
        model.retryAnnotations()
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        AnnotationProtocol.release()
        try await waitUntil { !model.isSaving }
        #expect(!model.annotationQueue.hasUndurableEdits && model.annotationQueue.pendingCount == 0)
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[1]?.rating } == 5)
    }

    @Test func failedAcknowledgmentPersistenceKeepsTheCurrentDesiredFieldsForRetry() async throws {
        let store = FailingAnnotationStore()
        let model = try await makeModel(store: store)
        defer { cleanup(model) }
        model.rateSelection(2)
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        model.rateSelection(5)
        model.annotate(ids: [2], rating: 3)
        store.failNextSave = true
        AnnotationProtocol.release()
        try await waitUntil { !model.isSaving }
        #expect(model.annotationQueue.pendingCount == 2 && model.annotationQueue.hasUndurableEdits)
        #expect(model.photos.first?.rating == 5)
        #expect(AnnotationProtocol.fixture.withLock { $0.patches.count } == 1)
        model.retryAnnotations()
        try await waitUntil { !model.isSaving }
        #expect(model.annotationQueue.pendingCount == 0)
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[1]?.rating } == 5)
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[2]?.rating } == 3)
    }

    @Test func unreadableDraftsArePreservedUntilExplicitlyDiscarded() async throws {
        let store = MemoryAnnotationDraftStore()
        store.data = Data("unfinished draft".utf8)
        let saved = store.data
        let model = try await makeModel(store: store)
        defer { cleanup(model) }
        model.rateSelection(5)
        model.retryAnnotations()
        #expect(store.data == saved)
        #expect(model.annotationQueue.errorMessage?.contains("preserved") == true)
        #expect(AnnotationProtocol.fixture.withLock { $0.patches.isEmpty })
        #expect(model.annotationQueue.discard())
        model.rateSelection(4)
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        AnnotationProtocol.release()
        try await waitUntil { !model.isSaving }
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[1]?.rating } == 4)
    }

    @Test func ratingsAreAcceptedWhileASequenceSaveIsPending() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        AnnotationProtocol.fixture.withLock { $0.holdSequence = true }
        let save = Task { await model.saveSequence(name: "Fixture", note: "", editing: nil, adding: nil) }
        defer { save.cancel() }
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        model.rateSelection(5)
        try await waitUntil { AnnotationProtocol.fixture.withLock { $0.held.count == 2 } }
        AnnotationProtocol.release()
        #expect(await save.value)
        try await waitUntil { !model.isSaving }
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[1]?.rating } == 5)
    }

    @Test func starredRefreshKeepsSelectionAvailableAndCoalescesQueuedEdits() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        AnnotationProtocol.fixture.withLock { fixture in
            for id in 1...3 { fixture.photos[id]?.rating = 5 }
            fixture.holdStarredCount = 1
        }
        model.photos = AnnotationProtocol.fixture.withLock { $0.photos.values.sorted { $0.id < $1.id } }
        model.source = .starred
        model.nextOffset = 250
        model.rateSelection(1)
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        model.setSelection([2], primary: 2)
        model.rateSelection(3)
        #expect(model.nextOffset == nil && model.selectedID == 2)
        #expect(AnnotationProtocol.fixture.withLock { $0.starredRequests } == 0)
        AnnotationProtocol.release()
        try await waitUntil { AnnotationProtocol.fixture.withLock { $0.starredRequests == 1 && !$0.held.isEmpty } }
        #expect(model.isLoading && model.selectedID == 2 && model.photos.count == 3)
        model.rateSelection(4)
        try await waitUntil { !model.isSaving && !model.isLoading }
        AnnotationProtocol.release()
        #expect(AnnotationProtocol.fixture.withLock { $0.photos[2]?.rating } == 4)
        #expect(model.selectedID == 2 && model.photos.first { $0.id == 2 }?.rating == 4)
        #expect(AnnotationProtocol.fixture.withLock { $0.starredRequests } == 2)
    }

    @Test func aLargeSelectionIsSentInBoundedBatchesWithoutLosingPhotos() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        model.photos = (1...501).map(annotationPhoto)
        AnnotationProtocol.fixture.withLock { fixture in
            fixture.photos = Dictionary(uniqueKeysWithValues: model.photos.map { ($0.id, $0) })
        }
        model.annotate(ids: Array(1...501), rating: 4)
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        AnnotationProtocol.release()
        try await waitUntil { !model.isSaving }
        let counts = AnnotationProtocol.fixture.withLock { $0.patches.map { $0.assetIds.count } }
        #expect(counts.count > 1 && counts.allSatisfy { $0 <= 500 } && counts.reduce(0, +) == 501)
        #expect(AnnotationProtocol.fixture.withLock { $0.bodySizes.allSatisfy { $0 <= 64 * 1024 } })
        #expect(AnnotationProtocol.fixture.withLock { $0.photos.values.allSatisfy { $0.rating == 4 } })
    }

    @Test func aLateNavigationSnapshotCannotReplaceAnAcknowledgedRating() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        model.rateSelection(2)
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        model.rateSelection(5)
        AnnotationProtocol.fixture.withLock { $0.assetResponseIDs = [1, 2, 3]; $0.holdAssetsCount = 1 }
        model.navigate(to: .library(date: nil))
        try await waitUntil { AnnotationProtocol.fixture.withLock { $0.held.contains { $0.path == "/api/assets" } } }
        AnnotationProtocol.release(path: "/api/annotations")
        try await waitUntil { !model.annotationQueue.isSaving }
        #expect(model.annotationQueue.pendingCount == 0)
        AnnotationProtocol.release(path: "/api/assets")
        try await waitUntil { !model.isLoading }
        #expect(model.photos.first?.rating == 5)
        // Once the old read completes, a genuinely newer external change can be shown.
        AnnotationProtocol.fixture.withLock { $0.photos[1]?.rating = 3 }
        model.navigate(to: .library(date: nil))
        try await waitUntil { !model.isLoading }
        #expect(model.photos.first?.rating == 3)
    }

    @Test func aLateSequenceMutationCannotReplaceAnAcknowledgedRating() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let items = model.photos.prefix(2).map { photo in
            let asset = String(data: try! encoder.encode(photo), encoding: .utf8)!
            return "{\"id\":\"item-\(photo.id)\",\"name\":\"Photo\",\"asset\":\(asset)}"
        }.joined(separator: ",")
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        model.activeSequence = try decoder.decode(PhotoSequence.self, from: Data("{\"id\":\"saved\",\"name\":\"Fixture\",\"note\":\"\",\"item_count\":2,\"items\":[\(items)]}".utf8))
        model.source = .sequence(id: "saved")
        AnnotationProtocol.fixture.withLock { $0.holdSequence = true }
        model.moveSelected(by: 1)
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        model.rateSelection(5)
        try await waitUntil { AnnotationProtocol.fixture.withLock { $0.held.contains { $0.path == "/api/annotations" } } }
        AnnotationProtocol.release(path: "/api/annotations")
        try await waitUntil { !model.annotationQueue.isSaving }
        #expect(model.annotationQueue.pendingCount == 0)
        AnnotationProtocol.release(path: "/api/sequences/saved/items/order")
        try await waitUntil { !model.isSaving }
        #expect(model.photos.map(\.id) == [2, 1])
        #expect(model.photos.first { $0.id == 1 }?.rating == 5)
    }

    @Test func anUndurableEditRemainsMarkedAfterDisconnectionAndRetry() async throws {
        let store = FailingAnnotationStore()
        let model = try await makeModel(store: store)
        defer { cleanup(model) }
        store.failNextSave = true
        model.rateSelection(5)
        model.shutdownService()
        model.retryAnnotations()
        #expect(model.annotationQueue.hasUndurableEdits && model.annotationQueue.pendingCount == 1)
        #expect(AnnotationProtocol.fixture.withLock { $0.patches.isEmpty })
    }

    @Test func aRatingAcknowledgmentDoesNotOverwriteAnUnrelatedExternalCaption() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        model.rateSelection(5)
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        AnnotationProtocol.fixture.withLock {
            $0.photos[1]?.caption = "Written elsewhere"
            $0.assetResponseIDs = [1]
            $0.holdAssetsCount = 1
        }
        model.navigate(to: .library(date: nil))
        try await waitUntil { AnnotationProtocol.fixture.withLock { $0.held.contains { $0.path == "/api/assets" } } }
        AnnotationProtocol.release(path: "/api/annotations")
        try await waitUntil { !model.annotationQueue.isSaving }
        AnnotationProtocol.release(path: "/api/assets")
        try await waitUntil { !model.isLoading }
        #expect(model.photos.first?.rating == 5)
        #expect(model.photos.first?.caption == "Written elsewhere")
    }

    @Test func malformedAcknowledgmentsDoNotDeletePendingEdits() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        AnnotationProtocol.fixture.withLock { $0.malformedAcknowledgment = true }
        model.rateSelection(5)
        try await waitUntil { AnnotationProtocol.fixture.withLock { !$0.held.isEmpty } }
        AnnotationProtocol.release()
        try await waitUntil { !model.isSaving }
        #expect(model.annotationQueue.pendingCount == 1 && model.annotationQueue.errorMessage != nil)
        AnnotationProtocol.fixture.withLock { $0.malformedAcknowledgment = false }
        model.retryAnnotations()
        try await waitUntil { !model.isSaving }
        #expect(model.annotationQueue.pendingCount == 0)
    }
}

@MainActor private final class FailingAnnotationStore: AnnotationDraftStore {
    var data: Data?
    var failNextSave = false
    func load() throws -> Data? { data }
    func save(_ data: Data) throws {
        if failNextSave {
            failNextSave = false
            throw ServiceConnectionError("Fixture disk failure")
        }
        self.data = data
    }
}
