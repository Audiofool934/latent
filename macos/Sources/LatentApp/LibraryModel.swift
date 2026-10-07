import AppKit
import LatentCore
import Observation
import OSLog
import UniformTypeIdentifiers

private struct AcknowledgedAnnotation {
    let target: AnnotationTarget
    var rating: (revision: Int, value: Int?)?
    var caption: (revision: Int, value: String?)?
    var flag: (revision: Int, value: PhotoFlag?)?
}

@MainActor @Observable
final class LibraryModel {
    let client: LibraryClient
    let service: ServiceConnection
    let preferences: UserDefaults
    var summary: LibrarySummary?
    var photos: [Photo] = []
    var sequences: [PhotoSequence] = []
    var sequenceFolders: [SequenceFolder] = []
    var selectedSequenceFolderID: String?
    var expandedSequenceFolders: Set<String> = []
    var folderEditor: SequenceFolderDraft?
    var movingSequenceItem: SequenceMoveDraft?
    var organizationBusy = false
    let sequenceDrag = SequenceDragSession()
    @ObservationIgnored var organizationDataID: String?
    var activeSequence: PhotoSequence?
    var source: GallerySource = .library(date: nil)
    var showingSequences = false
    var selectedID: Int? {
        didSet {
            if let selectedID {
                if !selectedIDs.contains(selectedID) { selectedIDs = [selectedID] }
            } else { selectedIDs = [] }
        }
    }
    var selectedIDs: Set<Int> = []
    var selectionAnchor: Int?
    var batches: [EditingBatch] = []
    var showingEditing = false
    var showingTrash = false
    var trashSelection: PhotoTrashSelection?
    var trashBatches: [PhotoTrashBatch] = []
    var trashBusy = false
    var trashError: String?
    @ObservationIgnored private var trashDataID: String?
    @ObservationIgnored private var trashGeneration = 0
    var showingLocations = false
    var importsAfterLocations = false
    var showingImports = false
    var importTab = "imports"
    var photoImports: [PhotoImport] = []
    var embeddingRuns: [PhotoEmbeddingRun] = []
    var workflowBusy = false
    var workflowError: String?
    var embeddingReview: PhotoEmbeddingRun?
    var searchEngines: SearchEngineList?
    @ObservationIgnored var workflowDataID: String?
    @ObservationIgnored var workflowGeneration = 0
    var showingTimelapses = false
    var collapseTimelapses = false
    var timelapseGroups: [TimelapseGroup] = []
    var timelapseBusy = false
    var timelapseError: String?
    @ObservationIgnored var timelapseDataID: String?
    @ObservationIgnored var timelapseGeneration = 0
    var expandedPhotoTotal: Int?
    var timelapseReturnSource: GallerySource = .library(date: nil)
    var timelapseReturnFilters = PhotoFilters()
    var locations: LibraryLocations?
    var locationsBusy = false
    var locationsError: String?
    var locationsNotice: String?
    @ObservationIgnored var locationsGeneration = 0
    var editingErrorMessage: String?
    var sequenceEditor: SequenceDraft?
    private var sequenceDrafts: [String: SequenceDraft] = [:]
    private var editingPolicies: [String: String] = [:]
    @ObservationIgnored private var libraryDataID: String?
    @ObservationIgnored private var editingDataID: String?
    @ObservationIgnored private var editingRevision = 0
    var editingBusy = false
    var openingBatchID: String?
    var previewPhoto: Photo? {
        didSet {
            if oldValue?.id != previewPhoto?.id { cancelPreviewNavigation() }
        }
    }
    var isPreviewPaging = false
    var showingPhotoInfo = false {
        didSet { preferences.set(showingPhotoInfo, forKey: "showingPhotoInfo") }
    }
    var photoInfoPositionReset = UUID()
    var showingPhotoCaption = false
    var previewPagingError: String?
    var searchText = ""
    var searchFilters = PhotoFilters()
    var searchOrder: PhotoSearchOrder
    var referenceImage: SearchReference?
    var searchHistory: [SearchHistoryEntry] = []
    var searchHistoryLoading = false
    var searchHistoryError: String?
    var searchQueryWasCached = false
    @ObservationIgnored var searchHistoryDataID: String?
    @ObservationIgnored var searchHistoryGeneration = 0
    @ObservationIgnored private var activeSearchHistoryID: String?
    var isPreparingReference = false
    @ObservationIgnored private var referenceTask: Task<Void, Never>?
    @ObservationIgnored private var referenceRequest = UUID()
    var minimumColumnWidth: Double
    var isLoading = false
    var isPaging = false
    private var savingSequence = false
    var isSaving: Bool { savingSequence || organizationBusy || annotationQueue.isSaving }
    let annotationQueue: AnnotationQueue
    var total = 0
    var nextOffset: Int?
    var errorMessage: String?
    var notice: String?
    var focusSearchRequest = UUID()
    var datasetID = UUID()
    var selectionRequest = UUID()

    @ObservationIgnored private var reloadingStarredAfterSave = false
    @ObservationIgnored private var annotationRevision = 0
    @ObservationIgnored private var photoReads: [UUID: Int] = [:]
    @ObservationIgnored private var acknowledgedAnnotations: [Int: AcknowledgedAnnotation] = [:]
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var navigationRevision = 0
    @ObservationIgnored var sequenceRevision = 0
    @ObservationIgnored private var loadTask: Task<Void, Never>?
    @ObservationIgnored private var pageTask: Task<Void, Never>?
    @ObservationIgnored private var pageErrorMessage: String?
    @ObservationIgnored private var previewMoveTask: Task<Void, Never>?
    @ObservationIgnored private var previewMoveID = UUID()
    @ObservationIgnored private var noticeTask: Task<Void, Never>?
    @ObservationIgnored private var starting = false
    @ObservationIgnored private let log = Logger(subsystem: "io.audiofool.Latent", category: "Library")

    init(client: LibraryClient, preferences: UserDefaults = .standard, service: ServiceConnection? = nil,
         annotationStore: any AnnotationDraftStore = MemoryAnnotationDraftStore()) {
        self.client = client
        let connection = service ?? ServiceConnection(client: client)
        self.service = connection
        annotationQueue = AnnotationQueue(client: client, service: connection, store: annotationStore)
        self.preferences = preferences
        showingPhotoInfo = preferences.bool(forKey: "showingPhotoInfo")
        searchOrder = preferences.string(forKey: "searchOrder").flatMap(PhotoSearchOrder.init(rawValue:))
            ?? (preferences.bool(forKey: "moreVariety") ? .variety : .closest)
        let savedWidth = preferences.double(forKey: "minimumColumnWidth")
        minimumColumnWidth = savedWidth > 0 ? min(420, max(190, savedWidth)) : 224
        annotationQueue.didSave = { [weak self] updated, drained, rating, caption, flag in
            self?.annotationsSaved(updated, drained: drained, rating: rating, caption: caption, flag: flag)
        }
    }

    var selectedPhotos: [Photo] { photos.filter { selectedIDs.contains($0.id) } }
    var selectedPhoto: Photo? { photos.first { $0.id == selectedID } }
    var isSemanticSearch: Bool {
        switch source { case .search, .imageSearch, .similar: true; default: false }
    }
    var libraryNavigationSelected: Bool {
        guard !showingSequences else { return false }
        switch source {
        case .library(let date): return date == nil
        case .search, .imageSearch, .similar: return true
        case .sequence, .starred, .timelapse, .importBatch: return false
        }
    }
    var selectedDate: String? { if case .library(let date) = source, !showingSequences { date } else { nil } }
    var workspaceWritable: Bool {
        service.isConnected && libraryDataID == service.identity?.dataId && summary?.workspace.writable == true
    }
    var years: [String] { Array(Set(summary?.dates.map(\.year) ?? [])).sorted(by: >) }

    var title: String {
        if showingSequences { return sequenceHierarchy.folders[selectedSequenceFolderID ?? ""]?.name ?? "Sequences" }
        switch source {
        case .library(let date): return date.map(PhotoFormatting.archivePeriod) ?? (searchFilters.isEmpty ? "Timeline" : "Filtered photos")
        case .starred: return "Starred"
        case .search: return "Search results"
        case .imageSearch: return "Image search"
        case .similar: return "Similar photos"
        case .sequence: return activeSequence?.name ?? "Sequence"
        case .timelapse: return "Time-lapse frames"
        case .importBatch(_, let name): return name
        }
    }

    var subtitle: String {
        if showingSequences { return sequenceFolderSummary(selectedSequenceFolderID) }
        switch source {
        case .search, .imageSearch: return "\(total.formatted()) matches across your archive"
        case .similar(_, let name, _): return "\(total.formatted()) photos related to \(name)"
        case .sequence:
            if activeSequence?.smartFilters != nil {
                return nextOffset == nil ? "\(total.formatted()) photos" : "\(photos.count.formatted()) of \(total.formatted()) photos"
            }
            let missing = (activeSequence?.itemCount ?? total) - photos.count
            return missing > 0 ? "\(total) photos · \(missing) unavailable in this index" : "\(total.formatted()) photos"
        case .library, .starred, .timelapse, .importBatch:
            if let expandedPhotoTotal, expandedPhotoTotal > total {
                let shown = nextOffset == nil ? total.formatted() : "\(photos.count.formatted()) of \(total.formatted())"
                return "\(shown) items · \(expandedPhotoTotal.formatted()) photos"
            }
            return nextOffset == nil ? "\(total.formatted()) photos" : "\(photos.count.formatted()) of \(total.formatted()) photos"
        }
    }

    func start() async {
        guard !starting else { return }
        starting = true
        defer { starting = false }
        let reconnecting = summary != nil || navigationRevision > 0
        let startingNavigation = navigationRevision
        isLoading = true
        errorMessage = nil
        do {
            try await service.connect()
            try requireServiceConnection()
            let startingDataID = service.identity?.dataId
            sequenceRevision += 1
            let startingSequences = sequenceRevision
            async let library = client.library()
            async let saved = client.sequenceLibrary()
            let (libraryResult, sequenceResult) = try await (library, saved)
            try requireServiceConnection()
            guard service.identity?.dataId == startingDataID else {
                throw ServiceConnectionError("The library connection changed. Reconnect to load its current contents.")
            }
            summary = libraryResult
            libraryDataID = startingDataID
            if let startingDataID {
                collapseTimelapses = preferences.bool(forKey: "collapseTimelapses.\(startingDataID)")
                if timelapseDataID != startingDataID { timelapseGroups = []; timelapseDataID = nil }
            }
            if sequenceRevision == startingSequences { applySequenceLibrary(sequenceResult, dataID: startingDataID) }
            // Loading sidebar metadata must not replace a choice made while it was pending.
            guard navigationRevision == startingNavigation else { return }
            if reconnecting {
                if showingSequences { isLoading = false }
                else { navigate(to: source, filters: searchFilters) }
                return
            }
            let initialQuery = ProcessInfo.processInfo.argument(after: "--initial-query")
            if let initialQuery {
                searchText = initialQuery
                submitSearch()
            } else {
                let requested = ProcessInfo.processInfo.argument(after: "--initial-date")
                    ?? preferences.string(forKey: "lastCaptureDate")
                let date = requested.flatMap { period in
                    [4, 7, 10].contains(period.count) && libraryResult.dates.contains { $0.captureDate.hasPrefix(period) }
                        ? period : nil
                } ?? libraryResult.dates.first?.captureDate
                navigate(to: .library(date: date))
            }
        } catch {
            if navigationRevision == startingNavigation { isLoading = false }
            if service.isConnected { present(error) }
        }
    }

    func navigate(to newSource: GallerySource, preservingSelection: Bool = false, retainingPhotos: Bool = false,
                  filters: PhotoFilters? = nil, cachedSearchID: String? = nil) {
        guard service.isConnected else { refresh(); return }
        let reusableID = cachedSearchID ?? (Self.sameSearch(source, newSource) ? activeSearchHistoryID : nil)
        let searchDataID = service.identity?.dataId
        activeSearchHistoryID = nil
        searchQueryWasCached = false
        if let filters { searchFilters = filters }
        else if !preservingSelection {
            switch newSource { case .library, .starred, .sequence, .timelapse, .importBatch: searchFilters = .init(); default: break }
        }
        let requestFilters = searchFilters
        let requestCollapse = collapseTimelapses
        cancelPreviewNavigation()
        if !retainingPhotos { previewPhoto = nil; selectionAnchor = nil }
        let previousSelection = preservingSelection ? selectedIDs : []
        let previousPrimary = preservingSelection ? selectedID : nil
        let previousOrder = preservingSelection ? photos.map(\.id) : []
        reloadingStarredAfterSave = retainingPhotos && newSource == .starred
        navigationRevision += 1
        generation += 1
        let requestGeneration = generation
        loadTask?.cancel()
        pageTask?.cancel()
        isPaging = false
        source = newSource
        showingSequences = false
        errorMessage = nil
        notice = nil
        nextOffset = nil
        activeSequence = nil
        expandedPhotoTotal = nil
        if !retainingPhotos {
            photos = []
            selectedID = nil
            total = 0
            datasetID = UUID()
        }
        isLoading = true
        if case .library(let date) = newSource {
            preferences.set(date, forKey: "lastCaptureDate")
            searchText = ""
        }
        switch newSource {
        case .search(let query, let order): referenceImage = nil; searchText = query; searchOrder = order
        case .imageSearch(let reference, let query, let order):
            referenceImage = reference; searchText = query; searchOrder = order
        default: referenceImage = nil; searchText = ""
        }
        loadTask = Task {
            let read = beginPhotoRead()
            defer { finishPhotoRead(read) }
            let start = ContinuousClock.now
            do {
                try requireServiceConnection()
                let result: [Photo]
                var resultTotal: Int
                var followingOffset: Int?
                var expandedTotal: Int?
                var sequence: PhotoSequence?
                var completedSearch: SearchResults?
                switch newSource {
                case .library(let date):
                    try requireServiceConnection()
                    let page = try await client.photos(date: date, filters: requestFilters, collapseTimelapses: requestCollapse)
                    result = page.assets
                    resultTotal = page.total
                    followingOffset = page.nextOffset
                    expandedTotal = page.expandedTotal
                case .timelapse(let id):
                    let page = try await client.photos(date: nil, timelapseGroup: id)
                    result = page.assets
                    resultTotal = page.total
                    followingOffset = page.nextOffset
                case .importBatch(let id, _):
                    let page = try await client.photos(date: nil, filters: requestFilters, importID: id)
                    result = page.assets
                    resultTotal = page.total
                    followingOffset = page.nextOffset
                case .starred:
                    try requireServiceConnection()
                    let page = try await client.starred()
                    result = page.assets
                    resultTotal = page.total
                    followingOffset = page.nextOffset
                case .search(let query, let order):
                    try requireServiceConnection()
                    let response: SearchResults
                    if let reusableID, let searchDataID {
                        response = try await client.replaySearch(id: reusableID, filters: requestFilters, order: order, dataID: searchDataID)
                    } else { response = try await client.search(query, order: order, filters: requestFilters) }
                    completedSearch = response
                    result = response.results
                    resultTotal = response.total
                case .imageSearch(let reference, let query, let order):
                    try requireServiceConnection()
                    let response: SearchResults
                    if let reusableID, let searchDataID {
                        response = try await client.replaySearch(id: reusableID, filters: requestFilters, order: order, dataID: searchDataID)
                    } else { response = try await client.search(image: reference, query: query, order: order, filters: requestFilters) }
                    completedSearch = response
                    result = response.results
                    resultTotal = response.total
                case .similar(let id, _, let order):
                    try requireServiceConnection()
                    let response = try await client.similar(to: id, order: order, filters: requestFilters)
                    result = response.results
                    resultTotal = response.total
                case .sequence(let id):
                    try requireServiceConnection()
                    let response = try await client.sequence(id: id)
                    sequence = response
                    result = response.items?.compactMap(\.asset) ?? []
                    resultTotal = response.itemCount
                    followingOffset = response.nextOffset
                }
                try Task.checkCancellation()
                guard generation == requestGeneration else { return }
                activeSearchHistoryID = completedSearch?.historyId
                searchQueryWasCached = completedSearch?.queryCached == true
                let previousSelection = retainingPhotos ? selectedIDs : previousSelection
                let previousPrimary = retainingPhotos ? selectedID : previousPrimary
                let previousOrder = retainingPhotos ? photos.map(\.id) : previousOrder
                photos = reconcilePhotoRead(result, read: read)
                total = resultTotal
                expandedPhotoTotal = expandedTotal
                nextOffset = newSource == .starred && annotationQueue.hasPendingEdits ? nil : followingOffset
                reloadingStarredAfterSave = false
                activeSequence = sequence
                let available = Set(result.map(\.id))
                let surviving = previousSelection.intersection(available)
                if !surviving.isEmpty {
                    setSelection(surviving, primary: previousPrimary)
                } else if let previousPrimary, let index = previousOrder.firstIndex(of: previousPrimary) {
                    selectedID = previousOrder.dropFirst(index + 1).first(where: available.contains)
                        ?? previousOrder.prefix(index).last(where: available.contains) ?? result.first?.id
                } else {
                    selectedID = result.first?.id
                }
                if let id = previewPhoto?.id { previewPhoto = photos.first(where: { $0.id == id }) ?? selectedPhoto }
                isLoading = false
                log.info("Loaded \(result.count) photos in \(String(describing: start.duration(to: .now)), privacy: .public)")
            } catch {
                guard generation == requestGeneration, !Task.isCancelled else { return }
                isLoading = false
                present(error)
            }
        }
    }

    func submitSearch() {
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty || referenceImage != nil else { navigate(to: .library(date: nil), filters: searchFilters); return }
        guard query.count <= 200 else {
            errorMessage = "Keep your search to 200 characters or fewer."
            return
        }
        if let referenceImage {
            if !query.isEmpty, summary?.embeddingIndex?.imageTextQueries == false {
                showNotice("Remove the words: this search engine matches by image only")
                return
            }
            navigate(to: .imageSearch(reference: referenceImage, query: query, order: searchOrder))
        } else {
            navigate(to: .search(query: query, order: searchOrder))
        }
    }

    private static func sameSearch(_ first: GallerySource, _ second: GallerySource) -> Bool {
        switch (first, second) {
        case let (.search(a, _), .search(b, _)): a == b
        case let (.imageSearch(a, qa, _), .imageSearch(b, qb, _)): a.jpeg == b.jpeg && qa == qb
        default: false
        }
    }

    func replaySearch(_ entry: SearchHistoryEntry) {
        guard entry.reusable, let dataID = service.identity?.dataId, dataID == searchHistoryDataID else { return }
        referenceTask?.cancel()
        let request = UUID()
        referenceRequest = request
        let navigation = navigationRevision
        isPreparingReference = true
        errorMessage = nil
        referenceTask = Task {
            defer { if referenceRequest == request { isPreparingReference = false; referenceTask = nil } }
            do {
                let reference = try await client.searchHistoryReference(entry)
                try Task.checkCancellation()
                guard navigationRevision == navigation, referenceRequest == request,
                      service.identity?.dataId == dataID else { return }
                let destination: GallerySource = reference.map {
                    .imageSearch(reference: $0, query: entry.query, order: entry.order)
                } ?? .search(query: entry.query, order: entry.order)
                navigate(to: destination, filters: entry.filters, cachedSearchID: entry.id)
            } catch {
                if !Task.isCancelled, navigationRevision == navigation { errorMessage = error.localizedDescription }
            }
        }
    }

    func refreshSearchHistory() async {
        guard service.isConnected, let dataID = service.identity?.dataId else { return }
        searchHistoryGeneration += 1
        let generation = searchHistoryGeneration
        if searchHistoryDataID != dataID { searchHistory = [] }
        searchHistoryDataID = dataID
        searchHistoryLoading = true
        searchHistoryError = nil
        defer { if searchHistoryGeneration == generation { searchHistoryLoading = false } }
        do {
            let entries = try await client.searchHistory()
            guard generation == searchHistoryGeneration, service.identity?.dataId == dataID else { return }
            searchHistory = entries
        } catch {
            if generation == searchHistoryGeneration { searchHistoryError = error.localizedDescription }
        }
    }

    func removeSearchHistory(_ entry: SearchHistoryEntry) async {
        guard let dataID = service.identity?.dataId, dataID == searchHistoryDataID else { return }
        searchHistoryGeneration += 1
        searchHistoryLoading = false
        do {
            try await client.deleteSearchHistory(id: entry.id, dataID: dataID)
            guard service.identity?.dataId == dataID else { return }
            searchHistory.removeAll { $0.id == entry.id }
            if activeSearchHistoryID == entry.id { activeSearchHistoryID = nil }
        } catch {
            if service.identity?.dataId == dataID { searchHistoryError = error.localizedDescription }
        }
    }

    func setSearchOrder(_ order: PhotoSearchOrder) {
        searchOrder = order
        preferences.set(order.rawValue, forKey: "searchOrder")
        switch source {
        case .search(let query, _): navigate(to: .search(query: query, order: order))
        case .imageSearch(let reference, let query, _):
            navigate(to: .imageSearch(reference: reference, query: query, order: order))
        case .similar(let id, let name, _): navigate(to: .similar(id: id, name: name, order: order))
        default: break
        }
    }

    func chooseReferenceImage() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.image]
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.title = "Search with an image"
        panel.prompt = "Search"
        panel.message = "Find photos across your archive. A small 512 px preview is sent to Gemini for this search."
        Task {
            let result: NSApplication.ModalResponse
            if let window = NSApp.keyWindow { result = await panel.beginSheetModal(for: window) }
            else { result = await panel.begin() }
            if result == .OK, let url = panel.url { loadReferenceImage(url) }
        }
    }

    func loadReferenceImage(_ url: URL) {
        referenceTask?.cancel()
        let request = UUID()
        referenceRequest = request
        let navigation = navigationRevision
        isPreparingReference = true
        errorMessage = nil
        referenceTask = Task {
            defer { if referenceRequest == request { isPreparingReference = false; referenceTask = nil } }
            do {
                let reference = try await Task.detached(priority: .userInitiated) { try ReferenceImageLoader.load(url) }.value
                try Task.checkCancellation()
                guard navigationRevision == navigation, referenceRequest == request else { return }
                referenceImage = reference
                submitSearch()
            } catch {
                if !Task.isCancelled, navigationRevision == navigation { errorMessage = error.localizedDescription }
            }
        }
    }

    func clearReferenceImage() {
        referenceRequest = UUID()
        referenceTask?.cancel()
        isPreparingReference = false
        referenceImage = nil
        submitSearch()
    }

    @discardableResult
    func loadMore() -> Task<Void, Never>? {
        guard service.isConnected, !isLoading else { return nil }
        switch source {
        case .library, .starred, .timelapse, .importBatch: break
        case .sequence where activeSequence?.smartFilters != nil: break
        default: return nil
        }
        if isPaging { return pageTask }
        guard let offset = nextOffset else { return nil }
        let date = selectedDate
        let starred = source == .starred
        let requestFilters = activeSequence?.smartFilters ?? searchFilters
        let requestCollapse: Bool = if case .library = source { collapseTimelapses } else { false }
        let requestGroup: String? = if case .timelapse(let id) = source { id } else { nil }
        let requestImport: String? = if case .importBatch(let id, _) = source { id } else { nil }
        let requestGeneration = generation
        if let pageErrorMessage, errorMessage == pageErrorMessage { errorMessage = nil }
        pageErrorMessage = nil
        isPaging = true
        pageTask = Task {
            let read = beginPhotoRead()
            defer { finishPhotoRead(read) }
            do {
                try requireServiceConnection()
                let page = try await starred ? client.starred(offset: offset) : client.photos(
                    date: date, offset: offset, filters: requestFilters,
                    collapseTimelapses: requestCollapse, timelapseGroup: requestGroup, importID: requestImport)
                try Task.checkCancellation()
                guard generation == requestGeneration else { return }
                let existing = Set(photos.map(\.id))
                photos.append(contentsOf: reconcilePhotoRead(page.assets, read: read).filter { !existing.contains($0.id) })
                total = page.total
                expandedPhotoTotal = page.expandedTotal
                nextOffset = page.nextOffset
                isPaging = false
            } catch {
                guard generation == requestGeneration, !Task.isCancelled else { return }
                isPaging = false
                present(error)
                pageErrorMessage = errorMessage
            }
        }
        return pageTask
    }

    func showSequences(folderID: String? = nil) {
        guard service.isConnected else { return }
        cancelPreviewNavigation()
        navigationRevision += 1
        generation += 1
        loadTask?.cancel()
        pageTask?.cancel()
        isLoading = false
        isPaging = false
        showingSequences = true
        previewPhoto = nil
        selectedSequenceFolderID = folderID
        expandSequenceAncestors(of: folderID)
        selectedID = nil
        errorMessage = nil
        Task { await refreshSequences() }
    }

    func refresh() {
        if summary == nil || !service.isConnected { Task { await start() } }
        else {
            Task { await refreshCatalogSummary() }
            if showingSequences { Task { await refreshSequences() } }
            else { navigate(to: source, filters: searchFilters) }
        }
    }

    /// Saved query vectors belong to one engine, so an active search is re-run fresh after a switch.
    func searchEngineChanged() async {
        activeSearchHistoryID = nil
        searchQueryWasCached = false
        await refreshCatalogSummary()
        await refreshSearchHistory()
        switch source {
        case .search, .imageSearch: navigate(to: source, preservingSelection: true, filters: searchFilters)
        default: break
        }
    }

    func refreshCatalogSummary() async {
        guard service.isConnected, let dataID = service.identity?.dataId else { return }
        do {
            let current = try await client.library()
            guard service.isConnected, service.identity?.dataId == dataID else { return }
            summary = current
        } catch { errorMessage = error.localizedDescription }
    }

    func serviceStateChanged() {
        guard !service.isConnected else { return }
        timelapseGeneration += 1
        timelapseBusy = false
        cancelPreviewNavigation()
        annotationQueue.connectionLost()
        editingRevision += 1
        generation += 1
        loadTask?.cancel()
        pageTask?.cancel()
        isLoading = false
        isPaging = false
    }

    func shutdownService() {
        service.shutdown()
        serviceStateChanged()
    }

    func findSimilar() {
        guard let selectedPhoto else { return }
        navigate(to: .similar(id: selectedPhoto.id, name: selectedPhoto.name))
    }

    func setSelection(_ ids: Set<Int>, primary: Int? = nil) {
        selectedIDs = ids
        selectedID = primary.flatMap { ids.contains($0) ? $0 : nil }
            ?? selectedID.flatMap { ids.contains($0) ? $0 : nil } ?? photos.first(where: { ids.contains($0.id) })?.id
    }

    func selectPhoto(_ id: Int, extending: Bool = false, toggling: Bool = false) {
        guard let index = photos.firstIndex(where: { $0.id == id }) else { return }
        if extending, let anchor = selectionAnchor ?? selectedID,
           let start = photos.firstIndex(where: { $0.id == anchor }) {
            let range = Set(photos[min(start, index)...max(start, index)].map(\.id))
            setSelection(toggling ? selectedIDs.union(range) : range, primary: id)
            selectionAnchor = anchor
        } else if toggling {
            var selection = selectedIDs
            if !selection.insert(id).inserted { selection.remove(id) }
            setSelection(selection, primary: id)
            selectionAnchor = id
        } else {
            setSelection([id], primary: id)
            selectionAnchor = id
        }
        selectionRequest = UUID()
    }

    func stepPhoto(by offset: Int, extending: Bool = false) {
        if previewPhoto != nil { movePreview(by: offset); return }
        guard let selectedID, let index = photos.firstIndex(where: { $0.id == selectedID }),
              photos.indices.contains(index + offset) else { return }
        selectPhoto(photos[index + offset].id, extending: extending)
    }

    func rateSelection(_ rating: Int) { annotate(ids: gradingPhotos.map(\.id), rating: rating) }

    var gradingPhotos: [Photo] { previewPhoto.map { [$0] } ?? selectedPhotos }

    func flagSelection(_ action: PhotoFlag) {
        let targets = gradingPhotos
        for flag in [PhotoFlag.unmarked, .pick, .reject] {
            let ids = targets.filter { photo in
                Self.nextFlag(current: photo.flag ?? .unmarked, action: action) == flag
            }.map(\.id)
            if !ids.isEmpty { annotate(ids: ids, flag: flag) }
        }
    }

    static func nextFlag(current: PhotoFlag, action: PhotoFlag) -> PhotoFlag {
        switch action {
        case .pick: current == .pick ? .unmarked : .pick
        case .reject: current == .pick ? .unmarked : .reject
        case .unmarked: .unmarked
        }
    }

    func annotate(ids: [Int], rating: Int? = nil, caption: String? = nil, flag: PhotoFlag? = nil) {
        guard service.isConnected, !ids.isEmpty else { return }
        let selected = photos.filter { ids.contains($0.id) }
        guard selected.count == Set(ids).count else {
            errorMessage = "The selected photos are no longer visible. Select them again before saving."
            return
        }
        guard annotationQueue.enqueue(photos: selected, rating: rating, caption: caption, flag: flag) else { return }
        invalidateStarredPage()
        photos = annotationQueue.overlay(photos)
        if let previewPhoto { self.previewPhoto = annotationQueue.overlay([previewPhoto]).first }
    }

    func retryAnnotations() { annotationQueue.retry() }

    func discardAnnotations() {
        if annotationQueue.discard() { refresh() }
    }

    private func invalidateStarredPage() {
        guard source == .starred, !showingSequences else { return }
        pageTask?.cancel()
        isPaging = false
        nextOffset = nil
        if reloadingStarredAfterSave {
            generation += 1
            loadTask?.cancel()
            isLoading = false
            reloadingStarredAfterSave = false
        }
    }

    private func annotationsSaved(_ updated: [Photo], drained: Bool, rating: Bool, caption: Bool, flag: Bool) {
        annotationRevision += 1
        if !photoReads.isEmpty {
            for photo in updated {
                guard let target = try? AnnotationTarget(photo: photo) else { continue }
                var fields = acknowledgedAnnotations[photo.id].flatMap { $0.target == target ? $0 : nil }
                    ?? AcknowledgedAnnotation(target: target)
                if rating { fields.rating = (annotationRevision, photo.rating) }
                if caption { fields.caption = (annotationRevision, photo.caption) }
                if flag { fields.flag = (annotationRevision, photo.flag) }
                acknowledgedAnnotations[photo.id] = fields
            }
        }
        let byID = Dictionary(uniqueKeysWithValues: updated.map { ($0.id, $0) })
        photos = annotationQueue.overlay(photos.map { photo in
            guard let replacement = byID[photo.id], replacement.remotePath == photo.remotePath,
                  replacement.provider == photo.provider, replacement.fingerprint == photo.fingerprint else { return photo }
            var result = photo
            if rating { result.rating = replacement.rating }
            if caption { result.caption = replacement.caption }
            if flag { result.flag = replacement.flag }
            return result
        })
        if let previewPhoto, let updated = photos.first(where: { $0.id == previewPhoto.id }) { self.previewPhoto = updated }
        invalidateStarredPage()
        if drained {
            if !showingSequences && (source == .starred || activeSequence?.smartFilters != nil || !searchFilters.isEmpty) {
                navigate(to: source, preservingSelection: true, retainingPhotos: true)
            }
            if sequences.contains(where: { $0.smartFilters != nil }) { Task { await refreshSequences() } }
            showNotice("Edits saved")
        }
    }

    func beginEditing() {
        let ids = selectedPhotos.map(\.id)
        guard service.isConnected, !ids.isEmpty, !editingBusy else { return }
        editingBusy = true
        Task {
            defer { editingBusy = false }
            do {
                try requireServiceConnection()
                let batch = try await client.beginEditing(ids: ids)
                editingDataID = service.identity?.dataId
                batches.insert(batch, at: 0)
                openingBatchID = batch.id
                showingEditing = true
            } catch { present(error) }
        }
    }

    var canTrashSelection: Bool {
        workspaceWritable && !selectedPhotos.isEmpty && selectedPhotos.count <= 500
            && !trashBusy && !trashBatches.contains(where: \.transferring)
            && !annotationQueue.hasPendingEdits && !batches.contains(where: \.transferring)
    }

    func requestTrashSelection() {
        guard canTrashSelection, let dataID = service.identity?.dataId else { return }
        trashSelection = PhotoTrashSelection(photos: selectedPhotos, dataID: dataID)
    }

    func moveSelectionToTrash(_ selected: PhotoTrashSelection? = nil) {
        guard let selection = selected ?? trashSelection else { return }
        trashSelection = nil
        guard workspaceWritable, selection.dataID == service.identity?.dataId, !trashBusy else { return }
        trashBusy = true
        trashError = nil
        Task {
            defer { trashBusy = false }
            do {
                _ = try await client.trash(selection)
                guard service.identity?.dataId == selection.dataID else { return }
                await refreshTrash()
                await refreshAfterPhotoMove(dataID: selection.dataID)
                showNotice("Moving \(selection.photos.count) photos to Trash")
            } catch {
                guard service.identity?.dataId == selection.dataID else { return }
                trashError = error.localizedDescription
                showingTrash = true
                await refreshTrash()
            }
        }
    }

    func refreshTrash() async {
        guard service.isConnected, let dataID = service.identity?.dataId else { return }
        trashGeneration += 1
        let revision = trashGeneration
        do {
            let updated = try await client.trashBatches()
            guard service.identity?.dataId == dataID, trashGeneration == revision else { return }
            let old = Dictionary(uniqueKeysWithValues: trashBatches.map { ($0.id, $0.status) })
            let changed = trashDataID == dataID && updated.contains { old[$0.id] != $0.status && !$0.transferring }
            trashDataID = dataID
            trashBatches = updated
            if changed { await refreshAfterPhotoMove(dataID: dataID) }
        } catch {
            if showingTrash, trashGeneration == revision { trashError = error.localizedDescription }
        }
    }

    private func refreshAfterPhotoMove(dataID: String) async {
        guard service.identity?.dataId == dataID else { return }
        do {
            let result = try await client.library()
            guard service.identity?.dataId == dataID else { return }
            summary = result
            await refreshSequences()
            if !showingSequences { navigate(to: source, preservingSelection: true, filters: searchFilters) }
        } catch { trashError = error.localizedDescription }
    }

    func restoreTrash(_ batch: PhotoTrashBatch, restore: Bool = true) {
        guard workspaceWritable, !trashBusy, let dataID = service.identity?.dataId, trashDataID == dataID else { return }
        trashBusy = true
        trashError = nil
        Task {
            defer { trashBusy = false }
            do {
                _ = try await client.trashAction(id: batch.id, restore: restore, dataID: dataID)
                guard service.identity?.dataId == dataID else { return }
                await refreshTrash()
            } catch {
                if service.identity?.dataId == dataID { trashError = error.localizedDescription }
            }
        }
    }

    func monitorTrash() async {
        while !Task.isCancelled {
            if service.isConnected { await refreshTrash() }
            do { try await Task.sleep(for: .seconds(trashBatches.contains(where: \.transferring) ? 1 : 5)) }
            catch { return }
        }
    }

    func monitorEditing() async {
        while !Task.isCancelled {
            if service.isConnected { await refreshEditing(clearError: false) }
            do { try await Task.sleep(for: .seconds(batches.contains(where: \.transferring) ? 1 : 5)) }
            catch { return }
        }
    }

    func refreshEditing(clearError: Bool = true) async {
        guard service.isConnected, !editingBusy, let dataID = service.identity?.dataId else { return }
        editingRevision += 1
        let revision = editingRevision
        do {
            let current = try await client.editingBatches()
            guard service.isConnected, service.identity?.dataId == dataID, editingRevision == revision else { return }
            batches = current
            editingDataID = dataID
            if clearError { editingErrorMessage = nil }
            if let id = openingBatchID, let batch = batches.first(where: { $0.id == id }) {
                if batch.status == "ready" {
                    openingBatchID = nil
                    openInPhotoLab(batch)
                } else if ["needs_attention", "interrupted"].contains(batch.status) { openingBatchID = nil }
            }
        } catch {
            guard editingRevision == revision else { return }
            if showingEditing { editingErrorMessage = error.localizedDescription }
        }
    }

    private func policyKey(_ batch: EditingBatch) -> String {
        [editingDataID ?? libraryDataID ?? service.identity?.dataId ?? "", batch.id, batch.uploadDestination ?? ""].joined(separator: "\u{0}")
    }

    func editingPolicy(for batch: EditingBatch) -> String { editingPolicies[policyKey(batch)] ?? "" }
    func setEditingPolicy(_ value: String, for batch: EditingBatch) { editingPolicies[policyKey(batch)] = value }

    func editAction(_ batch: EditingBatch, action: String, policy: String? = nil, exports: [ExportSelection]? = nil) {
        guard !editingBusy else { return }
        guard service.isConnected, let dataID = service.identity?.dataId else {
            editingErrorMessage = "Reconnect to the library, then check this editing batch."
            errorMessage = editingErrorMessage
            return
        }
        guard editingDataID == nil || editingDataID == dataID else {
            editingErrorMessage = "These editing batches belong to another library. Reconnect there before continuing."
            return
        }
        editingBusy = true
        editingErrorMessage = nil
        editingRevision += 1
        let revision = editingRevision
        Task {
            defer { editingBusy = false }
            do {
                let current = try await client.editingBatch(id: batch.id)
                try requireServiceConnection()
                guard service.identity?.dataId == dataID, editingRevision == revision else {
                    throw ServiceConnectionError("The library connection changed. Reconnect and check the batch status.")
                }
                guard current.id == batch.id else { throw LibraryError.invalidResponse }
                replaceEditingBatch(current, dataID: dataID)
                guard let expected = batch.updatedAt, current.updatedAt == expected,
                      current.uploadDestination == batch.uploadDestination else {
                    throw ServiceConnectionError("This batch changed. Its current status is shown; review it before choosing an action again.")
                }
                if action == "finish", !batch.usesFolderOutput, !["keep_exports", "clear_batch"].contains(policy ?? "") {
                    throw ServiceConnectionError("Choose which local files to keep before uploading.")
                }
                let updated = try await client.editingAction(id: batch.id, action: action, policy: policy,
                    expectedDataID: dataID, expectedUpdatedAt: expected, exports: exports)
                guard updated.id == batch.id else { throw LibraryError.invalidResponse }
                guard service.isConnected, service.identity?.dataId == dataID, editingRevision == revision else {
                    throw ServiceConnectionError("Check this batch after reconnecting to confirm its status.")
                }
                replaceEditingBatch(updated, dataID: dataID)
            } catch {
                editingErrorMessage = error.localizedDescription
            }
        }
    }

    private func replaceEditingBatch(_ batch: EditingBatch, dataID: String) {
        editingDataID = dataID
        if let index = batches.firstIndex(where: { $0.id == batch.id }) { batches[index] = batch }
        else { batches.append(batch) }
    }

    func openInPhotoLab(_ batch: EditingBatch) {
        guard let app = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.dxo.PhotoLab10") else {
            errorMessage = "DxO PhotoLab 10 is not installed. Your downloaded RAWs are available in the editing folder."
            return
        }
        let folder = URL(fileURLWithPath: batch.folder, isDirectory: true)
        let urls = batch.items.map { folder.appendingPathComponent($0.localPath) }
        guard urls.allSatisfy({ FileManager.default.fileExists(atPath: $0.path) }) else {
            errorMessage = "Some working RAWs are missing. Open the editing folder to check the batch."
            return
        }
        Task {
            // PhotoLab registers public.folder as an editable document. Opening
            // the folder selects the batch in its browser; individual RAW open
            // events can leave its previous source and filmstrip unchanged.
            do { _ = try await NSWorkspace.shared.open([folder], withApplicationAt: app, configuration: .init()) }
            catch { present(error) }
        }
    }

    func previewSelection() {
        guard service.isConnected, let photo = selectedPhoto else { return }
        if let group = photo.timelapse { openTimelapse(group.id); return }
        selectPhoto(photo.id)
        previewPhoto = photo
    }

    func togglePreview() {
        if previewPhoto != nil { previewPhoto = nil } else { previewSelection() }
    }

    func canMovePreview(by offset: Int) -> Bool {
        guard !isPreviewPaging, [-1, 1].contains(offset), let id = previewPhoto?.id,
              let index = photos.firstIndex(where: { $0.id == id }) else { return false }
        if photos.indices.contains(index + offset) { return true }
        guard offset == 1, index == photos.count - 1, nextOffset != nil,
              service.isConnected, !isLoading else { return false }
        switch source {
        case .library, .starred, .timelapse, .importBatch: return true
        default: return false
        }
    }

    func movePreview(by offset: Int) {
        guard canMovePreview(by: offset), let anchor = previewPhoto?.id,
              let index = photos.firstIndex(where: { $0.id == anchor }) else { return }
        previewPagingError = nil
        if photos.indices.contains(index + offset) {
            previewPhoto = photos[index + offset]
            if let id = previewPhoto?.id { selectPhoto(id) }
            return
        }
        guard let page = loadMore() else { return }
        let requestID = UUID()
        let requestGeneration = generation
        let dataID = service.identity?.dataId
        previewMoveID = requestID
        isPreviewPaging = true
        previewMoveTask = Task {
            defer {
                if previewMoveID == requestID {
                    isPreviewPaging = false
                    previewMoveTask = nil
                }
            }
            await page.value
            guard !Task.isCancelled, previewMoveID == requestID,
                  generation == requestGeneration, previewPhoto?.id == anchor,
                  service.isConnected, service.identity?.dataId == dataID else { return }
            guard !page.isCancelled else {
                previewPagingError = "The photo order changed. Close this preview and choose a photo again."
                return
            }
            if let pageErrorMessage {
                previewPagingError = pageErrorMessage
                return
            }
            // The gallery may have changed positions while loading; resolve the anchor again.
            guard let current = photos.firstIndex(where: { $0.id == anchor }),
                  photos.indices.contains(current + 1) else {
                previewPagingError = nextOffset == nil
                    ? "You have reached the end of these results."
                    : "The next page did not contain another photo. Retry to continue."
                return
            }
            previewPhoto = photos[current + 1]
            if let id = previewPhoto?.id { selectPhoto(id) }
        }
    }

    private func cancelPreviewNavigation() {
        previewMoveID = UUID()
        previewMoveTask?.cancel()
        previewMoveTask = nil
        isPreviewPaging = false
        previewPagingError = nil
    }

    func resizeThumbnails(by delta: Double) {
        minimumColumnWidth = min(420, max(190, minimumColumnWidth + delta))
        preferences.set(minimumColumnWidth, forKey: "minimumColumnWidth")
    }

    func addSelected(to sequence: PhotoSequence) {
        guard sequence.smartFilters == nil else { return }
        let selected = selectedPhotos
        guard workspaceWritable, let dataID = service.identity?.dataId, !selected.isEmpty, !isSaving else { return }
        savingSequence = true
        Task {
            defer { savingSequence = false }
            do {
                try requireServiceConnection()
                var remaining = selected[...]
                var added = 0
                while !remaining.isEmpty {
                    guard workspaceWritable, service.identity?.dataId == dataID else {
                        throw ServiceConnectionError("The library changed before all photos were added. Reconnect and retry.")
                    }
                    var batch = Array(remaining.prefix(100))
                    var expected = AnnotationExpectation(dataID: dataID, assets: try batch.map(AnnotationTarget.init(photo:)))
                    while try client.sequenceAdditionSize(photoIDs: batch.map(\.id), expected: expected) > 64 * 1024 {
                        guard batch.count > 1 else { throw ServiceConnectionError("A photo reference exceeds the request size limit.") }
                        batch = Array(batch.prefix(batch.count / 2))
                        expected = AnnotationExpectation(dataID: dataID, assets: try batch.map(AnnotationTarget.init(photo:)))
                    }
                    let result = try await client.add(photoIDs: batch.map(\.id), to: sequence.id, expected: expected)
                    added += result.added ?? 0
                    remaining = remaining.dropFirst(batch.count)
                }
                await refreshSequences()
                showNotice(added == 0 ? "Already in \(sequence.name)" : "Added to \(sequence.name)")
            } catch { present(error) }
        }
    }

    var pendingSequenceDrafts: [SequenceDraft] {
        sequenceDrafts.values.filter { !$0.completed }.sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
    }

    func sequenceDraft(editing: PhotoSequence? = nil, adding: Photo? = nil) -> SequenceDraft {
        sequenceDraft(editing: editing, addingPhotos: adding.map { [$0] } ?? [])
    }

    func sequenceDraft(editing: PhotoSequence? = nil, addingPhotos: [Photo], folderID: String? = nil) -> SequenceDraft {
        let identities = addingPhotos.map { "\($0.provider ?? ""):\($0.remotePath)" }
        let key = editing.map { "sequence:\($0.id)" }
            ?? ((identities.isEmpty ? "new" : "photos:" + identities.joined(separator: "\n")) + (folderID.map { "\nfolder:\($0)" } ?? ""))
        if let draft = sequenceDrafts[key] { return draft }
        // Retained source objects belong to the library that supplied the view,
        // even if a new connection is ready before its metadata has loaded.
        let dataID = editing != nil || !addingPhotos.isEmpty || folderID != nil ? libraryDataID : service.identity?.dataId ?? libraryDataID
        let draft = SequenceDraft(key: key, endpoint: client.baseURL,
            dataID: dataID, editing: editing, adding: addingPhotos, folderID: folderID)
        sequenceDrafts[key] = draft
        return draft
    }

    func openSequenceEditor(editing: PhotoSequence? = nil, adding: Photo? = nil) {
        sequenceEditor = sequenceDraft(editing: editing, addingPhotos: adding.map { [$0] } ?? [],
            folderID: showingSequences ? selectedSequenceFolderID : nil)
    }

    func newSequence(in folderID: String?) {
        sequenceEditor = sequenceDraft(addingPhotos: [], folderID: folderID)
    }

    func retainSmartSequenceDraft(_ draft: SequenceDraft) {
        sequenceDrafts[draft.key] = draft
        sequenceEditor = draft
    }

    func openSequenceEditorForSelection() {
        sequenceEditor = sequenceDraft(addingPhotos: selectedPhotos)
    }

    func discardSequenceDraft(_ draft: SequenceDraft) {
        guard !draft.isSaving else { return }
        if sequenceDrafts[draft.key]?.id == draft.id { sequenceDrafts.removeValue(forKey: draft.key) }
        if sequenceEditor?.id == draft.id { sequenceEditor = nil }
    }

    func forgetDeletedSequence(_ id: String, dataID: String) {
        for draft in Array(sequenceDrafts.values) where draft.dataID == dataID && draft.confirmed?.id == id {
            discardSequenceDraft(draft)
        }
        if movingSequenceItem?.item.id == "sequence:\(id)" { movingSequenceItem = nil }
        if activeSequence?.id == id { activeSequence = nil }
    }

    func reconnectSheets() async {
        await start()
        if service.isConnected, showingEditing { await refreshEditing() }
    }

    // Retained for non-sheet callers; the same draft makes partial-save retries safe.
    func saveSequence(name: String, note: String, editing: PhotoSequence?, adding photo: Photo?) async -> Bool {
        let draft = sequenceDraft(editing: editing, adding: photo)
        guard !draft.isSaving else { return false }
        draft.name = name
        draft.note = note
        let saved = await saveSequence(draft)
        if !saved, let message = draft.errorMessage { errorMessage = message }
        return saved
    }

    func saveSequence(_ draft: SequenceDraft) async -> Bool {
        guard !draft.isSaving, !savingSequence, !organizationBusy else { return false }
        if draft.completed { return true }
        guard service.isConnected else {
            draft.errorMessage = "Reconnect to the library, then save this draft."
            return false
        }
        let name = draft.name.trimmingCharacters(in: .whitespacesAndNewlines)
        let note = draft.note
        guard !name.isEmpty, name.unicodeScalars.count <= 120, note.unicodeScalars.count <= 10_000 else {
            draft.errorMessage = "Use a name of 1–120 characters and a note of at most 10,000 characters."
            return false
        }
        draft.isSaving = true
        savingSequence = true
        draft.errorMessage = nil
        defer { draft.isSaving = false; savingSequence = false }
        do {
            let dataID = try requireDraftLibrary(draft)
            if draft.editing == nil, draft.creationFields == nil {
                draft.creationFields = (name, note)
                draft.baselineName = name
                draft.baselineNote = note
            }
            draft.needsNameWrite = draft.needsNameWrite || name != draft.baselineName
            draft.needsNoteWrite = draft.needsNoteWrite || note != draft.baselineNote
            if draft.confirmed == nil {
                // Keep the identifier stable, but allow corrections after rejection.
                // The server leaves an existing creation untouched on replay.
                let created = try await client.createSequence(name: name, note: note,
                    sequenceID: draft.creationID, expectedDataID: dataID, folderID: draft.folderID, smartFilters: draft.smartFilters)
                guard created.id == draft.creationID else { throw LibraryError.invalidResponse }
                draft.confirmed = created
            }
            if draft.editing == nil, draft.confirmed?.folderId != draft.folderID {
                _ = try requireDraftLibrary(draft)
                draft.confirmed = try await client.moveSequence(id: draft.confirmed!.id,
                    to: draft.folderID, expectedDataID: dataID)
            }
            if draft.needsNameWrite || draft.needsNoteWrite || draft.smartFilters != draft.confirmed?.smartFilters {
                _ = try requireDraftLibrary(draft)
                let updated = try await client.updateSequence(id: draft.confirmed!.id,
                    name: draft.needsNameWrite ? name : nil, note: draft.needsNoteWrite ? note : nil,
                    expectedDataID: dataID, smartFilters: draft.smartFilters)
                guard updated.id == draft.confirmed!.id else { throw LibraryError.invalidResponse }
                draft.confirmed = updated
                if draft.needsNameWrite { draft.baselineName = name }
                if draft.needsNoteWrite { draft.baselineNote = note }
                draft.needsNameWrite = false
                draft.needsNoteWrite = false
            }
            // Keep the original ordered selection across dismissal and partial-save retries.
            // Replaying an accepted chunk is idempotent in the workspace store.
            var remaining = draft.adding[...]
            while !remaining.isEmpty {
                _ = try requireDraftLibrary(draft)
                var batch = Array(remaining.prefix(100))
                var expected = AnnotationExpectation(dataID: dataID, assets: try batch.map(AnnotationTarget.init(photo:)))
                while try client.sequenceAdditionSize(photoIDs: batch.map(\.id), expected: expected) > 64 * 1024 {
                    guard batch.count > 1 else { throw ServiceConnectionError("A photo reference exceeds the request size limit.") }
                    batch = Array(batch.prefix(batch.count / 2))
                    expected = AnnotationExpectation(dataID: dataID, assets: try batch.map(AnnotationTarget.init(photo:)))
                }
                let result = try await client.add(photoIDs: batch.map(\.id), to: draft.confirmed!.id, expected: expected).sequence
                guard result.id == draft.confirmed!.id else { throw LibraryError.invalidResponse }
                draft.confirmed = result
                remaining = remaining.dropFirst(batch.count)
            }
            let result = draft.confirmed!
            draft.completed = true
            if sequenceDrafts[draft.key]?.id == draft.id { sequenceDrafts.removeValue(forKey: draft.key) }
            if sequenceEditor?.id == draft.id { sequenceEditor = nil }
            if case .sequence(let id) = source, id == result.id {
                activeSequence = result
                if result.smartFilters != nil { navigate(to: source, preservingSelection: true) }
            }
            await refreshSequences()
            showNotice(draft.editing == nil ? "Created \(result.name)" : "Saved \(result.name)")
            return true
        } catch {
            draft.errorMessage = "Could not save this sequence. \(error.localizedDescription) The draft is kept while Latent is open."
            return false
        }
    }

    private func requireDraftLibrary(_ draft: SequenceDraft) throws -> String {
        try requireServiceConnection()
        guard let current = service.identity?.dataId, draft.endpoint == client.baseURL else {
            throw ServiceConnectionError("Reconnect to the library that owns this draft.")
        }
        if let expected = draft.dataID, expected != current {
            throw ServiceConnectionError("This draft belongs to another library. Reconnect there or discard this draft.")
        }
        if draft.dataID == nil, draft.editing != nil || !draft.adding.isEmpty {
            throw ServiceConnectionError("Reconnect and reopen the source sequence or photo before saving.")
        }
        draft.dataID = current
        return current
    }

    func moveSelected(by delta: Int) {
        guard service.isConnected, let sequence = activeSequence, let items = sequence.items,
              let index = items.firstIndex(where: { $0.asset?.id == selectedID }),
              items.indices.contains(index + delta), !isSaving else { return }
        var ids = items.map(\.id)
        ids.swapAt(index, index + delta)
        savingSequence = true
        Task {
            let read = beginPhotoRead()
            defer { finishPhotoRead(read) }
            defer { savingSequence = false }
            do {
                try requireServiceConnection()
                let updated = try await client.reorder(sequenceID: sequence.id, itemIDs: ids)
                if case .sequence(let id) = source, id == updated.id {
                    activeSequence = updated
                    photos = reconcilePhotoRead(updated.items?.compactMap(\.asset) ?? [], read: read)
                }
            } catch { present(error) }
        }
    }

    func canMoveSelected(by delta: Int) -> Bool {
        guard activeSequence?.smartFilters == nil else { return false }
        guard service.isConnected, let items = activeSequence?.items,
              let index = items.firstIndex(where: { $0.asset?.id == selectedID }) else { return false }
        return !isSaving && items.indices.contains(index + delta)
    }

    func removeSelectedFromSequence() {
        guard activeSequence?.smartFilters == nil else { return }
        guard service.isConnected, let sequence = activeSequence,
              let item = sequence.items?.first(where: { $0.asset?.id == selectedID }), !isSaving else { return }
        savingSequence = true
        Task {
            let read = beginPhotoRead()
            defer { finishPhotoRead(read) }
            defer { savingSequence = false }
            do {
                try requireServiceConnection()
                let updated = try await client.remove(itemID: item.id, from: sequence.id)
                if case .sequence(let id) = source, id == updated.id {
                    activeSequence = updated
                    photos = reconcilePhotoRead(updated.items?.compactMap(\.asset) ?? [], read: read)
                    total = updated.itemCount
                    selectedID = photos.first?.id
                }
                await refreshSequences()
            } catch { present(error) }
        }
    }

    private func beginPhotoRead() -> UUID {
        let token = UUID()
        photoReads[token] = annotationRevision
        return token
    }

    private func finishPhotoRead(_ token: UUID) {
        photoReads.removeValue(forKey: token)
        let oldest = photoReads.values.min() ?? annotationRevision
        for (id, var fields) in acknowledgedAnnotations {
            if let rating = fields.rating, rating.revision <= oldest { fields.rating = nil }
            if let caption = fields.caption, caption.revision <= oldest { fields.caption = nil }
            if let flag = fields.flag, flag.revision <= oldest { fields.flag = nil }
            acknowledgedAnnotations[id] = fields.rating == nil && fields.caption == nil && fields.flag == nil ? nil : fields
        }
    }

    private func reconcilePhotoRead(_ photos: [Photo], read: UUID) -> [Photo] {
        let startedAt = photoReads[read] ?? annotationRevision
        return annotationQueue.overlay(photos.map { photo in
            guard let saved = acknowledgedAnnotations[photo.id], saved.target.matches(photo) else { return photo }
            var result = photo
            if let rating = saved.rating, rating.revision > startedAt { result.rating = rating.value }
            if let caption = saved.caption, caption.revision > startedAt { result.caption = caption.value }
            if let flag = saved.flag, flag.revision > startedAt { result.flag = flag.value }
            return result
        })
    }

    func refreshSequences() async {
        guard service.isConnected else { return }
        let dataID = service.identity?.dataId
        sequenceRevision += 1
        let requestRevision = sequenceRevision
        do {
            let result = try await client.sequenceLibrary()
            guard sequenceRevision == requestRevision, service.isConnected, service.identity?.dataId == dataID else { return }
            applySequenceLibrary(result, dataID: dataID)
        } catch {
            guard sequenceRevision == requestRevision, service.isConnected else { return }
            present(error)
        }
    }

    func showNotice(_ text: String) {
        noticeTask?.cancel()
        notice = text
        noticeTask = Task {
            try? await Task.sleep(for: .seconds(4))
            if !Task.isCancelled { notice = nil }
        }
    }

    private func requireServiceConnection() throws {
        guard service.isConnected else {
            throw ServiceConnectionError("Library disconnected. Retry the connection before continuing.")
        }
    }

    private func present(_ error: Error) {
        if error is CancellationError { return }
        if let urlError = error as? URLError, [.cannotConnectToHost, .networkConnectionLost].contains(urlError.code) {
            errorMessage = "The local library is unavailable. Start the library service, then retry."
        } else {
            errorMessage = error.localizedDescription
        }
    }
}

extension ProcessInfo {
    func argument(after flag: String) -> String? {
        guard let index = arguments.firstIndex(of: flag), arguments.indices.contains(index + 1) else { return nil }
        return arguments[index + 1]
    }
}

@MainActor enum PhotoFormatting {
    private static let dayParser: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter
    }()
    private static let shortFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "MMM d, yyyy"
        return formatter
    }()
    private static let fullFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateStyle = .long
        return formatter
    }()
    private static let monthFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "LLLL"
        return formatter
    }()
    private static let dayOfMonthFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "d"
        return formatter
    }()

    static func month(_ value: String) -> String {
        guard let date = dayParser.date(from: "\(value)-01") else { return value }
        return monthFormatter.string(from: date)
    }

    static func dayOfMonth(_ value: String) -> String {
        guard let date = dayParser.date(from: value) else { return value }
        return dayOfMonthFormatter.string(from: date)
    }

    static func day(_ value: String?) -> String {
        guard let value, let date = dayParser.date(from: value) else { return "Date unavailable" }
        return shortFormatter.string(from: date)
    }

    static func fullDay(_ value: String) -> String {
        guard let date = dayParser.date(from: value) else { return value }
        return fullFormatter.string(from: date)
    }

    static func archivePeriod(_ value: String) -> String {
        switch value.count {
        case 4: return value
        case 7: return "\(month(value)) \(value.prefix(4))"
        default: return fullDay(value)
        }
    }

    static func capture(_ photo: Photo) -> String {
        let day = day(photo.captureDay)
        guard let time = photo.captureAt?.split(separator: " ").last,
              time.contains(":") else { return day }
        return "\(day) · \(time.prefix(5))"
    }

    static func bytes(_ value: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: value, countStyle: .file)
    }
}
