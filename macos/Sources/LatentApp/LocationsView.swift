import AppKit
import LatentCore
import SwiftUI

extension LibraryModel {
    func monitorLocations() async {
        while !Task.isCancelled {
            await refreshLocations()
            do { try await Task.sleep(for: .seconds(2)) } catch { return }
        }
    }

    func refreshLocations() async {
        guard service.isConnected, !locationsBusy, let dataID = service.identity?.dataId else { return }
        locationsGeneration += 1
        let generation = locationsGeneration
        do {
            let value = try await client.locations()
            guard service.isConnected, service.identity?.dataId == dataID,
                  value.dataId == dataID, !locationsBusy, locationsGeneration == generation else { return }
            locations = value
        } catch { locationsError = error.localizedDescription }
    }

    func changeLocation(route: String = "", action: String? = nil, path: String? = nil, sourceID: String? = nil) {
        guard !locationsBusy, service.isConnected, let current = locations,
              current.dataId == service.identity?.dataId else { return }
        locationsBusy = true
        locationsGeneration += 1
        locationsError = nil
        locationsNotice = nil
        Task {
            defer { locationsBusy = false }
            do {
                let result = try await client.updateLocations(route: route, action: action, path: path,
                                                             sourceID: sourceID, expected: current)
                guard service.isConnected, service.identity?.dataId == current.dataId else { return }
                locations = result
                if let links = result.sequenceResult {
                    locationsNotice = "Updated \(links.links) original-photo links in \(links.sequences) sequences."
                }
            } catch { locationsError = error.localizedDescription }
        }
    }

    func chooseLocation(action: String, sourceID: String? = nil) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = true
        panel.prompt = "Choose Folder"
        switch action {
        case "set_output": panel.message = "Choose where finished photos should be saved."
        case "set_sequence_folder": panel.message = "Latent will create a sequences folder here, with links to original photos."
        case "reconnect_source": panel.message = "Choose the existing archive root that matches the displayed folder hierarchy."
        default: panel.message = "Choose an original photo folder on this Mac, an external disk, or a mounted cloud drive."
        }
        panel.begin { response in
            guard response == .OK, let url = panel.url else { return }
            self.changeLocation(action: action, path: url.path, sourceID: sourceID)
        }
    }
}

struct LocationsView: View {
    @Bindable var model: LibraryModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Locations").font(.title2.bold())
                    Text("Connected photo folders and output destinations.")
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            SheetConnectionBanner(model: model)
            if let error = model.locationsError {
                Text(error).font(.callout).foregroundStyle(.orange).textSelection(.enabled)
            }
            if let notice = model.locationsNotice {
                Text(notice).font(.callout).foregroundStyle(.green)
            }
            if let locations = model.locations {
                ScrollView {
                    VStack(alignment: .leading, spacing: 22) {
                        inputSection(locations)
                        Divider()
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Finished photos").font(.headline)
                            folderPath(locations.output?.path ?? "Choose an output folder")
                            Text("Preserves each original's folder hierarchy. Choose the finished photos to save from each editing batch. Existing files are kept; different versions get a new name.")
                                .font(.callout).foregroundStyle(.secondary)
                            Text("When input and output overlap, Latent uses a protected _Latent Edits folder where needed. Outputs are excluded from imports.")
                                .font(.caption).foregroundStyle(.secondary)
                            Button("Choose output folder…") { model.chooseLocation(action: "set_output") }
                                .buttonStyle(.glass)
                        }.disabled(locationChangesDisabled)
                        Divider()
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Sequence links").font(.headline)
                            folderPath(locations.sequenceFolder.map { $0.path + "/sequences" } ?? "Choose where to keep sequence folders")
                            Text("Mirrors your sequence folders and photo order. Each photo is a symbolic link to its original, so the original disk or mount must stay connected. These are filesystem links, not Google Drive web shortcuts.")
                                .font(.callout).foregroundStyle(.secondary)
                            HStack {
                                Button("Choose parent folder…") { model.chooseLocation(action: "set_sequence_folder") }
                                Button("Update sequence links") { model.changeLocation(route: "/sequence-links") }
                                    .disabled(locations.sequenceFolder == nil)
                            }.buttonStyle(.glass)
                            Text("Update after organizing sequences. Photos are not duplicated.")
                                .font(.caption).foregroundStyle(.secondary)
                        }.disabled(locationChangesDisabled)
                    }.padding(.trailing, 4)
                }
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .padding(26).frame(width: 720, height: 690)
        .preferredColorScheme(.dark)
        .task { await model.monitorLocations() }
    }

    private func folderPath(_ path: String) -> some View {
        Text(path).font(.caption.monospaced()).foregroundStyle(.secondary)
            .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
    }

    private var locationChangesDisabled: Bool {
        model.locationsBusy || !model.workspaceWritable || model.importActive
    }

    private func inputSection(_ locations: LibraryLocations) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Original photos").font(.headline)
            if !locations.sources.isEmpty {
                Picker("Photo location", selection: Binding(
                    get: { locations.activeSourceId ?? "" },
                    set: { model.changeLocation(action: "select_source", sourceID: $0) }
                )) {
                    ForEach(locations.sources) { source in Text(source.name).tag(source.id) }
                }.disabled(locationChangesDisabled)
            }
            if let source = locations.activeSource {
                folderPath(source.folder?.path ?? source.logicalRoot)
                if source.folder == nil {
                    Text("Connect the matching archive root to keep your existing photos, ratings and sequences.")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Button(source.folder == nil ? "Connect archive folder…" : "Reconnect folder…") {
                    model.chooseLocation(action: "reconnect_source", sourceID: source.id)
                }.buttonStyle(.glass).disabled(locationChangesDisabled)
            }
            Text("Add photo folders and build local previews in Imports. Existing previews are reused.")
                .font(.caption).foregroundStyle(.secondary)
            Button(model.importActive ? "View active import…" : "Open Imports…") {
                model.importsAfterLocations = true
                dismiss()
            }.buttonStyle(.glass).disabled(!model.service.isConnected)
        }
    }
}
