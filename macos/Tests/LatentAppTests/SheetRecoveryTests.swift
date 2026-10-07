import Foundation
import LatentCore
import Synchronization
import Testing
@testable import LatentApp

private let sheetPhotoJSON = """
{"id":1,"name":"fixture.ARW","remote_path":"/fixture.ARW","provider":"fixture","fingerprint":"fixture-hash",
 "size_bytes":1024,"contact_url":"/media/fixture.jpg","preview_url":"/media/fixture.jpg",
 "preview_available":false,"rating":0,"caption":""}
"""

private struct SheetSequence: Sendable {
    let id: String
    var name: String
    var note: String
    var photoIDs: [Int] = []
    var folderID: String?
    var json: [String: Any] {
        ["id": id, "name": name, "note": note, "item_count": photoIDs.count,
         "folder_id": folderID as Any? ?? NSNull(),
         "items": photoIDs.map { ["id": "item-\($0)", "name": "fixture.ARW", "asset": try! JSONSerialization.jsonObject(with: Data(sheetPhotoJSON.utf8))] }]
    }
}

private struct SheetFixture: Sendable {
    var ready = true
    var dataID = "sheet-fixture"
    var sequences: [String: SheetSequence] = [:]
    var requests: [String] = []
    var createFailuresAfterCommit = 0
    var rejectedName: String?
    var createIDs: [String] = []
    var createNames: [String] = []
    var foldersJSON: [String] = []
    var addedBatchSizes: [Int] = []
    var addFailures = 0
    var updateFailuresAfterCommit = 0
    var deleteFailures = 0
    var prepareFailuresAfterCommit = 0
    var batchDestination = "/synthetic-only/upload"
    var finishFailuresAfterCommit = 0
    var batchStatus = "prepared"
    var batchRevision = 1
    var held: [SheetWork] = []
    var holdPath: String?

    var batch: [String: Any] {
        ["id": "batch-fixture", "created_at": "2026-10-02T00:00:00Z", "status": batchStatus,
         "updated_at": "revision-\(batchRevision)", "folder": "/synthetic-only/batch", "items": [], "uploads": [], "bytes_done": 0,
         "bytes_total": 0, "upload_destination": batchDestination, "phase": "upload"]
    }

    mutating func response(_ request: URLRequest) -> (Data, Int, Bool) {
        let path = request.url!.path
        let method = request.httpMethod ?? "GET"
        requests.append("\(method) \(path)")
        var body: [String: Any] = [:]
        if method != "GET" { body = (try? JSONSerialization.jsonObject(with: SheetProtocol.body(request))) as? [String: Any] ?? [:] }
        var object: [String: Any]
        var status = 200
        if path == "/health" {
            object = ["status": "ok", "service": "latent", "protocol_version": ready ? 1 : 2,
                      "service_version": "fixture", "instance_id": "f699d3ac-607a-4c96-9bf9-14cd7206feaa", "pid": 42, "data_id": dataID]
        } else if path == "/api/library" {
            object = ["dates": [], "cached_assets": 1, "workspace": ["writable": true]]
        } else if path.hasPrefix("/api/sequence-folders/"), method == "PATCH" {
            let id = request.url!.lastPathComponent
            let index = foldersJSON.firstIndex { text in
                let folder = try! JSONSerialization.jsonObject(with: Data(text.utf8)) as! [String: Any]
                return folder["id"] as? String == id
            }!
            var folder = try! JSONSerialization.jsonObject(with: Data(foldersJSON[index].utf8)) as! [String: Any]
            folder["parent_id"] = body["parent_id"]
            foldersJSON[index] = String(data: try! JSONSerialization.data(withJSONObject: folder), encoding: .utf8)!
            object = ["folder": folder]
        } else if path == "/api/sequences", method == "POST" {
            let id = body["sequence_id"] as? String ?? UUID().uuidString
            let name = body["name"] as? String ?? ""
            createIDs.append(id)
            createNames.append(name)
            if name == rejectedName {
                return (try! JSONSerialization.data(withJSONObject: ["message": "This name was rejected before saving"]), 400, false)
            }
            if sequences[id] == nil { sequences[id] = SheetSequence(id: id, name: body["name"] as? String ?? "", note: body["note"] as? String ?? "", folderID: body["folder_id"] as? String) }
            object = ["sequence": sequences[id]!.json]
            if createFailuresAfterCommit > 0 {
                createFailuresAfterCommit -= 1
                status = 500
                object = ["message": "Response lost after synthetic create"]
            }
        } else if path == "/api/sequences" {
            object = ["sequences": sequences.values.map(\.json), "folders": foldersJSON.map { try! JSONSerialization.jsonObject(with: Data($0.utf8)) }]
        } else if path.hasPrefix("/api/sequences/") {
            let parts = path.split(separator: "/")
            let id = String(parts[2])
            if method == "DELETE", parts.count == 3 {
                if body["expected_data_id"] as? String != dataID {
                    status = 400; object = ["message": "Wrong library for deletion"]
                } else if deleteFailures > 0 {
                    deleteFailures -= 1; status = 500; object = ["message": "Synthetic deletion failed"]
                } else {
                    sequences.removeValue(forKey: id)
                    object = ["deleted": true]
                }
            } else if parts.last == "items" {
                let count = (body["asset_ids"] as? [Int] ?? []).count
                addedBatchSizes.append(count)
                if count > 100 { return (Data("{\"message\":\"At most 100 photos per request\"}".utf8), 400, false) }
                if addFailures > 0 {
                    addFailures -= 1
                    status = 500
                    object = ["message": "Synthetic add failed"]
                } else if var sequence = sequences[id] {
                    for photoID in body["asset_ids"] as? [Int] ?? [] where !sequence.photoIDs.contains(photoID) { sequence.photoIDs.append(photoID) }
                    sequences[id] = sequence
                    object = ["sequence": sequence.json, "added": 1, "skipped": 0]
                } else { status = 404; object = ["message": "Missing sequence"] }
            } else if var sequence = sequences[id] {
                if let name = body["name"] as? String { sequence.name = name }
                if let note = body["note"] as? String { sequence.note = note }
                if body.keys.contains("folder_id") { sequence.folderID = body["folder_id"] as? String }
                sequences[id] = sequence
                object = ["sequence": sequence.json]
                if method == "PATCH", updateFailuresAfterCommit > 0 {
                    updateFailuresAfterCommit -= 1; status = 500
                    object = ["message": "Synthetic update response lost"]
                }
            } else { status = 404; object = ["message": "Missing sequence"] }
        } else if path == "/api/editing" {
            object = ["batches": [batch]]
        } else if path == "/api/editing/batch-fixture/prepare" {
            batchRevision += 1
            batchDestination = "/synthetic-only/upload-\(batchRevision)"
            batchStatus = "prepared"
            object = ["batch": batch]
            if prepareFailuresAfterCommit > 0 {
                prepareFailuresAfterCommit -= 1; status = 500
                object = ["message": "Synthetic prepare response lost"]
            }
        } else if path == "/api/editing/batch-fixture/finish" {
            if batchStatus == "prepared" { batchStatus = "uploading"; batchRevision += 1 }
            else { status = 400 }
            object = ["batch": batch]
            if finishFailuresAfterCommit > 0 { finishFailuresAfterCommit -= 1; status = 500 }
            if status != 200 { object = ["message": "Synthetic finish response unavailable"] }
        } else if path == "/api/editing/batch-fixture" {
            object = ["batch": batch]
        } else {
            object = ["assets": [try! JSONSerialization.jsonObject(with: Data(sheetPhotoJSON.utf8))],
                      "total": 1, "has_more": false, "next_offset": NSNull()]
        }
        return (try! JSONSerialization.data(withJSONObject: object), status, path == holdPath)
    }
}

private struct SheetWork: @unchecked Sendable { let item: DispatchWorkItem }

private final class SheetProtocol: URLProtocol, @unchecked Sendable {
    static let fixture = Mutex(SheetFixture())
    private let work = Mutex<SheetWork?>(nil)
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let (data, status, hold) = Self.fixture.withLock { $0.response(request) }
        let item = DispatchWorkItem { [weak self] in
            guard let self else { return }
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status,
                                                                httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        }
        work.withLock { $0 = SheetWork(item: item) }
        if hold { Self.fixture.withLock { $0.held.append(SheetWork(item: item)) } }
        else { DispatchQueue.global().async(execute: item) }
    }
    override func stopLoading() { work.withLock { $0?.item.cancel() } }

    static func release() {
        let held = fixture.withLock { value in let held = value.held; value.held = []; value.holdPath = nil; return held }
        for response in held where !response.item.isCancelled { DispatchQueue.global().async(execute: response.item) }
    }

    static func body(_ request: URLRequest) -> Data {
        if let data = request.httpBody { return data }
        guard let stream = request.httpBodyStream else { return Data() }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            data.append(contentsOf: buffer.prefix(count))
        }
        return data
    }
}

@MainActor @Suite(.serialized)
struct SheetRecoveryTests {
    @Test func dropsUseCurrentHierarchyAndLibraryThenPersistBothKindsOfMove() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        SheetProtocol.fixture.withLock {
            $0.foldersJSON = [
                "{\"id\":\"parent\",\"name\":\"Projects\",\"parent_id\":null}",
                "{\"id\":\"child\",\"name\":\"Child\",\"parent_id\":\"parent\"}",
                "{\"id\":\"loose\",\"name\":\"Loose\",\"parent_id\":null}"
            ]
        }
        let sequence = try await model.client.createSequence(name: "Study", note: "Keep metadata")
        await model.refreshSequences()
        SheetProtocol.fixture.withLock { $0.requests = [] }
        let payload = SequenceDragPayload(itemID: "sequence:\(sequence.id)", dataID: "sheet-fixture")
        #expect(model.itemForSequenceDrop(payload, to: "missing") == nil)
        #expect(model.itemForSequenceDrop(payload, to: nil) == nil)
        #expect(!model.acceptSequenceDrop([SequenceDragPayload(itemID: payload.itemID, dataID: "other")], to: "parent"))
        #expect(!model.acceptSequenceDrop([SequenceDragPayload(itemID: "sequence:deleted", dataID: "sheet-fixture")], to: "parent"))
        #expect(!model.acceptSequenceDrop([SequenceDragPayload(itemID: "folder:parent", dataID: "sheet-fixture")], to: "child"))
        #expect(!model.acceptSequenceDrop([SequenceDragPayload(itemID: "folder:parent", dataID: "sheet-fixture")], to: "parent"))
        #expect(SheetProtocol.fixture.withLock { $0.requests.isEmpty })
        #expect(model.acceptSequenceDrop([payload], to: "child"))
        try await waitUntil { !model.isSaving && model.sequences.first?.folderId == "child" }
        #expect(model.sequences.first?.note == "Keep metadata")
        #expect(model.expandedSequenceFolders == ["parent", "child"])
        #expect(model.sequenceHierarchy.visibleRows(expanded: model.expandedSequenceFolders).contains {
            $0.id == payload.itemID && $0.depth == 2
        })
        #expect(!model.acceptSequenceDrop([payload], to: "child"))
        #expect(model.acceptSequenceDrop([SequenceDragPayload(itemID: "folder:loose", dataID: "sheet-fixture")], to: "child"))
        try await waitUntil { !model.isSaving && model.sequenceHierarchy.folders["loose"]?.parentId == "child" }
        #expect(model.acceptSequenceDrop([payload], to: nil))
        try await waitUntil { !model.isSaving && model.sequences.first?.folderId == nil }
        #expect(SheetProtocol.fixture.withLock { $0.requests.filter { $0.hasPrefix("PATCH") }.count } == 3)
    }

    @Test func deletingAnOpenSequenceReturnsToItsFolderAndClearsItsDraft() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        SheetProtocol.fixture.withLock { $0.foldersJSON = ["{\"id\":\"folder\",\"name\":\"Projects\",\"parent_id\":null}"] }
        let sequence = try await model.client.createSequence(name: "Temporary study", note: "", folderID: "folder")
        await model.refreshSequences()
        model.navigate(to: .sequence(id: sequence.id))
        try await waitUntil { !model.isLoading }
        model.openSequenceEditor(editing: sequence)
        await model.deleteSequence(sequence, dataID: "sheet-fixture")
        try await waitUntil { model.sequences.isEmpty }
        #expect(model.showingSequences && model.selectedSequenceFolderID == "folder")
        #expect(model.activeSequence == nil && model.photos.isEmpty && model.previewPhoto == nil)
        #expect(model.sequenceEditor == nil && model.pendingSequenceDrafts.isEmpty)
        #expect(model.errorMessage == nil)
    }

    @Test func deletionFailureKeepsSequenceAndOldLibraryConfirmationCannotDelete() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        let sequence = try await model.client.createSequence(name: "Keep until confirmed", note: "")
        await model.refreshSequences()
        SheetProtocol.fixture.withLock { $0.requests = []; $0.deleteFailures = 1 }
        await model.deleteSequence(sequence, dataID: "another-library")
        #expect(SheetProtocol.fixture.withLock { $0.requests.isEmpty })
        await model.deleteSequence(sequence, dataID: "sheet-fixture")
        #expect(model.sequences.map(\.id) == [sequence.id])
        #expect(model.errorMessage == "Synthetic deletion failed")
        #expect(!model.isSaving)
    }

    @Test func pendingDeletionDoesNotReplaceNewerGalleryNavigation() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        let sequence = try await model.client.createSequence(name: "Temporary study", note: "")
        await model.refreshSequences()
        model.navigate(to: .sequence(id: sequence.id))
        try await waitUntil { !model.isLoading }
        SheetProtocol.fixture.withLock { $0.holdPath = "/api/sequences/\(sequence.id)" }
        let deletion = Task { await model.deleteSequence(sequence, dataID: "sheet-fixture") }
        try await waitUntil { SheetProtocol.fixture.withLock { !$0.held.isEmpty } }
        model.navigate(to: .library(date: "2026-01"))
        try await waitUntil { !model.isLoading }
        SheetProtocol.release()
        await deletion.value
        #expect(model.source == .library(date: "2026-01") && !model.showingSequences)
        #expect(model.photos.map(\.id) == [1])
        #expect(model.sequences.isEmpty)
    }

    private func makeModel(ready: Bool = true) async throws -> LibraryModel {
        SheetProtocol.fixture.withLock { $0 = SheetFixture(ready: ready) }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [SheetProtocol.self]
        let client = try LibraryClient(baseURL: URL(string: "http://localhost:8766")!, session: URLSession(configuration: configuration))
        let model = LibraryModel(client: client, preferences: MemoryPreferences())
        await model.start()
        try await waitUntil { !model.isLoading }
        return model
    }

    private func waitUntil(_ condition: () -> Bool) async throws {
        let deadline = ContinuousClock.now.advanced(by: .seconds(3))
        while !condition() {
            guard ContinuousClock.now < deadline else { throw ServiceConnectionError("Sheet fixture did not settle") }
            try await Task.sleep(for: .milliseconds(2))
        }
    }

    private func cleanup(_ model: LibraryModel) {
        model.shutdownService()
        SheetProtocol.release()
    }

    private func photo() throws -> Photo {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(Photo.self, from: Data(sheetPhotoJSON.utf8))
    }

    private func batch() throws -> EditingBatch {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(EditingBatch.self, from: SheetProtocol.fixture.withLock { try! JSONSerialization.data(withJSONObject: $0.batch) })
    }

    @Test func sequenceDraftKeepsItsFolderWhenNavigationChangesAndChunksLargeSelections() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let selected = try (1...205).map { id in
            try decoder.decode(Photo.self, from: Data(sheetPhotoJSON.replacingOccurrences(of: "\"id\":1", with: "\"id\":\(id)")
                .replacingOccurrences(of: "/fixture.ARW", with: "/fixture-\(id).ARW").utf8))
        }
        let draft = model.sequenceDraft(addingPhotos: selected, folderID: "chosen-folder")
        draft.name = "Large selection"
        model.selectedSequenceFolderID = "another-folder"
        #expect(await model.saveSequence(draft))
        #expect(SheetProtocol.fixture.withLock { $0.sequences[draft.creationID]?.folderID } == "chosen-folder")
        #expect(SheetProtocol.fixture.withLock { $0.sequences[draft.creationID]?.photoIDs } == Array(1...205))
        #expect(SheetProtocol.fixture.withLock { $0.addedBatchSizes } == [100, 100, 5])
    }

    @Test func folderExpansionPersistsPerLibraryAndOldFolderDraftsCannotWriteAfterReconnect() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        SheetProtocol.fixture.withLock { $0.foldersJSON = ["{\"id\":\"folder\",\"name\":\"Projects\",\"parent_id\":null}"] }
        await model.refreshSequences()
        model.toggleSequenceFolder("folder")
        model.openFolderEditor(parentID: "folder")
        let draft = try #require(model.folderEditor)
        draft.name = "Belongs to original library"
        let restored = LibraryModel(client: model.client, preferences: model.preferences)
        await restored.start()
        #expect(restored.expandedSequenceFolders == ["folder"])
        restored.shutdownService()
        model.shutdownService()
        SheetProtocol.fixture.withLock { $0.dataID = "another-library"; $0.requests = [] }
        let replacement = LibraryModel(client: model.client, preferences: model.preferences)
        defer { replacement.shutdownService() }
        await replacement.start()
        #expect(replacement.expandedSequenceFolders.isEmpty)
        await replacement.saveFolder(draft)
        #expect(draft.error?.contains("owns these folders") == true)
        #expect(SheetProtocol.fixture.withLock { $0.requests.allSatisfy { !$0.hasPrefix("POST") && !$0.hasPrefix("PATCH") } })
        replacement.shutdownService()
        SheetProtocol.fixture.withLock { $0.dataID = "sheet-fixture" }
        let original = LibraryModel(client: model.client, preferences: model.preferences)
        defer { original.shutdownService() }
        await original.start()
        #expect(original.expandedSequenceFolders == ["folder"])
    }

    @Test func offlineSequenceSaveExplainsWhyTheDraftCannotBeSaved() async throws {
        let model = try await makeModel(ready: false)
        defer { cleanup(model) }
        #expect(await !model.saveSequence(name: "Keep my draft", note: "Remember this", editing: nil, adding: nil))
        #expect(model.errorMessage?.localizedCaseInsensitiveContains("reconnect") == true)
        #expect(SheetProtocol.fixture.withLock { $0.requests.filter { $0.hasPrefix("POST") }.isEmpty })
    }

    @Test(arguments: [false, true])
    func disconnectedSidebarCanReopenEditingWithoutSendingRequests(hasRetainedReview: Bool) async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        if hasRetainedReview {
            await model.refreshEditing()
            model.setEditingPolicy("keep_exports", for: try #require(model.batches.first))
        }
        model.showingEditing = true
        model.shutdownService()
        model.showingEditing = false
        let requests = SheetProtocol.fixture.withLock { $0.requests }
        let available = SidebarAccess.retainedEditing.isEnabled(isConnected: model.service.isConnected)
        #expect(available)
        #expect(!SidebarAccess.liveLibrary.isEnabled(isConnected: model.service.isConnected))
        if available { model.showingEditing = true }
        #expect(model.showingEditing)
        await model.refreshEditing()
        #expect(SheetProtocol.fixture.withLock { $0.requests } == requests)
        #expect(!model.workspaceWritable)
        if hasRetainedReview {
            #expect(model.editingPolicy(for: try #require(model.batches.first)) == "keep_exports")
        } else { #expect(model.batches.isEmpty) }
    }

    @Test func newSequenceKeepsEntireOrderedSelectionAcrossSelectionChangesAndRetry() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        model.photos = try [3, 1, 2].map { id in
            try decoder.decode(Photo.self, from: Data(sheetPhotoJSON.replacingOccurrences(of: "\"id\":1", with: "\"id\":\(id)")
                .replacingOccurrences(of: "/fixture.ARW", with: "/fixture-\(id).ARW").utf8))
        }
        model.selectPhoto(3)
        model.selectPhoto(2, extending: true)
        #expect(model.selectedIDs == [3, 1, 2])
        model.openSequenceEditorForSelection()
        let draft = try #require(model.sequenceEditor)
        draft.name = "Whole selection"
        model.selectPhoto(1)
        SheetProtocol.fixture.withLock { $0.addFailures = 1 }
        #expect(await !model.saveSequence(draft))
        #expect(await model.saveSequence(draft))
        #expect(SheetProtocol.fixture.withLock { $0.sequences.count } == 1)
        #expect(SheetProtocol.fixture.withLock { $0.sequences.values.first?.photoIDs } == [3, 1, 2])
    }

    @Test func photoAddFailureDoesNotCreateAnotherSequenceOnRetry() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        SheetProtocol.fixture.withLock { $0.addFailures = 1 }
        let photo = try photo()
        #expect(await !model.saveSequence(name: "Draft", note: "Keep", editing: nil, adding: photo))
        #expect(await model.saveSequence(name: "Draft", note: "Keep", editing: nil, adding: photo))
        #expect(SheetProtocol.fixture.withLock { $0.sequences.count } == 1)
        #expect(SheetProtocol.fixture.withLock { $0.sequences.values.first?.photoIDs } == [1])
    }

    @Test func aLostCreationResponseDoesNotCreateAnotherSequenceOnRetry() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        SheetProtocol.fixture.withLock { $0.createFailuresAfterCommit = 1 }
        #expect(await !model.saveSequence(name: "Draft", note: "Keep", editing: nil, adding: nil))
        #expect(await model.saveSequence(name: "Draft", note: "Keep", editing: nil, adding: nil))
        #expect(SheetProtocol.fixture.withLock { $0.sequences.count } == 1)
    }

    @Test func offlineEditingActionExplainsWhyNothingWasSent() async throws {
        let model = try await makeModel(ready: false)
        defer { cleanup(model) }
        model.editAction(try batch(), action: "prepare")
        #expect(model.errorMessage?.localizedCaseInsensitiveContains("reconnect") == true)
        #expect(SheetProtocol.fixture.withLock { $0.requests.filter { $0.hasPrefix("POST") }.isEmpty })
    }

    @Test func anAcceptedFinishIsNotRepeatedAfterItsResponseWasLost() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        let draftBatch = try batch()
        model.batches = [draftBatch]
        SheetProtocol.fixture.withLock { $0.finishFailuresAfterCommit = 1 }
        model.editAction(draftBatch, action: "finish", policy: "keep_exports")
        try await waitUntil { !model.editingBusy }
        model.editAction(draftBatch, action: "finish", policy: "keep_exports")
        try await waitUntil { !model.editingBusy }
        #expect(SheetProtocol.fixture.withLock { $0.requests.filter { $0 == "POST /api/editing/batch-fixture/finish" }.count } == 1)
        #expect(model.batches.first?.status == "uploading")
    }

    @Test func anOfflineDraftSurvivesDismissReopenAndRepeatedConnectionRetry() async throws {
        let model = try await makeModel(ready: false)
        defer { cleanup(model) }
        model.openSequenceEditor()
        let draft = try #require(model.sequenceEditor)
        draft.name = "Keep this name"
        draft.note = "Keep this note"
        #expect(await !model.saveSequence(draft))
        #expect(draft.errorMessage?.localizedCaseInsensitiveContains("reconnect") == true)
        model.sequenceEditor = nil
        model.openSequenceEditor()
        #expect(model.sequenceEditor?.id == draft.id)
        #expect(model.sequenceEditor?.name == "Keep this name" && model.sequenceEditor?.note == "Keep this note")
        SheetProtocol.fixture.withLock { $0.ready = true }
        async let first: Void = model.reconnectSheets()
        async let second: Void = model.reconnectSheets()
        _ = await (first, second)
        #expect(model.service.isConnected)
        #expect(SheetProtocol.fixture.withLock { $0.requests.filter { $0.hasPrefix("POST") }.isEmpty })
        #expect(await model.saveSequence(draft))
        #expect(await model.saveSequence(draft))
        #expect(SheetProtocol.fixture.withLock { $0.requests.filter { $0 == "POST /api/sequences" }.count } == 1)
        #expect(model.sequenceEditor == nil && model.pendingSequenceDrafts.isEmpty)
    }

    @Test func aLateSaveCannotCloseAnotherDraftOrDuplicateTheFirst() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        model.openSequenceEditor()
        let first = try #require(model.sequenceEditor)
        first.name = "First draft"
        SheetProtocol.fixture.withLock { $0.holdPath = "/api/sequences" }
        let save = Task { await model.saveSequence(first) }
        defer { save.cancel() }
        try await waitUntil { SheetProtocol.fixture.withLock { !$0.held.isEmpty } }
        #expect(await !model.saveSequence(first))
        model.sequenceEditor = nil
        model.openSequenceEditor()
        #expect(model.sequenceEditor?.id == first.id && model.sequenceEditor?.isSaving == true)
        model.openSequenceEditor(adding: try photo())
        let second = try #require(model.sequenceEditor)
        second.name = "Second draft"
        second.note = "Keep me open"
        SheetProtocol.release()
        #expect(await save.value)
        #expect(model.sequenceEditor?.id == second.id)
        #expect(second.name == "Second draft" && second.note == "Keep me open")
        #expect(model.pendingSequenceDrafts.map(\.id) == [second.id])
        #expect(SheetProtocol.fixture.withLock { $0.sequences.count } == 1)
    }

    @Test func unchangedCreateReplayDoesNotOverwriteAnExternalRename() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        let draft = model.sequenceDraft()
        draft.name = "Original"
        draft.note = "First note"
        SheetProtocol.fixture.withLock { $0.createFailuresAfterCommit = 1 }
        #expect(await !model.saveSequence(draft))
        SheetProtocol.fixture.withLock { $0.sequences[draft.creationID]?.name = "Renamed elsewhere" }
        draft.note = "User revised the note"
        #expect(await model.saveSequence(draft))
        #expect(SheetProtocol.fixture.withLock { $0.sequences[draft.creationID]?.name } == "Renamed elsewhere")
        #expect(SheetProtocol.fixture.withLock { $0.sequences[draft.creationID]?.note } == "User revised the note")
        #expect(SheetProtocol.fixture.withLock { $0.sequences.count } == 1)
    }

    @Test func revertingAnUncertainMetadataWriteStillSendsTheLatestIntent() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        let sequence = try await model.client.createSequence(name: "Original", note: "Keep")
        let draft = model.sequenceDraft(editing: sequence)
        draft.name = "Attempted name"
        SheetProtocol.fixture.withLock { $0.updateFailuresAfterCommit = 1 }
        #expect(await !model.saveSequence(draft))
        draft.name = "Original"
        #expect(await model.saveSequence(draft))
        #expect(SheetProtocol.fixture.withLock { $0.sequences[sequence.id]?.name } == "Original")
        #expect(SheetProtocol.fixture.withLock { $0.sequences[sequence.id]?.note } == "Keep")
    }

    @Test func correctingARejectedCreationRetriesCurrentFieldsWithTheSameID() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        let draft = model.sequenceDraft()
        draft.name = "Rejected name"
        draft.note = "First note"
        SheetProtocol.fixture.withLock { $0.rejectedName = draft.name }
        #expect(await !model.saveSequence(draft))
        #expect(SheetProtocol.fixture.withLock { $0.sequences.isEmpty })
        draft.name = "Corrected name"
        draft.note = "Corrected note"
        #expect(await model.saveSequence(draft))
        #expect(SheetProtocol.fixture.withLock { $0.createIDs } == [draft.creationID, draft.creationID])
        #expect(SheetProtocol.fixture.withLock { $0.createNames } == ["Rejected name", "Corrected name"])
        #expect(SheetProtocol.fixture.withLock { $0.sequences[draft.creationID]?.name } == "Corrected name")
        #expect(SheetProtocol.fixture.withLock { $0.sequences[draft.creationID]?.note } == "Corrected note")
    }

    @Test func sourceDraftsOpenedDuringAnotherLibraryHandshakeKeepTheirLoadedIdentity() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        let sequence = try await model.client.createSequence(name: "Library A sequence", note: "Keep")
        model.navigate(to: .sequence(id: sequence.id))
        try await waitUntil { !model.isLoading }
        let retainedSequence = try #require(model.activeSequence)
        let retainedPhoto = try photo()
        SheetProtocol.fixture.withLock { $0.ready = false }
        try await waitUntil { !model.service.isConnected }
        model.serviceStateChanged()
        SheetProtocol.fixture.withLock {
            $0.ready = true
            $0.dataID = "library-B"
            $0.holdPath = "/api/library"
            $0.requests = []
        }
        let reconnect = Task { await model.reconnectSheets() }
        defer { reconnect.cancel() }
        try await waitUntil { SheetProtocol.fixture.withLock { !$0.held.isEmpty } }
        #expect(model.service.isConnected)
        #expect(!model.workspaceWritable)
        let edit = model.sequenceDraft(editing: retainedSequence)
        let add = model.sequenceDraft(adding: retainedPhoto)
        edit.name = "An edit to library A"
        add.name = "A photo from library A"
        #expect(edit.dataID == "sheet-fixture" && add.dataID == "sheet-fixture")
        #expect(await !model.saveSequence(edit))
        #expect(await !model.saveSequence(add))
        #expect(SheetProtocol.fixture.withLock { $0.requests.allSatisfy { !$0.hasPrefix("POST") && !$0.hasPrefix("PATCH") } })
        #expect(SheetProtocol.fixture.withLock { $0.sequences[sequence.id]?.name } == "Library A sequence")
        SheetProtocol.release()
        await reconnect.value
    }

    @Test func discardingAPartialDraftDoesNotDeleteItsCreatedSequence() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        let draft = model.sequenceDraft(adding: try photo())
        draft.name = "Partly saved"
        SheetProtocol.fixture.withLock { $0.addFailures = 1 }
        #expect(await !model.saveSequence(draft))
        model.discardSequenceDraft(draft)
        #expect(model.pendingSequenceDrafts.isEmpty)
        #expect(SheetProtocol.fixture.withLock { $0.sequences[draft.creationID]?.name } == "Partly saved")
        #expect(SheetProtocol.fixture.withLock { $0.requests.allSatisfy { !$0.hasPrefix("DELETE") } })
    }

    @Test func retainedDraftsCannotWriteToADifferentLibraryAfterReconnect() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        let draft = model.sequenceDraft()
        draft.name = "Original library only"
        SheetProtocol.fixture.withLock { $0.ready = false }
        try await waitUntil { !model.service.isConnected }
        model.serviceStateChanged()
        SheetProtocol.fixture.withLock { $0.ready = true; $0.dataID = "another-library" }
        await model.reconnectSheets()
        #expect(await !model.saveSequence(draft))
        #expect(draft.errorMessage?.contains("another library") == true)
        #expect(draft.name == "Original library only")
        #expect(SheetProtocol.fixture.withLock { $0.requests.filter { $0.hasPrefix("POST") || $0.hasPrefix("PATCH") }.isEmpty })
    }

    @Test func editingRetentionChoiceSurvivesReopenButBelongsToOneReview() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        await model.refreshEditing()
        let original = try #require(model.batches.first)
        model.setEditingPolicy("clear_batch", for: original)
        model.showingEditing = false
        model.showingEditing = true
        await model.refreshEditing()
        #expect(model.editingPolicy(for: try #require(model.batches.first)) == "clear_batch")
        SheetProtocol.fixture.withLock { $0.batchRevision += 1; $0.batchDestination = "/synthetic-only/new-review" }
        await model.refreshEditing()
        #expect(model.editingPolicy(for: try #require(model.batches.first)).isEmpty)
    }

    @Test func anUncertainPrepareIsCheckedBeforeAnIntentionalRefreshCanRun() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        await model.refreshEditing()
        let original = try #require(model.batches.first)
        model.setEditingPolicy("keep_exports", for: original)
        SheetProtocol.fixture.withLock { $0.prepareFailuresAfterCommit = 1 }
        model.editAction(original, action: "prepare")
        try await waitUntil { !model.editingBusy }
        model.editAction(original, action: "prepare")
        try await waitUntil { !model.editingBusy }
        #expect(SheetProtocol.fixture.withLock { $0.requests.filter { $0 == "POST /api/editing/batch-fixture/prepare" }.count } == 1)
        let refreshed = try #require(model.batches.first)
        #expect(refreshed.uploadDestination != original.uploadDestination)
        #expect(model.editingPolicy(for: refreshed).isEmpty)
        model.editAction(refreshed, action: "prepare")
        try await waitUntil { !model.editingBusy }
        #expect(SheetProtocol.fixture.withLock { $0.requests.filter { $0 == "POST /api/editing/batch-fixture/prepare" }.count } == 2)
    }

    @Test func anOlderEditingPollCannotReplaceANewerPreparedReview() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        await model.refreshEditing()
        let original = try #require(model.batches.first)
        SheetProtocol.fixture.withLock { $0.holdPath = "/api/editing" }
        let poll = Task { await model.refreshEditing() }
        defer { poll.cancel() }
        try await waitUntil { SheetProtocol.fixture.withLock { !$0.held.isEmpty } }
        model.editAction(original, action: "prepare")
        try await waitUntil { !model.editingBusy }
        let expected = model.batches.first?.uploadDestination
        SheetProtocol.release()
        await poll.value
        #expect(model.batches.first?.uploadDestination == expected && expected != original.uploadDestination)
    }

    @Test func changedReviewedFilesCannotReuseAnOldUploadChoice() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        await model.refreshEditing()
        let original = try #require(model.batches.first)
        model.setEditingPolicy("clear_batch", for: original)
        SheetProtocol.fixture.withLock { $0.batchRevision += 1; $0.batchDestination = "/synthetic-only/another-review" }
        model.editAction(original, action: "finish", policy: "clear_batch")
        try await waitUntil { !model.editingBusy }
        #expect(SheetProtocol.fixture.withLock { $0.requests.filter { $0 == "POST /api/editing/batch-fixture/finish" }.isEmpty })
        #expect(model.editingPolicy(for: try #require(model.batches.first)).isEmpty)
        #expect(model.editingErrorMessage?.contains("changed") == true)
    }

    @Test func repeatedEditingActionsCoalesceWhileTheirFirstRequestIsPending() async throws {
        let model = try await makeModel()
        defer { cleanup(model) }
        await model.refreshEditing()
        let original = try #require(model.batches.first)
        SheetProtocol.fixture.withLock { $0.holdPath = "/api/editing/batch-fixture/finish" }
        model.editAction(original, action: "finish", policy: "keep_exports")
        model.editAction(original, action: "finish", policy: "keep_exports")
        try await waitUntil { SheetProtocol.fixture.withLock { !$0.held.isEmpty } }
        SheetProtocol.release()
        try await waitUntil { !model.editingBusy }
        #expect(SheetProtocol.fixture.withLock { $0.requests.filter { $0 == "POST /api/editing/batch-fixture/finish" }.count } == 1)
        #expect(model.batches.first?.status == "uploading")
    }
}
