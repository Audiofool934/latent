import LatentCore
import SwiftUI

extension LibraryModel {
    func setCollapseTimelapses(_ enabled: Bool) {
        guard service.isConnected, let dataID = service.identity?.dataId else { return }
        collapseTimelapses = enabled
        preferences.set(enabled, forKey: "collapseTimelapses.\(dataID)")
        if case .library = source, !showingSequences {
            navigate(to: source, preservingSelection: true, filters: searchFilters)
        }
    }

    func loadTimelapses(audit: Bool = false) async {
        guard !timelapseBusy, workspaceWritable, let dataID = service.identity?.dataId else { return }
        timelapseBusy = true
        timelapseGeneration += 1
        let generation = timelapseGeneration
        timelapseError = nil
        if timelapseDataID != dataID { timelapseGroups = [] }
        defer { if generation == timelapseGeneration { timelapseBusy = false } }
        do {
            let response = try await audit ? client.auditTimelapses(expectedDataID: dataID) : client.timelapses()
            guard generation == timelapseGeneration, service.isConnected, service.identity?.dataId == dataID else { return }
            timelapseGroups = response.groups
            timelapseDataID = dataID
        } catch {
            if generation == timelapseGeneration { timelapseError = error.localizedDescription }
        }
    }

    func setTimelapse(_ group: TimelapseGroup, confirmed: Bool) async {
        guard !timelapseBusy, workspaceWritable, let dataID = timelapseDataID,
              service.identity?.dataId == dataID else { return }
        timelapseBusy = true
        timelapseGeneration += 1
        let generation = timelapseGeneration
        timelapseError = nil
        defer { if generation == timelapseGeneration { timelapseBusy = false } }
        do {
            let response = try await client.setTimelapse(group, confirmed: confirmed, expectedDataID: dataID)
            guard generation == timelapseGeneration, service.isConnected, service.identity?.dataId == dataID else { return }
            timelapseGroups = response.groups
            if case .library = source, !showingSequences, collapseTimelapses {
                navigate(to: source, preservingSelection: true, filters: searchFilters)
            }
        } catch {
            if generation == timelapseGeneration { timelapseError = error.localizedDescription }
        }
    }

    func openTimelapse(_ id: String) {
        guard service.isConnected else { return }
        if case .timelapse = source {} else {
            timelapseReturnSource = source
            timelapseReturnFilters = searchFilters
        }
        showingTimelapses = false
        navigate(to: .timelapse(id: id))
    }

    func closeTimelapse() {
        navigate(to: timelapseReturnSource, filters: timelapseReturnFilters)
    }
}

struct TimelapseView: View {
    @Bindable var model: LibraryModel
    @Environment(\.dismiss) private var dismiss
    private var confirmed: [TimelapseGroup] { model.timelapseGroups.filter(\.confirmed) }
    private var candidates: [TimelapseGroup] { model.timelapseGroups.filter { !$0.confirmed } }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 5) {
                    Label("Time-lapse organizer", systemImage: "square.stack.3d.up").font(.title2.bold())
                    Text("Optional add-on · Keep interval shoots together.").foregroundStyle(.secondary)
                }
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            SheetConnectionBanner(model: model)
            VStack(alignment: .leading, spacing: 6) {
                Toggle("Collapse time-lapse groups in Timeline", isOn: Binding(
                    get: { model.collapseTimelapses }, set: { model.setCollapseTimelapses($0) }
                )).toggleStyle(.switch)
                    .accessibilityIdentifier("collapse-timelapses")
                    .disabled(!model.service.isConnected)
                Text("One representative photo per group. Open a group to browse every frame. Search, Starred and saved sequences keep their individual photos.")
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            .padding(14).background(.white.opacity(0.04), in: RoundedRectangle(cornerRadius: 10))
            HStack {
                Text("\(confirmed.count) groups · \(confirmed.reduce(0) { $0 + $1.photoCount }.formatted()) photos")
                    .font(.headline)
                Spacer()
                if model.timelapseBusy { ProgressView().controlSize(.small) }
                Button("Find candidates", systemImage: "magnifyingglass") {
                    Task { await model.loadTimelapses(audit: true) }
                }.disabled(!model.workspaceWritable || model.timelapseBusy)
                Button { Task { await model.loadTimelapses() } } label: { Image(systemName: "arrow.clockwise") }
                    .accessibilityLabel("Refresh time-lapse groups")
                    .disabled(!model.workspaceWritable || model.timelapseBusy)
            }
            if let error = model.timelapseError {
                Text(error).foregroundStyle(.orange).font(.callout).textSelection(.enabled)
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 16) {
                    if model.timelapseGroups.isEmpty && !model.timelapseBusy {
                        ContentUnavailableView("No groups yet", systemImage: "square.stack.3d.up",
                            description: Text("Find candidates using local capture times and camera metadata, then choose which shoots to group."))
                    }
                    if !confirmed.isEmpty {
                        ForEach(confirmed) { group in
                            groupRow(group).id("confirmed-\(group.id)-\(group.revision)")
                        }
                    }
                    if !candidates.isEmpty {
                        Text("Candidates to review · \(candidates.count)").font(.headline).padding(.top, 8)
                        Text("Regular timing is a clue. Check the frames before grouping a shoot.")
                            .foregroundStyle(.secondary).font(.callout)
                        ForEach(candidates) { group in
                            groupRow(group).id("candidate-\(group.id)-\(group.revision)")
                        }
                    }
                }.padding(.trailing, 8)
            }
            Text("Grouping preserves originals, ratings and search indexes. Ungroup returns a shoot to individual photos. Finding candidates reads local metadata only.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .padding(24).frame(width: 760, height: 670)
        .task(id: model.service.isConnected) { if model.service.isConnected { await model.loadTimelapses() } }
    }

    private func groupRow(_ group: TimelapseGroup) -> some View {
        HStack(alignment: .top, spacing: 14) {
            if let cover = group.cover, let url = try? model.client.mediaURL(cover.contactUrl) {
                AsyncImage(url: url) { image in image.resizable().scaledToFit() } placeholder: {
                    Rectangle().fill(.white.opacity(0.04))
                }.frame(width: 112, height: 78)
            } else {
                Image(systemName: "photo").frame(width: 112, height: 78).foregroundStyle(.secondary)
            }
            VStack(alignment: .leading, spacing: 5) {
                HStack {
                    Text(group.day).font(.headline)
                    Text("\(group.photoCount.formatted()) photos").foregroundStyle(.secondary)
                }
                Text("\(group.timeRange) · every \(group.intervalSeconds.formatted()) s")
                    .font(.caption).foregroundStyle(.secondary)
                Text("\(group.cameraModel) · \(group.lensModel)")
                    .font(.caption).foregroundStyle(.secondary).lineLimit(2)
                Text("\(group.firstFile) - \(group.lastFile)").font(.caption).foregroundStyle(.tertiary)
                if group.availableCount != group.photoCount {
                    Text("\(group.availableCount) currently available").font(.caption).foregroundStyle(.orange)
                }
                if !group.confirmed, !group.warnings.isEmpty {
                    Text(group.warnings.joined(separator: " ")).font(.caption).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
            VStack(alignment: .trailing, spacing: 10) {
                Button("View frames") { model.openTimelapse(group.id) }
                    .disabled(!model.service.isConnected || group.availableCount == 0)
                Button(group.confirmed ? "Ungroup" : "Group") {
                    Task { await model.setTimelapse(group, confirmed: !group.confirmed) }
                }.disabled(!model.workspaceWritable || model.timelapseBusy
                    || (!group.confirmed && group.availableCount != group.photoCount))
            }
        }
        .padding(12).background(.white.opacity(0.025), in: RoundedRectangle(cornerRadius: 10))
    }
}
