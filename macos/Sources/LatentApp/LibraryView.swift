import AppKit
import LatentCore
import SwiftUI

enum SidebarAccess: Equatable {
    case liveLibrary, retainedEditing

    func isEnabled(isConnected: Bool) -> Bool { isConnected || self == .retainedEditing }
}

struct LibraryView: View {
    @Bindable var model: LibraryModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var discardingAnnotations = false
    @State private var captionDraft = ""
    @State private var expandedYears: Set<String> = []
    @State private var expandedMonths: Set<String> = []
    @State private var referenceDropTargeted = false
    @State private var timelineExpanded = true
    @State private var sequencesExpanded = true
    @State private var showingFilters = false
    @State private var showingSearchHistory = false
    @State private var draftFilters = PhotoFilters()
    @State private var detailWidth: CGFloat = 1000

    var body: some View {
        NavigationSplitView {
            sidebar
                .navigationSplitViewColumnWidth(min: 180, ideal: 210, max: 460)
        } detail: {
            VStack(spacing: 0) {
                header
                if !model.service.isConnected { serviceBanner }
                if model.annotationQueue.errorMessage != nil || model.annotationQueue.hasUndurableEdits { annotationBanner }
                if let error = model.errorMessage { errorBanner(error) }
                if let batch = model.currentImport, !model.showingSequences {
                    HStack {
                        Text("\(batch.label) · \(batch.previewReady.formatted()) / \(batch.photoCount.formatted()) previews")
                            .font(.callout).foregroundStyle(.secondary)
                        Spacer()
                        Button("Refresh photos") {
                            model.navigate(to: model.source, preservingSelection: true, retainingPhotos: true, filters: model.searchFilters)
                        }
                        Button("Import details") { model.openImports() }
                    }.padding(.horizontal, 24).padding(.vertical, 8)
                }
                if model.showingSequences {
                    SequenceOverview(model: model)
                } else {
                    ZStack {
                        gallery
                            .opacity(model.previewPhoto == nil ? 1 : 0)
                            .allowsHitTesting(model.previewPhoto == nil)
                            .accessibilityHidden(model.previewPhoto != nil)
                        if model.previewPhoto != nil { PhotoPreview(model: model) }
                    }
                    .disabled(!model.service.isConnected)
                    .overlay(alignment: .bottomLeading) {
                        if model.showingPhotoCaption && model.selectedPhoto != nil {
                            PhotoCaptionCard(model: model, caption: $captionDraft)
                                .padding(.leading, 24).padding(.bottom, 12)
                        }
                    }
                    .background(PhotoInfoPanel(model: model,
                        requested: model.showingPhotoInfo && model.selectedPhoto != nil
                            && !model.showingEditing && !model.showingLocations && !model.showingTrash && !model.showingImports
                            && !model.showingTimelapses && model.sequenceEditor == nil && model.folderEditor == nil,
                        resetRequest: model.photoInfoPositionReset))
                    GeometryReader { geometry in
                        PhotoReviewBar(model: model, caption: $captionDraft, compact: geometry.size.width < 950)
                            .frame(maxWidth: 1060)
                            .opacity(model.selectedPhoto == nil ? 0 : 1)
                            .accessibilityHidden(model.selectedPhoto == nil)
                            .padding(.horizontal, 24)
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }.frame(height: 80)
                }
            }
            .frame(minWidth: 690)
            .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { detailWidth = $0 }
            .background(Color(nsColor: GalleryAppearance.canvas))
            .background(GalleryKeyboard(model: model).frame(width: 0, height: 0))
            .toolbarBackground(.hidden, for: .windowToolbar)
            .toolbar {
                ToolbarItem(placement: .principal) { searchField.disabled(!model.service.isConnected) }
            }
            .navigationTitle("")
        }
        .confirmationDialog("Discard pending edits?", isPresented: $discardingAnnotations) {
            Button("Discard pending edits", role: .destructive) { model.discardAnnotations() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Edits already saved in the library remain. Any edits still waiting to save will be discarded.")
        }
        .navigationSplitViewStyle(.balanced)
        .coordinateSpace(name: "sequence-organization")
        .overlay(alignment: .topLeading) { SequenceDragPreview(session: model.sequenceDrag) }
        .task { await model.monitorEditing() }
        .task { await model.monitorTrash() }
        .task { await model.monitorImports() }
        .onChange(of: model.service.state) { _, _ in model.serviceStateChanged() }
        .onChange(of: model.selectedID) { _, _ in
            captionDraft = model.selectedPhoto?.caption ?? ""
            model.showingPhotoCaption = false
        }
        .sheet(isPresented: $model.showingEditing) { EditingView(model: model) }
        .sheet(isPresented: $model.showingTrash) { PhotoTrashView(model: model) }
        .confirmationDialog("Move \(model.trashSelection?.photos.count ?? 0) photos to Latent Trash?", isPresented: Binding(
            get: { model.trashSelection != nil }, set: { if !$0 { model.trashSelection = nil } }
        ), presenting: model.trashSelection) { selection in
            Button("Move to Trash", role: .destructive) { model.moveSelectionToTrash(selection) }
            Button("Cancel", role: .cancel) { model.trashSelection = nil }
        } message: { _ in
            Text("The selected original files move to .Latent Trash in the same source folder, including CloudDrive mounts. You can restore them from Trash. Working copies and sidecars stay where they are.")
        }
        .sheet(isPresented: $model.showingLocations, onDismiss: {
            if model.importsAfterLocations {
                model.importsAfterLocations = false
                model.openImports()
            }
        }) { LocationsView(model: model) }
        .sheet(isPresented: $model.showingImports) { ImportsView(model: model) }
        .sheet(isPresented: $model.showingTimelapses) { TimelapseView(model: model) }
        .onChange(of: model.selectedDate, initial: true) { _, date in
            if let date {
                expandedYears.insert(String(date.prefix(4)))
                if date.count >= 7 { expandedMonths.insert(String(date.prefix(7))) }
            }
        }
        .sheet(item: $model.sequenceEditor) { draft in
            SequenceEditor(model: model, draft: draft)
        }
        .sheet(item: $model.folderEditor) { draft in
            SequenceFolderEditor(model: model, draft: draft)
        }
        .sheet(item: $model.movingSequenceItem) { draft in
            SequenceMoveSheet(model: model, draft: draft)
        }
    }

    private var sidebar: some View {
        VStack(spacing: 0) {
            List {
                Section {
                    sidebarButton("Imports", symbol: "square.and.arrow.down", selected: model.currentImport != nil && !model.showingSequences) {
                        model.openImports()
                    }
                    DisclosureGroup(isExpanded: $timelineExpanded) {
                        ForEach(model.years, id: \.self) { year in archiveYear(year) }
                    } label: {
                    sidebarButton("Timeline", symbol: "calendar",
                                  selected: model.libraryNavigationSelected) {
                        model.navigate(to: .library(date: nil))
                    }
                    }
                    sidebarButton("Starred", symbol: "star", selected: model.source == .starred && !model.showingSequences) {
                        model.navigate(to: .starred)
                    }
                    sidebarButton("Editing", symbol: "slider.horizontal.3", selected: false, access: .retainedEditing) {
                        model.showingEditing = true
                    }
                    sidebarButton("Locations", symbol: "externaldrive", selected: false) {
                        model.showingLocations = true
                    }
                    DisclosureGroup(isExpanded: $sequencesExpanded) {
                        SequenceSidebar(model: model)
                    } label: {
                    sidebarButton("Sequences", symbol: "rectangle.stack",
                                  selected: model.showingSequences && model.selectedSequenceFolderID == nil) { model.showSequences() }
                        .modifier(SequenceDropDestination(model: model, folderID: nil))
                        .contextMenu {
                            Button("New folder…") { model.openFolderEditor() }
                            Button("New sequence…") { model.newSequence(in: nil) }
                            Button("New smart sequence…") { model.newSmartSequence() }
                        }
                    }
                }
            }
            .listStyle(.sidebar)
            HStack {
                Text(model.summary.map { "\($0.cachedAssets.formatted()) photos" } ?? "Library not loaded")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button { model.showingTimelapses = true } label: { Image(systemName: "puzzlepiece.extension") }
                    .buttonStyle(.plain).help("Time-lapse organizer")
                    .accessibilityLabel("Time-lapse organizer")
                Button { model.showingTrash = true } label: { Image(systemName: "trash") }
                    .buttonStyle(.plain).help("Open Latent Trash").accessibilityLabel("Open Latent Trash")
                Menu {
                    Button("New sequence…") { model.openSequenceEditor() }
                    Button("New folder…") { model.openFolderEditor(parentID: model.showingSequences ? model.selectedSequenceFolderID : nil) }
                    Button("New smart sequence…") { model.newSmartSequence(folderID: model.showingSequences ? model.selectedSequenceFolderID : nil) }
                } label: { Image(systemName: "plus") }
                    .menuStyle(.borderlessButton).fixedSize()
                    .help("New sequence or folder")
                    .accessibilityLabel("New sequence or folder")
                    .disabled(!model.workspaceWritable || model.isSaving)
            }
            .padding(16)
        }
        .navigationTitle("")
    }

    private func archiveYear(_ year: String) -> some View {
        let months = Dictionary(grouping: model.summary?.dates.filter { $0.year == year } ?? [], by: \.month)
        return DisclosureGroup(isExpanded: Binding(
            get: { expandedYears.contains(year) },
            set: { if $0 { expandedYears.insert(year) } else { expandedYears.remove(year) } }
        )) {
            ForEach(months.keys.sorted(by: >), id: \.self) { month in
                archiveMonth(month, days: months[month] ?? [])
            }
        } label: {
            archiveButton(year, title: year, count: months.values.flatMap { $0 }.reduce(0) { $0 + $1.assetCount }, symbol: "calendar")
        }
    }

    private func archiveMonth(_ month: String, days: [CaptureDay]) -> some View {
        let count = days.reduce(0) { $0 + $1.assetCount }
        return DisclosureGroup(isExpanded: Binding(
            get: { expandedMonths.contains(month) },
            set: { if $0 { expandedMonths.insert(month) } else { expandedMonths.remove(month) } }
        )) {
            ForEach(days.sorted { $0.captureDate > $1.captureDate }) { day in
                archiveDay(day)
            }
        } label: {
            archiveButton(month, title: PhotoFormatting.month(month), count: count)
        }
    }

    private func archiveDay(_ day: CaptureDay) -> some View {
        archiveButton(day.captureDate, title: PhotoFormatting.dayOfMonth(day.captureDate), count: day.assetCount)
    }

    private func archiveButton(_ period: String, title: String, count: Int, symbol: String? = nil) -> some View {
        Button {
            model.navigate(to: .library(date: period))
        } label: {
            HStack {
                if let symbol { Image(systemName: symbol) }
                Text(title)
                Spacer(minLength: 6)
                Text(count.formatted()).foregroundStyle(.secondary).font(.caption)
            }
            .padding(.vertical, 2)
            .padding(.horizontal, 7)
            .background(model.selectedDate == period ? Color(nsColor: GalleryAppearance.accent).opacity(0.15) : .clear,
                        in: RoundedRectangle(cornerRadius: 7))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!SidebarAccess.liveLibrary.isEnabled(isConnected: model.service.isConnected))
        .accessibilityLabel("\(PhotoFormatting.archivePeriod(period)), \(count) photos")
        .accessibilityAddTraits(model.selectedDate == period ? .isSelected : [])
        .accessibilityIdentifier("date-\(period)")
    }

    private func sidebarButton(_ title: String, symbol: String, selected: Bool,
                               access: SidebarAccess = .liveLibrary,
                               action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: symbol)
                .fontWeight(selected ? .semibold : .regular)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.vertical, 4).padding(.horizontal, 7)
                .background(selected ? Color(nsColor: GalleryAppearance.accent).opacity(0.17) : .clear,
                            in: RoundedRectangle(cornerRadius: 8))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!access.isEnabled(isConnected: model.service.isConnected))
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private var searchField: some View {
        HStack(spacing: 9) {
            Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
            if let reference = model.referenceImage {
                HStack(spacing: 6) {
                    if let image = NSImage(data: reference.jpeg) {
                        Image(nsImage: image).resizable().scaledToFill().frame(width: 30, height: 26)
                            .clipShape(RoundedRectangle(cornerRadius: 5))
                    }
                    Text(reference.name).font(.caption).lineLimit(1).truncationMode(.middle).frame(maxWidth: 95)
                    Button { model.clearReferenceImage() } label: { Image(systemName: "xmark").font(.caption2) }
                        .buttonStyle(.plain).accessibilityLabel("Remove reference image")
                }
                .padding(4).background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 7))
                .accessibilityIdentifier("search-reference-image")
            }
            NativeSearchField(text: $model.searchText, focusRequest: model.focusSearchRequest,
                              placeholder: model.referenceImage == nil ? (model.summary?.searchPlaceholder ?? "Search indexed photos")
                                : model.summary?.embeddingIndex?.imageTextQueries == false ? "Press Return to search by image" : "Add words to refine…") {
                model.submitSearch()
            }
            .frame(height: 20)
            Button { showingSearchHistory.toggle() } label: { Image(systemName: "clock.arrow.circlepath") }
                .buttonStyle(.plain).foregroundStyle(.secondary)
                .accessibilityLabel("Search history").help("Reopen a saved text or image search")
                .popover(isPresented: $showingSearchHistory) {
                    SearchHistoryView(model: model) { entry in
                        showingSearchHistory = false
                        NSApp.keyWindow?.makeFirstResponder(nil)
                        model.replaySearch(entry)
                    }
                }
            Button {
                draftFilters = model.filtersForSearch
                showingFilters = true
            } label: {
                Image(systemName: model.searchFilters.isEmpty ? "line.3.horizontal.decrease.circle" : "line.3.horizontal.decrease.circle.fill")
            }
            .buttonStyle(.plain).accessibilityLabel("Search filters").help("Filter by date, rating and selection")
            .popover(isPresented: $showingFilters) {
                VStack(alignment: .leading, spacing: 14) {
                    Text("Search filters").font(.headline)
                    PhotoFilterControls(filters: $draftFilters)
                    Divider()
                    Button(model.currentImport == nil ? "Save filters as smart sequence…" : "Save filters for whole library…",
                           systemImage: "sparkles.rectangle.stack") {
                        showingFilters = false
                        model.newSmartSequence(filters: draftFilters)
                    }.buttonStyle(.borderless).disabled(!model.workspaceWritable || !draftFilters.validDateRange)
                    HStack {
                        Button("Reset") { draftFilters = .init() }
                        Spacer()
                        Button("Apply") { model.applySearchFilters(draftFilters); showingFilters = false }
                            .keyboardShortcut(.defaultAction)
                            .disabled(!draftFilters.validDateRange)
                    }
                }.controlSize(.small).padding(18).frame(width: 354)
            }
            if model.isPreparingReference { ProgressView().controlSize(.small) }
            Button { model.chooseReferenceImage() } label: { Image(systemName: "photo.badge.magnifyingglass") }
                .buttonStyle(.plain).foregroundStyle(.secondary)
                .help("Search with an image. A small preview is sent to Gemini. You can also drop an image here.")
                .accessibilityLabel("Search with an image").accessibilityIdentifier("image-search")
            if !model.searchText.isEmpty || model.referenceImage != nil {
                Button {
                    model.searchText = ""
                    model.navigate(to: .library(date: nil))
                } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary) }
                    .buttonStyle(.plain).accessibilityLabel("Clear search")
            }
        }
        .font(.system(size: 14))
        .padding(.horizontal, 13).padding(.vertical, 8)
        .frame(width: min(600, max(280, detailWidth * 0.55)))
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(referenceDropTargeted ? Color.accentColor : .clear, lineWidth: 2))
        .dropDestination(for: URL.self) { urls, _ in
            guard let url = urls.first, url.isFileURL else { return false }
            model.loadReferenceImage(url)
            return true
        } isTargeted: { referenceDropTargeted = $0 }
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 12) {
                    Text(model.title).font(.system(size: 19, weight: .semibold)).lineLimit(1)
                    if model.summary != nil {
                        Text(model.subtitle).foregroundStyle(.secondary).font(.system(size: 12)).lineLimit(1)
                    }
                }
                if let smart = model.activeSequence?.smartFilters {
                    Label(smart.summary, systemImage: "sparkles").font(.caption).foregroundStyle(.secondary)
                } else if !model.searchFilters.isEmpty {
                    Text(model.searchFilters.summary).font(.caption).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 18)
            if model.isLoading || model.isPaging || model.isPreviewPaging || model.annotationQueue.isSaving {
                ProgressView().controlSize(.small)
            }
            if let notice = model.notice {
                Text(notice).foregroundStyle(Color(nsColor: GalleryAppearance.accent)).font(.caption).lineLimit(1)
            }
            if case .timelapse = model.source, !model.showingSequences {
                Button("Back to photos", systemImage: "arrow.left") { model.closeTimelapse() }
                    .buttonStyle(.glass)
            } else if model.isSemanticSearch && !model.showingSequences {
                VStack(alignment: .trailing, spacing: 6) {
                    Text("Result order").font(.caption).foregroundStyle(.secondary)
                    Picker("Result order", selection: Binding(get: { model.searchOrder }, set: { model.setSearchOrder($0) })) {
                        ForEach(PhotoSearchOrder.allCases, id: \.self) { order in Text(order.label).tag(order) }
                    }
                    .pickerStyle(.menu).labelsHidden().frame(width: 166)
                    .accessibilityIdentifier("result-order")
                }
            } else if let sequence = model.activeSequence, !model.showingSequences {
                Button("Edit sequence", systemImage: "pencil") { model.openSequenceEditor(editing: sequence) }
                    .buttonStyle(.glass).disabled(!model.workspaceWritable)
            }
            if !model.showingSequences {
                HStack(spacing: 3) {
                    Button { finishTextEntry(); model.stepPhoto(by: -1) } label: { Image(systemName: "chevron.left").frame(width: 28, height: 28) }
                        .disabled(!canStepPhoto(-1)).help("Previous photo (W)").accessibilityLabel("Previous photo")
                    Button { finishTextEntry(); model.stepPhoto(by: 1) } label: { Image(systemName: "chevron.right").frame(width: 28, height: 28) }
                        .disabled(!canStepPhoto(1)).help("Next photo (E)").accessibilityLabel("Next photo")
                    Button { finishTextEntry(); model.togglePreview() } label: {
                        Image(systemName: model.previewPhoto == nil ? "arrow.up.left.and.arrow.down.right" : "square.grid.2x2")
                            .frame(width: 28, height: 28)
                    }.disabled(model.selectedPhoto == nil)
                        .help(model.previewPhoto == nil ? "Open preview (Space)" : "Back to gallery (Space)")
                        .accessibilityLabel(model.previewPhoto == nil ? "Open preview" : "Back to gallery")
                        .accessibilityIdentifier("toggle-preview")
                }.buttonStyle(.plain).foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 24).frame(height: 56)
        .accessibilityIdentifier("gallery-context-bar")
    }

    private func canStepPhoto(_ offset: Int) -> Bool {
        if model.previewPhoto != nil { return model.canMovePreview(by: offset) }
        guard let id = model.selectedID, let index = model.photos.firstIndex(where: { $0.id == id }) else { return false }
        return model.photos.indices.contains(index + offset)
    }

    private func finishTextEntry() { NSApp.keyWindow?.makeFirstResponder(nil) }

    private var gallery: some View {
        ZStack {
            MasonryGallery(model: model)
            if model.photos.isEmpty && !model.service.isConnected {
                if model.service.isConnecting {
                    ProgressView("Opening your library…").controlSize(.regular)
                } else {
                    ContentUnavailableView("Library not connected", systemImage: "externaldrive.badge.exclamationmark",
                                           description: Text("Reconnect to load your photos."))
                }
            } else if model.photos.isEmpty && model.summary != nil && !model.isLoading && model.errorMessage == nil {
                ContentUnavailableView("No photos here", systemImage: "photo.on.rectangle.angled",
                                       description: Text(model.activeSequence?.smartFilters != nil
                                           ? "Photos appear here when they match this sequence’s filters."
                                           : model.activeSequence == nil
                                           ? "Try another date or search your archive."
                                           : "Add photos from your library to begin this sequence."))
            }
            if model.photos.isEmpty && model.service.isConnected
                && (model.isLoading || (model.summary == nil && model.errorMessage == nil)) {
                ProgressView("Loading photos…").controlSize(.regular)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var annotationBanner: some View {
        HStack(spacing: 10) {
            if model.annotationQueue.isSaving { ProgressView().controlSize(.small) }
            VStack(alignment: .leading, spacing: 4) {
                Text(model.annotationQueue.errorMessage
                     ?? "Saving edits for \(model.annotationQueue.pendingCount.formatted()) photos…")
                if model.annotationQueue.hasUndurableEdits {
                    Text("Some edits are only in memory. Keep Latent open until saving succeeds.").foregroundStyle(.orange)
                }
            }.font(.callout).textSelection(.enabled)
            Spacer()
            if model.annotationQueue.errorMessage != nil {
                Button("Retry saving") { model.retryAnnotations() }
                    .disabled(model.annotationQueue.isSaving || !model.service.isConnected)
                Button("Discard pending edits…") { discardingAnnotations = true }
                    .disabled(!model.annotationQueue.canDiscard)
            }
        }
        .padding(12).background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 10))
        .padding(.horizontal, 24).padding(.top, 12)
    }

    private func errorBanner(_ message: String) -> some View {
        HStack(alignment: .center, spacing: 10) {
            Image(systemName: "exclamationmark.circle").foregroundStyle(.orange)
            Text(message).font(.callout).textSelection(.enabled)
            Spacer()
            Button("Retry") { model.refresh() }
            Button { model.errorMessage = nil } label: { Image(systemName: "xmark") }
                .buttonStyle(.plain).accessibilityLabel("Dismiss error")
        }
        .padding(12).background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 10))
        .padding(.horizontal, 24).padding(.top, 12)
    }

    private var serviceBanner: some View {
        HStack(spacing: 10) {
            if model.service.isConnecting { ProgressView().controlSize(.small) }
            Text(model.service.state.message).font(.callout).textSelection(.enabled)
            Spacer()
            if !model.pendingSequenceDrafts.isEmpty {
                Menu("Resume draft") {
                    ForEach(model.pendingSequenceDrafts) { draft in
                        Button(draft.name.isEmpty ? "Untitled sequence" : draft.name) { model.sequenceEditor = draft }
                    }
                }
            }
            Button("Retry connection") { model.refresh() }
                .disabled(model.service.isConnecting)
        }
        .padding(12).background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 10))
        .padding(.horizontal, 24).padding(.top, 12)
    }


}

private struct SequenceEditor: View {
    @Environment(\.dismiss) private var dismiss
    @Bindable var model: LibraryModel
    @Bindable var draft: SequenceDraft
    @State private var discarding = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text(draft.smartFilters != nil ? (draft.editing == nil ? "New smart sequence" : "Edit smart sequence") : (draft.editing == nil ? "New sequence" : "Edit sequence")).font(.title2.weight(.semibold))
            if !draft.adding.isEmpty { Text("Start with \(draft.adding.count) selected \(draft.adding.count == 1 ? "photo" : "photos")").font(.callout).foregroundStyle(.secondary) }
            Text(model.folderPath(draft.editing == nil ? draft.folderID : model.sequences.first { $0.id == draft.editing?.id }?.folderId))
                .font(.caption).foregroundStyle(.secondary).lineLimit(3)
            SheetConnectionBanner(model: model)
            TextField("Name", text: $draft.name).textFieldStyle(.roundedBorder)
                .accessibilityIdentifier("sequence-name").disabled(draft.isSaving)
            TextField("Note (optional)", text: $draft.note, axis: .vertical)
                .lineLimit(3...5).textFieldStyle(.roundedBorder).disabled(draft.isSaving)
            if draft.smartFilters != nil {
                Text("Photos update automatically from these filters.").font(.callout).foregroundStyle(.secondary)
                PhotoFilterControls(filters: Binding(get: { draft.smartFilters ?? .init() }, set: { draft.smartFilters = $0 }))
                    .disabled(draft.isSaving)
                if draft.editing == nil {
                    Picker("Folder", selection: Binding(get: { draft.folderID ?? "" }, set: { draft.folderID = $0.isEmpty ? nil : $0 })) {
                        Text("Sequences").tag("")
                        ForEach(model.sequenceFolders) { folder in Text(model.folderPath(folder.id)).tag(folder.id) }
                    }.disabled(draft.isSaving || draft.confirmed != nil)
                }
            }
            if let error = draft.errorMessage { Text(error).font(.callout).foregroundStyle(.orange).textSelection(.enabled) }
            Text("Closing keeps this draft while Latent is open.").font(.caption).foregroundStyle(.secondary)
            HStack {
                Button("Discard draft…") { discarding = true }.disabled(draft.isSaving)
                Spacer()
                Button("Close") { dismiss() }.keyboardShortcut(.cancelAction)
                Button(draft.editing == nil ? "Create" : "Save") {
                    Task { _ = await model.saveSequence(draft) }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(draft.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                          || model.isSaving || draft.isSaving || !model.workspaceWritable
                          || draft.smartFilters?.validDateRange == false)
            }
        }
        .padding(28).frame(width: 470)
        .confirmationDialog("Discard this sequence draft?", isPresented: $discarding) {
            Button("Discard draft", role: .destructive) { model.discardSequenceDraft(draft) }
            Button("Keep draft", role: .cancel) {}
        } message: {
            Text("Unsaved text will be discarded. Any sequence or photos already saved in the library remain.")
        }
    }
}
