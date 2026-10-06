import Foundation

private struct ServiceErrorResponse: Decodable { let message: String }
private struct AnnotationBody: Encodable, Sendable {
    let asset_ids: [Int]; let rating: Int?; let caption: String?; let flag: PhotoFlag?; let expected: AnnotationExpectation?
}

private struct SequenceItemsBody: Encodable, Sendable {
    let asset_ids: [Int]; let expected: AnnotationExpectation?
}

/// A nil destination means the root, so it must be encoded as JSON null.
private struct SequenceLocationBody: Encodable, Sendable {
    let destination: String?
    let folder: Bool
    let expectedDataID: String
    enum CodingKeys: String, CodingKey { case parent_id, folder_id, expected_data_id }
    func encode(to encoder: any Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(destination, forKey: folder ? .parent_id : .folder_id)
        try values.encode(expectedDataID, forKey: .expected_data_id)
    }
}

private final class LocalServiceSessionDelegate: NSObject, URLSessionTaskDelegate, Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

public enum LibraryError: LocalizedError, Sendable {
    case invalidServer
    case invalidResponse
    case server(String)

    public var errorDescription: String? {
        switch self {
        case .invalidServer: "Choose a local library service at http://127.0.0.1 or http://localhost."
        case .invalidResponse: "The library returned an unreadable response."
        case .server(let message): message
        }
    }
}

/// Only connects to the archive-safe loopback service, never directly to the archive.
public final class LibraryClient: Sendable {
    public let baseURL: URL
    private let session: URLSession

    public init(baseURL: URL, session: URLSession? = nil) throws {
        guard baseURL.scheme == "http",
              ["127.0.0.1", "localhost", "::1", "[::1]"].contains(baseURL.host ?? ""),
              baseURL.user == nil, baseURL.password == nil,
              baseURL.query == nil, baseURL.fragment == nil,
              baseURL.path.isEmpty || baseURL.path == "/" else {
            throw LibraryError.invalidServer
        }
        self.baseURL = baseURL
        if let session {
            self.session = session
        } else {
            let configuration = URLSessionConfiguration.ephemeral
            configuration.timeoutIntervalForRequest = 45
            configuration.timeoutIntervalForResource = 90
            configuration.httpMaximumConnectionsPerHost = 4
            configuration.urlCache = nil
            self.session = URLSession(configuration: configuration, delegate: LocalServiceSessionDelegate(), delegateQueue: nil)
        }
    }

    public func library() async throws -> LibrarySummary {
        try await get("api/library")
    }

    public func trashBatches() async throws -> [PhotoTrashBatch] {
        let response: PhotoTrashList = try await get("api/trash")
        return response.batches
    }

    public func trash(_ selection: PhotoTrashSelection) async throws -> PhotoTrashBatch {
        struct Body: Encodable, Sendable { let request_id: String; let asset_ids: [Int]; let expected: AnnotationExpectation }
        let response: PhotoTrashResponse = try await send("api/trash", method: "POST", body:
            Body(request_id: selection.id, asset_ids: selection.photos.map(\.id), expected:
                AnnotationExpectation(dataID: selection.dataID, assets: try selection.photos.map(AnnotationTarget.init(photo:)))))
        return response.batch
    }

    public func trashAction(id: String, restore: Bool, dataID: String) async throws -> PhotoTrashBatch {
        let action = restore ? "restore" : "resume"
        let response: PhotoTrashResponse = try await send("api/trash/\(id)/\(action)", method: "POST", body: ["expected_data_id": dataID])
        return response.batch
    }

    public func serviceIdentity() async throws -> ServiceIdentity {
        var request = URLRequest(url: baseURL.appendingPathComponent("health"))
        request.timeoutInterval = 1
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        do {
            let identity: ServiceIdentity = try await perform(request)
            try identity.validate()
            return identity
        } catch is DecodingError {
            throw ServiceConnectionError("This address serves another app or an older Latent service. Update or choose the correct service, then retry; it has not been stopped.")
        }
    }

    public func photos(date: String?, offset: Int = 0, filters: PhotoFilters = .init(),
                       collapseTimelapses: Bool = false, timelapseGroup: String? = nil,
                       importID: String? = nil) async throws -> PhotoPage {
        var query = [URLQueryItem(name: "limit", value: "250"),
                     URLQueryItem(name: "offset", value: String(offset))]
        if let date { query.append(URLQueryItem(name: "date", value: date)) }
        query += filters.queryItems
        if collapseTimelapses { query.append(.init(name: "collapse_timelapses", value: "1")) }
        if let timelapseGroup { query.append(.init(name: "timelapse_group", value: timelapseGroup)) }
        if let importID { query.append(.init(name: "import_id", value: importID)) }
        return try await get("api/assets", query: query)
    }

    public func imports() async throws -> ImportList { try await get("api/imports") }

    public func prepareImport(id: String, paths: [String], dataID: String) async throws -> ImportResponse {
        struct Body: Encodable, Sendable { let request_id: String; let paths: [String]; let expected_data_id: String }
        return try await send("api/imports", method: "POST",
            body: Body(request_id: id, paths: paths, expected_data_id: dataID))
    }

    public func importAction(_ batch: PhotoImport, action: String, dataID: String, path: String? = nil) async throws -> ImportResponse {
        struct Body: Encodable, Sendable { let expected_revision: String; let expected_data_id: String; let path: String? }
        return try await send("api/imports/\(batch.id)/\(action)", method: "POST",
            body: Body(expected_revision: batch.revision, expected_data_id: dataID, path: path))
    }

    public func embeddingRuns() async throws -> EmbeddingRunList { try await get("api/embedding-runs") }

    public func prepareEmbeddings(id: String, scope: String, ids: [Int]?, importID: String?, dataID: String) async throws -> EmbeddingRunResponse {
        struct Body: Encodable, Sendable {
            let request_id: String; let scope: String; let asset_ids: [Int]?; let batch_id: String?; let expected_data_id: String
        }
        return try await send("api/embedding-runs", method: "POST", body:
            Body(request_id: id, scope: scope, asset_ids: ids, batch_id: importID, expected_data_id: dataID))
    }

    public func embeddingAction(_ run: PhotoEmbeddingRun, action: String, dataID: String) async throws -> EmbeddingRunResponse {
        struct Body: Encodable, Sendable { let expected_revision: String; let expected_data_id: String }
        return try await send("api/embedding-runs/\(run.id)/\(action)", method: "POST",
            body: Body(expected_revision: run.revision, expected_data_id: dataID))
    }

    public func timelapses() async throws -> TimelapseResponse {
        try await get("api/timelapse")
    }

    public func auditTimelapses(expectedDataID: String) async throws -> TimelapseResponse {
        struct Body: Encodable, Sendable { let expected_data_id: String }
        return try await send("api/timelapse/audit", method: "POST", body: Body(expected_data_id: expectedDataID))
    }

    public func setTimelapse(_ group: TimelapseGroup, confirmed: Bool, expectedDataID: String) async throws -> TimelapseResponse {
        struct Body: Encodable, Sendable {
            let confirmed: Bool; let expected_revision: Int; let expected_data_id: String
        }
        return try await send("api/timelapse/\(group.id)", method: "PATCH",
            body: Body(confirmed: confirmed, expected_revision: group.revision, expected_data_id: expectedDataID))
    }

    public func search(_ query: String, variety: Bool) async throws -> SearchResults {
        try await search(query, order: variety ? .variety : .closest)
    }

    public func search(_ query: String, order: PhotoSearchOrder, filters: PhotoFilters = .init()) async throws -> SearchResults {
        try await get("api/search", query: [
            URLQueryItem(name: "q", value: query),
            URLQueryItem(name: "limit", value: "100"),
            URLQueryItem(name: "variety", value: order == .variety ? "1" : "0"),
            URLQueryItem(name: "order", value: order.rawValue),
        ] + filters.queryItems)
    }

    public func similar(to id: Int, order: PhotoSearchOrder = .closest, filters: PhotoFilters = .init()) async throws -> SearchResults {
        try await get("api/assets/\(id)/similar", query: [
            URLQueryItem(name: "limit", value: "100"), URLQueryItem(name: "order", value: order.rawValue),
        ] + filters.queryItems)
    }

    public func search(image: SearchReference, query: String, order: PhotoSearchOrder, filters: PhotoFilters = .init()) async throws -> SearchResults {
        struct Body: Encodable, Sendable {
            let image_base64: String; let query: String; let order: String; let limit: Int
            let image_name: String
            let filters: PhotoFilters
        }
        return try await send("api/search/image", method: "POST", body:
            Body(image_base64: image.jpeg.base64EncodedString(), query: query, order: order.rawValue,
                 limit: 100, image_name: String(image.name.prefix(255)), filters: filters))
    }

    public func searchHistory() async throws -> [SearchHistoryEntry] {
        struct Response: Decodable, Sendable { let entries: [SearchHistoryEntry] }
        let response: Response = try await get("api/search/history")
        return response.entries
    }

    public func replaySearch(id: String, filters: PhotoFilters, order: PhotoSearchOrder, dataID: String) async throws -> SearchResults {
        struct Body: Encodable, Sendable {
            let filters: PhotoFilters; let order: String; let expected_data_id: String
        }
        return try await send("api/search/history/\(id)/replay", method: "POST", body:
            Body(filters: filters, order: order.rawValue, expected_data_id: dataID))
    }

    public func deleteSearchHistory(id: String, dataID: String) async throws {
        struct Body: Encodable, Sendable { let expected_data_id: String }
        struct Response: Decodable, Sendable { let deleted: String }
        let _: Response = try await send("api/search/history/\(id)", method: "DELETE", body: Body(expected_data_id: dataID))
    }

    public func searchHistoryImageURL(id: String) -> URL {
        baseURL.appendingPathComponent("api/search/history").appendingPathComponent(id).appendingPathComponent("image")
    }

    public func searchHistoryReference(_ entry: SearchHistoryEntry) async throws -> SearchReference? {
        guard entry.kind == "image" else { return nil }
        let (data, response) = try await session.data(from: searchHistoryImageURL(id: entry.id))
        guard let response = response as? HTTPURLResponse, response.statusCode == 200,
              data.count <= 1_048_576 else { throw LibraryError.server("The saved reference image is unavailable.") }
        return SearchReference(name: entry.imageName ?? "Reference image", jpeg: data)
    }

    public func sequences() async throws -> [PhotoSequence] {
        try await sequenceLibrary().sequences
    }

    public func sequenceLibrary() async throws -> SequencesResponse {
        try await get("api/sequences")
    }

    public func sequence(id: String) async throws -> PhotoSequence {
        let response: SequenceResponse = try await get("api/sequences/\(id)")
        return response.sequence
    }

    public func createSequence(name: String, note: String, sequenceID: String? = nil,
                               expectedDataID: String? = nil, folderID: String? = nil,
                               smartFilters: PhotoFilters? = nil) async throws -> PhotoSequence {
        struct Body: Encodable, Sendable {
            let name: String; let note: String; let sequence_id: String?; let expected_data_id: String?; let folder_id: String?
            let smart_filters: PhotoFilters?
        }
        let response: SequenceResponse = try await send("api/sequences", method: "POST",
            body: Body(name: name, note: note, sequence_id: sequenceID, expected_data_id: expectedDataID, folder_id: folderID, smart_filters: smartFilters))
        return response.sequence
    }

    public func createSequenceFolder(name: String, parentID: String?, folderID: String, expectedDataID: String) async throws -> SequenceFolder {
        struct Body: Encodable, Sendable {
            let name: String; let parent_id: String?; let folder_id: String; let expected_data_id: String
        }
        let response: SequenceFolderResponse = try await send("api/sequence-folders", method: "POST",
            body: Body(name: name, parent_id: parentID, folder_id: folderID, expected_data_id: expectedDataID))
        return response.folder
    }

    public func renameSequenceFolder(id: String, name: String, expectedDataID: String) async throws -> SequenceFolder {
        let response: SequenceFolderResponse = try await send("api/sequence-folders/\(id)", method: "PATCH",
            body: ["name": name, "expected_data_id": expectedDataID])
        return response.folder
    }

    public func moveSequenceFolder(id: String, to parentID: String?, expectedDataID: String) async throws -> SequenceFolder {
        let response: SequenceFolderResponse = try await send("api/sequence-folders/\(id)", method: "PATCH",
            body: SequenceLocationBody(destination: parentID, folder: true, expectedDataID: expectedDataID))
        return response.folder
    }

    public func moveSequence(id: String, to folderID: String?, expectedDataID: String) async throws -> PhotoSequence {
        let response: SequenceResponse = try await send("api/sequences/\(id)", method: "PATCH",
            body: SequenceLocationBody(destination: folderID, folder: false, expectedDataID: expectedDataID))
        return response.sequence
    }

    public func removeSequenceFolder(id: String, expectedDataID: String) async throws {
        struct Removed: Decodable { let removed: Bool }
        let result: Removed = try await send("api/sequence-folders/\(id)", method: "DELETE",
            body: ["expected_data_id": expectedDataID])
        guard result.removed else { throw LibraryError.invalidResponse }
    }

    public func deleteSequence(id: String, expectedDataID: String) async throws {
        struct Deleted: Decodable { let deleted: Bool }
        let result: Deleted = try await send("api/sequences/\(id)", method: "DELETE",
            body: ["expected_data_id": expectedDataID])
        guard result.deleted else { throw LibraryError.invalidResponse }
    }

    public func add(photoIDs: [Int], to sequenceID: String, expected: AnnotationExpectation? = nil) async throws -> SequenceResponse {
        return try await send("api/sequences/\(sequenceID)/items", method: "POST", body: SequenceItemsBody(asset_ids: photoIDs, expected: expected))
    }

    public func sequenceAdditionSize(photoIDs: [Int], expected: AnnotationExpectation) throws -> Int {
        try JSONEncoder().encode(SequenceItemsBody(asset_ids: photoIDs, expected: expected)).count
    }

    public func updateSequence(id: String, name: String?, note: String?, expectedDataID: String? = nil,
                               smartFilters: PhotoFilters? = nil) async throws -> PhotoSequence {
        struct Body: Encodable, Sendable { let name: String?; let note: String?; let expected_data_id: String?; let smart_filters: PhotoFilters? }
        let response: SequenceResponse = try await send("api/sequences/\(id)", method: "PATCH",
            body: Body(name: name, note: note, expected_data_id: expectedDataID, smart_filters: smartFilters))
        return response.sequence
    }

    public func reorder(sequenceID: String, itemIDs: [String]) async throws -> PhotoSequence {
        let response: SequenceResponse = try await send("api/sequences/\(sequenceID)/items/order", method: "PUT",
                                                       body: ["item_ids": itemIDs])
        return response.sequence
    }

    public func remove(itemID: String, from sequenceID: String) async throws -> PhotoSequence {
        let response: SequenceResponse = try await send("api/sequences/\(sequenceID)/items/\(itemID)",
                                                       method: "DELETE", body: Optional<String>.none)
        return response.sequence
    }

    public func starred(offset: Int = 0) async throws -> PhotoPage {
        try await get("api/starred", query: [URLQueryItem(name: "offset", value: String(offset))])
    }

    public func annotate(ids: [Int], rating: Int? = nil, caption: String? = nil, flag: PhotoFlag? = nil,
                         expected: AnnotationExpectation? = nil) async throws -> [Photo] {
        let response: AnnotationResponse = try await send("api/annotations", method: "PATCH",
            body: AnnotationBody(asset_ids: ids, rating: rating, caption: caption, flag: flag, expected: expected))
        return response.assets
    }

    public func annotationRequestSize(ids: [Int], rating: Int?, caption: String?, flag: PhotoFlag? = nil, expected: AnnotationExpectation) throws -> Int {
        try JSONEncoder().encode(AnnotationBody(asset_ids: ids, rating: rating, caption: caption, flag: flag, expected: expected)).count
    }

    public func editingBatches() async throws -> [EditingBatch] {
        let response: EditingList = try await get("api/editing")
        return response.batches
    }

    public func editingBatch(id: String) async throws -> EditingBatch {
        let response: EditingResponse = try await get("api/editing/\(id)")
        return response.batch
    }

    public func beginEditing(ids: [Int]) async throws -> EditingBatch {
        let response: EditingResponse = try await send("api/editing", method: "POST", body: ["asset_ids": ids])
        return response.batch
    }

    public func editingAction(id: String, action: String, policy: String? = nil,
                              expectedDataID: String? = nil, expectedUpdatedAt: String? = nil,
                              exports: [ExportSelection]? = nil) async throws -> EditingBatch {
        struct Body: Encodable, Sendable {
            let cleanup_policy: String?; let expected_data_id: String?; let expected_updated_at: String?
            let exports: [ExportSelection]?
        }
        let response: EditingResponse = try await send("api/editing/\(id)/\(action)", method: "POST",
            body: Body(cleanup_policy: policy, expected_data_id: expectedDataID, expected_updated_at: expectedUpdatedAt, exports: exports))
        return response.batch
    }

    public func locations() async throws -> LibraryLocations { try await get("api/locations") }

    public func updateLocations(route: String = "", action: String? = nil, path: String? = nil,
                                sourceID: String? = nil, expected: LibraryLocations) async throws -> LibraryLocations {
        struct Body: Encodable, Sendable {
            let action: String?; let path: String?; let source_id: String?
            let expected_revision: String; let expected_data_id: String
        }
        return try await send("api/locations" + route, method: "POST",
            body: Body(action: action, path: path, source_id: sourceID,
                       expected_revision: expected.revision, expected_data_id: expected.dataId))
    }

    public func mediaURL(_ path: String) throws -> URL {
        guard path.hasPrefix("/media/"), !path.contains(".."),
              let url = URL(string: path, relativeTo: baseURL)?.absoluteURL,
              url.host == baseURL.host, url.port == baseURL.port,
              url.scheme == baseURL.scheme else { throw LibraryError.invalidResponse }
        return url
    }

    private func get<T: Decodable & Sendable>(_ path: String, query: [URLQueryItem] = []) async throws -> T {
        var components = URLComponents(url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        components.queryItems = query.isEmpty ? nil : query
        return try await perform(URLRequest(url: components.url!))
    }

    private func send<T: Decodable & Sendable, Body: Encodable & Sendable>(
        _ path: String, method: String, body: Body?
    ) async throws -> T {
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.httpMethod = method
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder().encode(body)
        }
        return try await perform(request)
    }

    private func perform<T: Decodable & Sendable>(_ request: URLRequest) async throws -> T {
        let (data, response) = try await session.data(for: request)
        try Task.checkCancellation()
        guard let response = response as? HTTPURLResponse else { throw LibraryError.invalidResponse }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        guard (200..<300).contains(response.statusCode) else {
            let message = (try? decoder.decode(ServiceErrorResponse.self, from: data).message)
                ?? "The library request failed (\(response.statusCode))."
            throw LibraryError.server(message)
        }
        return try decoder.decode(T.self, from: data)
    }
}
