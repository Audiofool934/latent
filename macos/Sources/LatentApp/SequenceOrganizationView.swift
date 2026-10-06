import LatentCore
import SwiftUI

struct SequenceSidebar: View {
    @Bindable var model: LibraryModel
    var body: some View {
        ForEach(model.sequenceHierarchy.visibleRows(expanded: model.expandedSequenceFolders)) { row in
            SequenceSidebarRow(model: model, row: row)
        }
    }
}

private struct SequenceSidebarRow: View {
    @Bindable var model: LibraryModel
    let row: SequenceHierarchy.Row
    private var selected: Bool {
        switch row.item {
        case .folder(let folder): model.showingSequences && model.selectedSequenceFolderID == folder.id
        case .sequence(let sequence): !model.showingSequences && model.source == .sequence(id: sequence.id)
        }
    }

    var body: some View {
        Group {
            if case .folder(let folder) = row.item {
                DisclosureGroup(isExpanded: Binding(
                    get: { model.expandedSequenceFolders.contains(folder.id) },
                    set: { if $0 != model.expandedSequenceFolders.contains(folder.id) { model.toggleSequenceFolder(folder.id) } }
                )) { EmptyView() } label: { navigationButton }
            } else {
                navigationButton
            }
        }
        // The manager's breadcrumb navigation remains usable beyond the sidebar's available width.
        .padding(.leading, CGFloat(min(row.depth, 8)) * 12 + 3).padding(.trailing, 7)
        .background(selected ? Color.accentColor.opacity(0.17) : .clear, in: RoundedRectangle(cornerRadius: 7))
        .help(model.folderPath(row.item.parentID) + " / " + row.item.name)
        .accessibilityIdentifier("sidebar-\(row.item.id)")
        .disabled(!model.service.isConnected)
        .modifier(SequenceContextActions(model: model, item: row.item))
    }

    private var navigationButton: some View {
        Button {
            switch row.item {
            case .folder(let folder): model.showSequences(folderID: folder.id)
            case .sequence(let sequence): model.navigate(to: .sequence(id: sequence.id))
            }
        } label: {
            HStack(spacing: 5) {
                Label(row.item.name, systemImage: symbol).lineLimit(1).truncationMode(.middle)
                if row.depth > 8 { Text("L\(row.depth + 1)").font(.caption2).foregroundStyle(.secondary) }
            }
            .frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 7)
            .contentShape(Rectangle())
        }.buttonStyle(.plain)
            .modifier(SequenceDragSource(model: model, item: row.item))
            .modifier(SequenceFolderDropDestination(model: model, item: row.item))
    }

    private var symbol: String {
        switch row.item {
        case .folder: "folder"
        case .sequence(let sequence): sequence.smartFilters == nil ? "rectangle.stack" : "sparkles.rectangle.stack"
        }
    }
}

struct SequenceOverview: View {
    @Bindable var model: LibraryModel
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 12) {
                SequenceBreadcrumbs(model: model, folderID: model.selectedSequenceFolderID) { model.showSequences(folderID: $0) }
                Spacer(minLength: 16)
                Button("New folder", systemImage: "folder.badge.plus") { model.openFolderEditor(parentID: model.selectedSequenceFolderID) }
                    .buttonStyle(.glass).disabled(!model.workspaceWritable || model.isSaving)
                Button("New sequence", systemImage: "plus") { model.newSequence(in: model.selectedSequenceFolderID) }
                    .buttonStyle(.glass).disabled(!model.workspaceWritable || model.isSaving)
                Button("Smart sequence", systemImage: "sparkles") { model.newSmartSequence(folderID: model.selectedSequenceFolderID) }
                    .buttonStyle(.glass).disabled(!model.workspaceWritable || model.isSaving)
            }
            Text("Expand folders to see their sequences. Drag a sequence or folder into another folder to move it.")
                .font(.callout).foregroundStyle(.secondary)
            let rows = model.sequenceHierarchy.visibleRows(expanded: model.expandedSequenceFolders,
                                                          rootFolderID: model.selectedSequenceFolderID)
            if rows.isEmpty {
                ContentUnavailableView("This folder is empty", systemImage: "folder",
                    description: Text("Drop a sequence or folder here, or create one above."))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .modifier(SequenceDropDestination(model: model, folderID: model.selectedSequenceFolderID))
            } else {
                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(rows) { row in
                            SequenceOverviewRow(model: model, row: row)
                            if case .folder(let folder) = row.item,
                               model.expandedSequenceFolders.contains(folder.id),
                               model.sequenceHierarchy.contents(of: folder.id).isEmpty {
                                HStack(spacing: 0) {
                                    SequenceTreeIndent(depth: row.depth + 1)
                                    Label("Drop sequences or folders here", systemImage: "arrow.down.right")
                                        .font(.callout).foregroundStyle(.secondary)
                                        .padding(.vertical, 18).padding(.horizontal, 12)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                        .modifier(SequenceDropDestination(model: model, folderID: folder.id))
                                }
                                .accessibilityIdentifier("empty-folder-\(folder.id)")
                            }
                        }
                    }
                }
            }
        }
        .padding(24).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

private struct SequenceOverviewRow: View {
    @Bindable var model: LibraryModel
    let row: SequenceHierarchy.Row
    private var item: SequenceHierarchy.Item { row.item }
    var body: some View {
        HStack(spacing: 0) {
            SequenceTreeIndent(depth: row.depth)
            HStack(spacing: 10) {
                if case .folder(let folder) = item {
                    Button { model.toggleSequenceFolder(folder.id) } label: {
                        Image(systemName: model.expandedSequenceFolders.contains(folder.id) ? "chevron.down" : "chevron.right")
                            .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                            .frame(width: 24, height: 48).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("\(model.expandedSequenceFolders.contains(folder.id) ? "Collapse" : "Expand") \(folder.name)")
                    .accessibilityIdentifier("overview-disclosure-\(folder.id)")
                } else { Color.clear.frame(width: 24, height: 1) }
                Button {
                    switch item {
                    case .folder(let folder): model.showSequences(folderID: folder.id)
                    case .sequence(let sequence): model.navigate(to: .sequence(id: sequence.id))
                    }
                } label: {
                    HStack(spacing: 14) {
                        Image(systemName: symbol).font(.title2).foregroundStyle(Color.accentColor).frame(width: 30)
                        VStack(alignment: .leading, spacing: 5) {
                            Text(item.name).font(.headline).lineLimit(1)
                            Text(detail).font(.callout).foregroundStyle(.secondary).lineLimit(2)
                        }
                        Spacer()
                        Image(systemName: "chevron.right").font(.caption).foregroundStyle(.tertiary)
                    }
                    .padding(.vertical, 16).padding(.trailing, 12)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain).accessibilityIdentifier("overview-\(item.id)")
                .modifier(SequenceDragSource(model: model, item: item))
                .modifier(SequenceFolderDropDestination(model: model, item: item))
            }
            .padding(.leading, 4)
            .background(isFolder ? Color.secondary.opacity(0.045) : .clear, in: RoundedRectangle(cornerRadius: 8))
            .modifier(SequenceContextActions(model: model, item: item))
        }
        .disabled(!model.service.isConnected)
    }
    private var isFolder: Bool { if case .folder = item { true } else { false } }
    private var symbol: String {
        switch item {
        case .folder(let folder): model.expandedSequenceFolders.contains(folder.id) ? "folder.fill" : "folder"
        case .sequence(let sequence): sequence.smartFilters == nil ? "rectangle.stack" : "sparkles.rectangle.stack"
        }
    }
    private var detail: String {
        switch item {
        case .folder(let folder):
            return model.sequenceFolderSummary(folder.id)
        case .sequence(let sequence):
            return sequence.note.isEmpty ? "\(sequence.itemCount) \(sequence.itemCount == 1 ? "photo" : "photos")" : sequence.note
        }
    }
}

private struct SequenceTreeIndent: View {
    let depth: Int
    var body: some View {
        HStack(spacing: 0) {
            ForEach(0..<min(depth, 16), id: \.self) { _ in
                Rectangle().fill(.quaternary).frame(width: 1).frame(width: 26)
            }
        }.accessibilityHidden(true)
    }
}

struct SequenceContextActions: ViewModifier {
    @Bindable var model: LibraryModel
    let item: SequenceHierarchy.Item
    @State private var removing = false
    @State private var removalDataID: String?
    func body(content: Content) -> some View {
        content.contextMenu {
            if case .folder(let folder) = item {
                Button("New folder…") { model.openFolderEditor(parentID: folder.id) }
                Button("New sequence…") { model.newSequence(in: folder.id) }
                Button("New smart sequence…") { model.newSmartSequence(folderID: folder.id) }
                Divider()
                Button("Rename…") { model.openFolderEditor(editing: folder) }
            } else if case .sequence(let sequence) = item {
                Button("Edit sequence…") { model.openSequenceEditor(editing: sequence) }
            }
            Button("Move to…") { model.startMoving(item) }
            Divider()
            Button(removalLabel + "…", role: .destructive) {
                removalDataID = model.service.identity?.dataId
                removing = true
            }
            .disabled(!model.workspaceWritable || model.isSaving)
        }
        .confirmationDialog("\(removalLabel) “\(item.name)”?", isPresented: $removing) {
            Button(removalLabel, role: .destructive) {
                Task {
                    switch item {
                    case .folder(let folder): await model.removeFolder(folder, dataID: removalDataID)
                    case .sequence(let sequence): await model.deleteSequence(sequence, dataID: removalDataID)
                    }
                }
            }
            .disabled(!model.workspaceWritable || model.isSaving)
            Button("Cancel", role: .cancel) {}
        } message: {
            if case .folder = item {
                Text("Its sequences and subfolders will move up one level. All sequences and their photos are kept.")
            } else {
                Text("Only this sequence and its photo references will be deleted. Original photos, ratings and editing files are kept.")
            }
        }
    }

    private var removalLabel: String { if case .folder = item { "Remove folder" } else { "Delete sequence" } }
}

struct SequenceBreadcrumbs: View {
    @Bindable var model: LibraryModel
    let folderID: String?
    let select: (String?) -> Void
    var body: some View {
        HStack(spacing: 8) {
            Button { select(nil) } label: { Image(systemName: "rectangle.stack").padding(5) }
                .buttonStyle(.plain).help("All sequences").accessibilityLabel("All sequences")
                .modifier(SequenceDropDestination(model: model, folderID: nil))
            ScrollView(.horizontal) {
                HStack(spacing: 8) {
                    if folderID == nil {
                        Text("All sequences").fontWeight(.medium).padding(.vertical, 6)
                            .modifier(SequenceDropDestination(model: model, folderID: nil))
                    }
                    ForEach(model.sequenceHierarchy.path(to: folderID)) { folder in
                        Image(systemName: "chevron.right").font(.caption2).foregroundStyle(.tertiary)
                        Button(folder.name) { select(folder.id) }.buttonStyle(.plain)
                            .fontWeight(folder.id == folderID ? .medium : .regular)
                            .lineLimit(1).fixedSize()
                            .modifier(SequenceDropDestination(model: model, folderID: folder.id))
                    }
                }
            }.scrollIndicators(.hidden)
                .defaultScrollAnchor(.trailing, for: .initialOffset)
                .defaultScrollAnchor(.leading, for: .alignment)
        }
        .accessibilityIdentifier("sequence-breadcrumbs")
    }
}

struct SequenceFolderEditor: View {
    @Environment(\.dismiss) private var dismiss
    @Bindable var model: LibraryModel
    @Bindable var draft: SequenceFolderDraft
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text(draft.editing == nil ? "New folder" : "Rename folder").font(.title2.weight(.semibold))
            Text(model.folderPath(draft.parentID)).font(.callout).foregroundStyle(.secondary).lineLimit(3)
            TextField("Folder name", text: $draft.name).textFieldStyle(.roundedBorder)
                .accessibilityIdentifier("folder-name").disabled(model.organizationBusy)
            if let error = draft.error { Text(error).font(.callout).foregroundStyle(.orange) }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction).disabled(model.organizationBusy)
                Button(draft.editing == nil ? "Create" : "Save") { Task { await model.saveFolder(draft) } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(draft.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !model.workspaceWritable || model.isSaving)
            }
        }.padding(28).frame(width: 440).interactiveDismissDisabled(model.organizationBusy)
    }
}

struct SequenceMoveSheet: View {
    @Environment(\.dismiss) private var dismiss
    @Bindable var model: LibraryModel
    let draft: SequenceMoveDraft
    @State private var destination: String?
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Move “\(draft.item.name)”").font(.title2.weight(.semibold)).lineLimit(2)
            Text("Choose a destination folder.").foregroundStyle(.secondary)
            SequenceBreadcrumbs(model: model, folderID: destination) { destination = $0 }
            List {
                if let destination, let folder = model.sequenceHierarchy.folders[destination] {
                    Button { self.destination = folder.parentId } label: { Label("Up one level", systemImage: "arrow.up") }
                }
                ForEach(model.sequenceHierarchy.contents(of: destination)) { item in
                    if case .folder(let folder) = item, model.sequenceHierarchy.canMove(draft.item, to: folder.id) {
                        Button { destination = folder.id } label: {
                            HStack { Label(folder.name, systemImage: "folder"); Spacer(); Image(systemName: "chevron.right").font(.caption) }
                                .contentShape(Rectangle())
                        }.buttonStyle(.plain).padding(.vertical, 4)
                    }
                }
            }.frame(height: 240).clipShape(RoundedRectangle(cornerRadius: 8))
            if let error { Text(error).font(.callout).foregroundStyle(.orange) }
            HStack {
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Spacer()
                Button("Move here") {
                    Task {
                        if await model.move(draft.item, to: destination, dataID: draft.dataID) { dismiss() }
                        else { error = model.errorMessage ?? "Choose another destination and try again." }
                    }
                }.keyboardShortcut(.defaultAction)
                    .disabled(!model.workspaceWritable || model.isSaving || draft.item.parentID == destination
                              || !model.sequenceHierarchy.canMove(draft.item, to: destination))
            }.disabled(model.organizationBusy)
        }.padding(28).frame(width: 500).interactiveDismissDisabled(model.organizationBusy)
    }
}

struct SequenceAddMenu: View {
    @Bindable var model: LibraryModel
    let folderID: String?
    var body: some View {
        ForEach(model.sequenceHierarchy.contents(of: folderID)) { item in
            switch item {
            case .folder(let folder):
                Menu(folder.name) { SequenceAddMenu(model: model, folderID: folder.id) }
            case .sequence(let sequence):
                if sequence.smartFilters == nil { Button(sequence.name) { model.addSelected(to: sequence) } }
            }
        }
    }
}
