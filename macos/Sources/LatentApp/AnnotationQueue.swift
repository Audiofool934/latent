import Foundation
import LatentCore
import Observation

/// Tests use an in-memory store. Only the app composition root chooses a disk location.
@MainActor protocol AnnotationDraftStore {
    func load() throws -> Data?
    func save(_ data: Data) throws
}

@MainActor final class MemoryAnnotationDraftStore: AnnotationDraftStore {
    var data: Data?
    func load() throws -> Data? { data }
    func save(_ data: Data) throws { self.data = data }
}

@MainActor final class FileAnnotationDraftStore: AnnotationDraftStore {
    let url: URL
    init(url: URL) { self.url = url }
    func load() throws -> Data? {
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        return try Data(contentsOf: url)
    }
    func save(_ data: Data) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: url, options: .atomic)
    }
}

private struct AnnotationValue<Value: Codable & Equatable & Sendable>: Codable, Sendable {
    let value: Value
    let revision: UUID
    init(_ value: Value) { self.value = value; revision = UUID() }
}

private struct AnnotationDraft: Codable, Sendable {
    let target: AnnotationTarget
    var rating: AnnotationValue<Int>?
    var caption: AnnotationValue<String>?
    var flag: AnnotationValue<PhotoFlag>?
    var isEmpty: Bool { rating == nil && caption == nil && flag == nil }
}

private struct AnnotationDocument: Codable, Sendable {
    var version: Int
    let endpoint: String
    let dataID: String
    var edits: [AnnotationDraft]
}

/// A write-ahead snapshot of desired fields, not an operation history.
/// Requests leave their fields in the snapshot until those exact revisions are acknowledged.
@MainActor @Observable final class AnnotationQueue {
    private(set) var isSaving = false
    private(set) var errorMessage: String?
    private(set) var hasUndurableEdits = false
    private var document: AnnotationDocument?
    private var paused = false
    private var unreadableDraft = false
    @ObservationIgnored private let store: any AnnotationDraftStore
    @ObservationIgnored private let client: LibraryClient
    @ObservationIgnored private let service: ServiceConnection
    @ObservationIgnored private var worker: Task<Void, Never>?
    @ObservationIgnored var didSave: (([Photo], Bool, Bool, Bool, Bool) -> Void)?

    init(client: LibraryClient, service: ServiceConnection, store: any AnnotationDraftStore) {
        self.client = client
        self.service = service
        self.store = store
        restore()
    }

    var pendingCount: Int { document?.edits.count ?? 0 }
    var hasPendingEdits: Bool { pendingCount > 0 }
    var needsAttention: Bool { errorMessage != nil || hasPendingEdits }
    var canDiscard: Bool { !isSaving && (hasPendingEdits || unreadableDraft) }

    private var endpoint: String { client.baseURL.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/")) }

    private func restore() {
        do {
            if let data = try store.load() {
                let restored = try JSONDecoder().decode(AnnotationDocument?.self, from: data)
                if let restored {
                    guard [1, 2].contains(restored.version),
                          Set(restored.edits.map { $0.target.id }).count == restored.edits.count,
                          restored.edits.allSatisfy({ !$0.isEmpty && $0.target.isValid &&
                              ($0.rating.map { (0...5).contains($0.value) } ?? true) &&
                              ($0.caption.map { $0.value.count <= 2000 } ?? true) }) else {
                        throw LibraryError.invalidResponse
                    }
                }
                document = restored?.edits.isEmpty == false ? restored : nil
            }
            unreadableDraft = false
            paused = hasPendingEdits
            errorMessage = hasPendingEdits ? "Edits from your last session are kept on this Mac. Retry saving when this library is ready." : nil
        } catch {
            unreadableDraft = true
            paused = true
            errorMessage = "The saved edits could not be read. The draft file has been preserved. \(error.localizedDescription)"
        }
    }

    @discardableResult func enqueue(photos: [Photo], rating: Int?, caption: String?, flag: PhotoFlag? = nil) -> Bool {
        guard !photos.isEmpty, rating != nil || caption != nil || flag != nil else { return false }
        guard !unreadableDraft else { return false }
        do {
            guard rating.map({ (0...5).contains($0) }) ?? true,
                  caption.map({ $0.count <= 2000 }) ?? true else {
                throw ServiceConnectionError("Use a rating from 0 to 5 and a caption of at most 2,000 characters.")
            }
            let dataID = try checkedDataID()
            let targets = try photos.map(AnnotationTarget.init(photo:))
            var next = document ?? AnnotationDocument(version: 2, endpoint: endpoint, dataID: dataID, edits: [])
            next.version = 2
            for target in targets {
                let index = next.edits.firstIndex { $0.target.id == target.id }
                if let index, next.edits[index].target != target {
                    throw ServiceConnectionError("A photo changed in the library. Keep or discard its pending edits before rating it again.")
                }
                var edit = index.map { next.edits[$0] } ?? AnnotationDraft(target: target)
                if let rating { edit.rating = AnnotationValue(rating) }
                if let caption { edit.caption = AnnotationValue(caption) }
                if let flag { edit.flag = AnnotationValue(flag) }
                if let index { next.edits[index] = edit } else { next.edits.append(edit) }
            }
            // Retain intent in memory even if disk persistence fails, and never dispatch it then.
            document = next
            if persist(next) { beginSaving() }
            return true
        } catch {
            pause(error.localizedDescription)
            return false
        }
    }

    func overlay(_ photos: [Photo]) -> [Photo] {
        guard let document, document.endpoint == endpoint,
              document.dataID == service.identity?.dataId else { return photos }
        let edits = Dictionary(uniqueKeysWithValues: document.edits.map { ($0.target.id, $0) })
        return photos.map { photo in
            guard let edit = edits[photo.id], edit.target.matches(photo) else { return photo }
            var result = photo
            if let rating = edit.rating { result.rating = rating.value }
            if let caption = edit.caption { result.caption = caption.value }
            if let flag = edit.flag { result.flag = flag.value }
            return result
        }
    }

    func retry() {
        guard !isSaving else { return }
        if unreadableDraft { restore() }
        guard !unreadableDraft else { return }
        do {
            _ = try checkedDataID()
            guard persist(document) else { return }
            errorMessage = nil
            paused = false
            beginSaving()
        } catch { pause(error.localizedDescription) }
    }

    @discardableResult func discard() -> Bool {
        guard canDiscard else { return false }
        guard persist(nil) else { return false }
        document = nil
        unreadableDraft = false
        paused = false
        errorMessage = nil
        return true
    }

    func connectionLost() {
        guard hasPendingEdits else { return }
        // A request may have committed before a disconnection; replaying desired fields is safe.
        worker?.cancel()
        pause("Your pending edits are kept for retry. Choose Retry saving when the library is ready.")
    }

    private func checkedDataID() throws -> String {
        guard service.isConnected, let identity = service.identity else {
            throw ServiceConnectionError("Reconnect to the library, then retry saving your edits.")
        }
        if let document, !document.edits.isEmpty,
           document.endpoint != endpoint || document.dataID != identity.dataId {
            throw ServiceConnectionError("These pending edits belong to another library. Reconnect to that library or discard the pending edits.")
        }
        return identity.dataId
    }

    private func persist(_ next: AnnotationDocument?) -> Bool {
        do {
            try store.save(JSONEncoder().encode(next))
            hasUndurableEdits = false
            return true
        } catch {
            hasUndurableEdits = true
            pause("Edits could not be kept on this Mac. Keep Latent open and retry saving. \(error.localizedDescription)")
            return false
        }
    }

    private func pause(_ message: String) { paused = true; errorMessage = message }

    private func beginSaving() {
        guard !paused, !isSaving, hasPendingEdits else { return }
        isSaving = true
        worker = Task {
            defer { isSaving = false; worker = nil }
            while !paused, !Task.isCancelled, let first = document?.edits.first {
                do {
                    let dataID = try checkedDataID()
                    let candidates = Array(document!.edits.filter {
                        $0.rating?.value == first.rating?.value && $0.caption?.value == first.caption?.value
                            && $0.flag?.value == first.flag?.value
                    }.prefix(500))
                    let batch = try boundedBatch(candidates, dataID: dataID)
                    let updated = try await client.annotate(ids: batch.map { $0.target.id },
                        rating: first.rating?.value, caption: first.caption?.value, flag: first.flag?.value,
                        expected: AnnotationExpectation(dataID: dataID, assets: batch.map(\.target)))
                    try Task.checkCancellation()
                    guard updated.count == batch.count,
                          Set(updated.map(\.id)).count == updated.count,
                          batch.allSatisfy({ sent in updated.contains(where: sent.target.matches) }) else {
                        throw LibraryError.invalidResponse
                    }
                    guard var next = document else { return }
                    for sent in batch {
                        guard let index = next.edits.firstIndex(where: { $0.target == sent.target }) else { continue }
                        if next.edits[index].rating?.revision == sent.rating?.revision { next.edits[index].rating = nil }
                        if next.edits[index].caption?.revision == sent.caption?.revision { next.edits[index].caption = nil }
                        if next.edits[index].flag?.revision == sent.flag?.revision { next.edits[index].flag = nil }
                    }
                    next.edits.removeAll(where: \.isEmpty)
                    if persist(next.edits.isEmpty ? nil : next) {
                        document = next.edits.isEmpty ? nil : next
                        didSave?(updated, !hasPendingEdits, first.rating != nil, first.caption != nil, first.flag != nil)
                    } else {
                        didSave?(updated, false, first.rating != nil, first.caption != nil, first.flag != nil)
                    }
                } catch {
                    if !Task.isCancelled { pause("Could not save your edits. \(error.localizedDescription) Retry saving when ready.") }
                    break
                }
            }
        }
    }

    private func boundedBatch(_ candidates: [AnnotationDraft], dataID: String) throws -> [AnnotationDraft] {
        var lower = 1
        var upper = candidates.count
        var count = 0
        while lower <= upper {
            let midpoint = (lower + upper) / 2
            let batch = Array(candidates.prefix(midpoint))
            let size = try client.annotationRequestSize(ids: batch.map { $0.target.id },
                rating: batch[0].rating?.value, caption: batch[0].caption?.value, flag: batch[0].flag?.value,
                expected: AnnotationExpectation(dataID: dataID, assets: batch.map(\.target)))
            if size <= 64 * 1024 { count = midpoint; lower = midpoint + 1 }
            else { upper = midpoint - 1 }
        }
        guard count > 0 else { throw ServiceConnectionError("A photo edit exceeds the library's request size limit.") }
        return Array(candidates.prefix(count))
    }
}
