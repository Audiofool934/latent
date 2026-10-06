import Foundation
import LatentCore
import Observation

@MainActor @Observable final class SequenceFolderDraft: Identifiable {
    let id = UUID()
    let editing: SequenceFolder?
    let parentID: String?
    let dataID: String?
    var name: String
    var error: String?
    var creationName: String?
    init(editing: SequenceFolder?, parentID: String?, dataID: String?) {
        self.editing = editing; self.parentID = parentID; self.dataID = dataID
        name = editing?.name ?? ""
    }
}

struct SequenceMoveDraft: Identifiable {
    let id = UUID()
    let item: SequenceHierarchy.Item
    let dataID: String?
}

extension LibraryModel {
    var sequenceHierarchy: SequenceHierarchy { SequenceHierarchy(folders: sequenceFolders, sequences: sequences) }

    func sequenceFolderSummary(_ folderID: String?) -> String {
        let contents = sequenceHierarchy.contents(of: folderID)
        let folders = contents.filter { if case .folder = $0 { true } else { false } }.count
        let sequences = contents.count - folders
        return "\(folders) \(folders == 1 ? "folder" : "folders") · \(sequences) \(sequences == 1 ? "sequence" : "sequences")"
    }

    func folderPath(_ folderID: String?) -> String {
        (["Sequences"] + sequenceHierarchy.path(to: folderID).map(\.name)).joined(separator: " / ")
    }

    func applySequenceLibrary(_ result: SequencesResponse, dataID: String?) {
        if organizationDataID != dataID {
            organizationDataID = dataID
            selectedSequenceFolderID = nil
            expandedSequenceFolders = Set(preferences.stringArray(forKey: expansionKey) ?? [])
        }
        sequences = result.sequences
        sequenceFolders = result.folders ?? []
        let ids = Set(sequenceFolders.map(\.id))
        expandedSequenceFolders.formIntersection(ids)
        if let selectedSequenceFolderID, !ids.contains(selectedSequenceFolderID) { self.selectedSequenceFolderID = nil }
    }

    private var expansionKey: String { "sequenceFolders.expanded.\(organizationDataID ?? "unconnected")" }

    func toggleSequenceFolder(_ id: String) {
        if expandedSequenceFolders.contains(id) { expandedSequenceFolders.remove(id) }
        else { expandedSequenceFolders.insert(id) }
        preferences.set(expandedSequenceFolders.sorted(), forKey: expansionKey)
    }

    func expandSequenceAncestors(of folderID: String?) {
        expandedSequenceFolders.formUnion(sequenceHierarchy.path(to: folderID).map(\.id))
        preferences.set(expandedSequenceFolders.sorted(), forKey: expansionKey)
    }

    func openFolderEditor(editing: SequenceFolder? = nil, parentID: String? = nil) {
        guard workspaceWritable else { return }
        folderEditor = SequenceFolderDraft(editing: editing, parentID: editing?.parentId ?? parentID,
                                          dataID: service.identity?.dataId)
    }

    private func requireOrganizationLibrary(_ dataID: String?) throws -> String {
        guard workspaceWritable, let dataID, service.identity?.dataId == dataID else {
            throw ServiceConnectionError("Reconnect to the library that owns these folders before saving.")
        }
        return dataID
    }

    func saveFolder(_ draft: SequenceFolderDraft) async {
        guard !isSaving else { return }
        let name = draft.name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, name.unicodeScalars.count <= 120 else {
            draft.error = "Use a folder name of 1-120 characters."; return
        }
        organizationBusy = true
        sequenceRevision += 1
        defer { organizationBusy = false }
        do {
            let dataID = try requireOrganizationLibrary(draft.dataID)
            let folder: SequenceFolder
            if let editing = draft.editing {
                folder = try await client.renameSequenceFolder(id: editing.id, name: name, expectedDataID: dataID)
            } else {
                if draft.creationName == nil { draft.creationName = name }
                folder = try await client.createSequenceFolder(name: name, parentID: draft.parentID,
                    folderID: draft.id.uuidString.lowercased(), expectedDataID: dataID)
                // A response lost after creation may be replayed with a corrected name.
                if draft.creationName != name {
                    _ = try requireOrganizationLibrary(dataID)
                    _ = try await client.renameSequenceFolder(id: folder.id, name: name, expectedDataID: dataID)
                }
            }
            _ = try requireOrganizationLibrary(dataID)
            await refreshSequences()
            _ = try requireOrganizationLibrary(dataID)
            expandSequenceAncestors(of: folder.parentId)
            if folderEditor?.id == draft.id { folderEditor = nil }
        } catch { draft.error = error.localizedDescription }
    }

    func startMoving(_ item: SequenceHierarchy.Item) {
        guard workspaceWritable else { return }
        movingSequenceItem = SequenceMoveDraft(item: item, dataID: service.identity?.dataId)
    }

    @discardableResult
    func move(_ item: SequenceHierarchy.Item, to destination: String?, dataID: String?) async -> Bool {
        guard !isSaving, sequenceHierarchy.canMove(item, to: destination) else { return false }
        organizationBusy = true
        sequenceRevision += 1
        defer { organizationBusy = false }
        do {
            let expected = try requireOrganizationLibrary(dataID)
            switch item {
            case .folder(let folder):
                _ = try await client.moveSequenceFolder(id: folder.id, to: destination, expectedDataID: expected)
            case .sequence(let sequence):
                _ = try await client.moveSequence(id: sequence.id, to: destination, expectedDataID: expected)
            }
            _ = try requireOrganizationLibrary(expected)
            await refreshSequences()
            _ = try requireOrganizationLibrary(expected)
            expandSequenceAncestors(of: destination)
            showNotice("Moved \(item.name)")
            return true
        } catch { errorMessage = error.localizedDescription; return false }
    }

    func removeFolder(_ folder: SequenceFolder, dataID: String?) async {
        guard !isSaving else { return }
        organizationBusy = true
        sequenceRevision += 1
        defer { organizationBusy = false }
        do {
            let expected = try requireOrganizationLibrary(dataID)
            try await client.removeSequenceFolder(id: folder.id, expectedDataID: expected)
            _ = try requireOrganizationLibrary(expected)
            if selectedSequenceFolderID == folder.id { selectedSequenceFolderID = folder.parentId }
            await refreshSequences()
            showNotice("Removed folder; its contents moved up one level")
        } catch { errorMessage = error.localizedDescription }
    }

    func deleteSequence(_ sequence: PhotoSequence, dataID: String?) async {
        guard !isSaving else { return }
        organizationBusy = true
        sequenceRevision += 1
        defer { organizationBusy = false }
        do {
            let expected = try requireOrganizationLibrary(dataID)
            let current = sequences.first { $0.id == sequence.id } ?? sequence
            let parent = current.folderId
            try await client.deleteSequence(id: sequence.id, expectedDataID: expected)
            _ = try requireOrganizationLibrary(expected)
            sequences.removeAll { $0.id == sequence.id }
            forgetDeletedSequence(sequence.id, dataID: expected)
            if !showingSequences, source == .sequence(id: sequence.id) {
                photos = []
                total = 0
                nextOffset = nil
                source = .library(date: nil)
                showSequences(folderID: parent)
            } else {
                await refreshSequences()
            }
            _ = try requireOrganizationLibrary(expected)
            showNotice("Deleted sequence; original photos are kept")
        } catch { errorMessage = error.localizedDescription }
    }

}
