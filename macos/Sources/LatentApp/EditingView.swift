import AppKit
import LatentCore
import SwiftUI

struct EditingView: View {
    @Bindable var model: LibraryModel
    @Environment(\.dismiss) private var dismiss
    @State private var showingLocations = false

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Editing").font(.title2.bold())
                    Text("Edit working copies, then choose the finished photos to save.")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Locations…") { showingLocations = true }
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            SheetConnectionBanner(model: model)
            if let error = model.editingErrorMessage ?? model.errorMessage {
                HStack {
                    Text(error).font(.callout).foregroundStyle(.orange).textSelection(.enabled)
                    Spacer()
                    Button("Check status") { Task { await model.refreshEditing() } }
                        .disabled(!model.service.isConnected || model.editingBusy)
                }
            }
            if model.batches.isEmpty {
                ContentUnavailableView("No editing batches", systemImage: "slider.horizontal.3",
                    description: Text("Select photos in your library and choose Edit in PhotoLab."))
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 18) {
                        ForEach(model.batches) { batch in batchCard(batch) }
                    }
                }
            }
        }
        .padding(26).frame(width: 740, height: 620)
        .preferredColorScheme(.dark)
        .task { await model.refreshEditing() }
        .sheet(isPresented: $showingLocations) { LocationsView(model: model) }
    }

    private func batchCard(_ batch: EditingBatch) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("\(batch.items.count) \(batch.items.count == 1 ? "photo" : "photos") · \(batch.label)").font(.headline)
                    Text(batch.items.prefix(3).map(\.name).joined(separator: ", ")
                        + (batch.items.count > 3 ? "…" : ""))
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer()
                if batch.transferring { ProgressView().controlSize(.small) }
                Button("Show folder", systemImage: "folder") {
                    NSWorkspace.shared.open(URL(fileURLWithPath: batch.folder, isDirectory: true))
                }.buttonStyle(.glass)
            }
            Text(batch.folder).font(.caption.monospaced()).foregroundStyle(.secondary)
                .textSelection(.enabled).lineLimit(2)
                .accessibilityLabel("Editing folder: \(batch.folder)")
            if batch.status == "downloading" {
                ProgressView(value: Double(batch.bytesDone), total: Double(max(1, batch.bytesTotal)))
                Text("\(PhotoFormatting.bytes(batch.bytesDone)) of \(PhotoFormatting.bytes(batch.bytesTotal))")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let error = batch.error { Text(error).font(.callout).foregroundStyle(.orange).textSelection(.enabled) }
            if ["ready", "prepared", "saved", "cloud_verified"].contains(batch.status) {
                Text("Save PhotoLab settings as .dop sidecars. Export finished photos into this editing folder (a subfolder is fine), then review the files below.")
                    .font(.callout).foregroundStyle(.secondary)
                HStack {
                    Button("Open in PhotoLab") { model.openInPhotoLab(batch) }.buttonStyle(.glass)
                    Button(batch.status == "prepared" ? "Refresh files" : "Review finished photos…") {
                        model.editAction(batch, action: "prepare")
                    }.buttonStyle(.glass).disabled(model.editingBusy || !model.workspaceWritable)
                }
            }
            if batch.status == "prepared" {
                Divider()
                if batch.usesFolderOutput {
                    FinishedPhotoReview(model: model, batch: batch)
                        .id(batch.id + (batch.uploadDestination ?? ""))
                } else {
                Text("Upload \(batch.uploads.count) files").font(.headline)
                ForEach(batch.uploads, id: \.localPath) { upload in
                    HStack {
                        Text(upload.localPath).lineLimit(1)
                        Spacer()
                        Text(PhotoFormatting.bytes(upload.sizeBytes)).foregroundStyle(.secondary)
                    }.font(.callout)
                }
                VStack(alignment: .leading, spacing: 5) {
                    Text("Cloud folder").font(.caption).foregroundStyle(.secondary)
                    Text(batch.uploadDestination ?? batch.uploads.first.map {
                        ($0.remotePath as NSString).deletingLastPathComponent
                    } ?? "")
                    .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                }
                Text("Originals stay in the archive. Uploads are verified before local cleanup.")
                    .font(.caption).foregroundStyle(.secondary)
                Picker("After verified upload", selection: Binding(
                    get: { model.editingPolicy(for: batch) },
                    set: { model.setEditingPolicy($0, for: batch) }
                )) {
                    Text("Choose local retention…").tag("")
                    Text("Keep JPEG/HEIC and sidecars; remove cached RAWs").tag("keep_exports")
                    Text("Remove all files in this editing batch").tag("clear_batch")
                }
                Button("Upload edits", systemImage: "icloud.and.arrow.up") {
                    model.editAction(batch, action: "finish", policy: model.editingPolicy(for: batch))
                }.buttonStyle(.glassProminent)
                    .disabled((model.editingPolicy(for: batch)).isEmpty || model.editingBusy || !model.workspaceWritable)
                }
            }
            if ["needs_attention", "interrupted"].contains(batch.status) {
                if ["upload", "cleanup"].contains(batch.phase) {
                    Button("Verify cloud copies") { model.editAction(batch, action: "verify") }
                        .buttonStyle(.glassProminent).disabled(model.editingBusy || !model.workspaceWritable)
                    Text("Checks the existing upload and keeps every local working file.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if ["upload", "copy"].contains(batch.phase) {
                    Button("Review files again") {
                        model.editAction(batch, action: "prepare")
                    }
                        .buttonStyle(.glass).disabled(model.editingBusy || !model.workspaceWritable)
                }
                Button("Retry transfer") { model.editAction(batch, action: "resume") }
                    .buttonStyle(.glass).disabled(model.editingBusy || !model.workspaceWritable || model.batches.contains(where: \.transferring))
            }
            if batch.status == "complete" {
                Label(batch.cleanupPolicy == "keep_exports" ? "Cloud copies verified. Cached RAWs removed; exports and sidecars kept here." : "Cloud copies verified. Reviewed working files removed.", systemImage: "checkmark.icloud")
                    .font(.callout).foregroundStyle(.green)
            }
            if batch.status == "saved" {
                Label("Selected photos saved and checked in the output folder. All working files are kept.", systemImage: "checkmark.folder")
                    .font(.callout).foregroundStyle(.green)
                Text("For cloud mounts, check CloudDrive2 for upload completion.")
                    .font(.caption).foregroundStyle(.secondary)
                if let path = batch.uploadDestination {
                    Button("Show output folder") { NSWorkspace.shared.open(URL(fileURLWithPath: path, isDirectory: true)) }
                        .buttonStyle(.glass)
                }
            }
            if batch.status == "waiting_cloud" {
                Text("CloudDrive is still confirming the upload. All local files are kept.")
                    .font(.callout).foregroundStyle(.secondary)
                Button("Verify cloud copies") { model.editAction(batch, action: "verify") }
                    .buttonStyle(.glass).disabled(model.editingBusy || !model.workspaceWritable)
            }
            if ["uploaded", "cloud_verified"].contains(batch.status) {
                Label("Cloud hashes verified. Local files are still kept.", systemImage: "checkmark.icloud")
                    .font(.callout).foregroundStyle(.green)
            }
        }
        .padding(18)
        .background(.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(.white.opacity(0.08)))
    }
}

private struct FinishedPhotoReview: View {
    @Bindable var model: LibraryModel
    let batch: EditingBatch
    @State private var selected: Set<String>

    init(model: LibraryModel, batch: EditingBatch) {
        self.model = model
        self.batch = batch
        _selected = State(initialValue: Set(batch.uploads.map(\.localPath)))
    }

    private var selections: [ExportSelection] {
        batch.uploads.filter { selected.contains($0.localPath) }.compactMap { upload in
            upload.assetId.map { ExportSelection(path: upload.localPath, assetID: $0) }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("\(selected.count) of \(batch.uploads.count) finished photos selected").font(.headline)
                Spacer()
                Button(selected.isEmpty ? "Select all" : "Select none") {
                    selected = selected.isEmpty ? Set(batch.uploads.map(\.localPath)) : []
                }.buttonStyle(.borderless)
            }
            ForEach(batch.uploads, id: \.localPath) { upload in
                VStack(alignment: .leading, spacing: 5) {
                    HStack {
                        Toggle(upload.localPath, isOn: Binding(
                            get: { selected.contains(upload.localPath) },
                            set: { if $0 { selected.insert(upload.localPath) } else { selected.remove(upload.localPath) } }
                        )).toggleStyle(.checkbox)
                        Spacer()
                        Text(PhotoFormatting.bytes(upload.sizeBytes)).foregroundStyle(.secondary)
                    }
                    Picker("Original photo", selection: Binding(
                        get: { upload.assetId ?? 0 },
                        set: { value in
                            guard value != 0 else { return }
                            model.editAction(batch, action: "map", exports: [ExportSelection(path: upload.localPath, assetID: value)])
                        }
                    )) {
                        Text("Match to an original…").tag(0)
                        ForEach(batch.items, id: \.assetId) { item in
                            Text(item.archiveRelative ?? item.name).tag(item.assetId)
                        }
                    }.font(.caption)
                    Text(upload.remotePath.isEmpty ? "Choose the original photo to preserve its folder hierarchy." : upload.remotePath)
                        .font(.caption.monospaced()).foregroundStyle(upload.remotePath.isEmpty ? .orange : .secondary)
                        .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                }.padding(.vertical, 4)
            }
            Text("Only selected finished photos are copied. RAWs, sidecars and exports stay in this working folder. A cloud mount handles its own upload after the copy.")
                .font(.caption).foregroundStyle(.secondary)
            Button("Save \(selected.count) photos to output folder", systemImage: "arrow.up.doc") {
                model.editAction(batch, action: "finish", exports: selections)
            }.buttonStyle(.glassProminent)
                .disabled(selected.isEmpty || selections.count != selected.count || model.editingBusy || !model.workspaceWritable)
        }.disabled(model.editingBusy)
            .onChange(of: batch.uploads.map(\.localPath)) { _, paths in
                selected.formIntersection(Set(paths))
            }
    }
}
