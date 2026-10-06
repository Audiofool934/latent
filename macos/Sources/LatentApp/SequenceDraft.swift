import Foundation
import LatentCore
import Observation

/// Sheet drafts live for this app session and survive dismissal and connection retries.
@MainActor @Observable final class SequenceDraft: Identifiable {
    let id = UUID()
    let key: String
    let endpoint: URL
    var dataID: String?
    let editing: PhotoSequence?
    let adding: [Photo]
    var folderID: String?
    var smartFilters: PhotoFilters?
    var name: String
    var note: String
    var confirmed: PhotoSequence?
    var creationFields: (name: String, note: String)?
    var baselineName: String
    var baselineNote: String
    var needsNameWrite = false
    var needsNoteWrite = false
    var isSaving = false
    var completed = false
    var errorMessage: String?

    init(key: String, endpoint: URL, dataID: String?, editing: PhotoSequence?, adding: [Photo], folderID: String? = nil) {
        self.key = key
        self.endpoint = endpoint
        self.dataID = dataID
        self.editing = editing
        self.adding = adding
        self.folderID = editing?.folderId ?? folderID
        smartFilters = editing?.smartFilters
        name = editing?.name ?? ""
        note = editing?.note ?? ""
        baselineName = editing?.name ?? ""
        baselineNote = editing?.note ?? ""
        confirmed = editing
    }

    var creationID: String { id.uuidString.lowercased() }
}
