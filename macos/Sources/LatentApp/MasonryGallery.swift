import AppKit
import LatentCore
import SwiftUI

enum GalleryAppearance {
    static let canvas = NSColor(name: "LatentCanvas") { appearance in
        appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            ? NSColor(srgbRed: 0.055, green: 0.062, blue: 0.067, alpha: 1)
            : NSColor(srgbRed: 0.975, green: 0.973, blue: 0.965, alpha: 1)
    }
    static let accent = NSColor.controlAccentColor
    static let captionHeight: CGFloat = 44
}

struct MasonryGallery: NSViewRepresentable {
    @Bindable var model: LibraryModel

    func makeCoordinator() -> Coordinator { Coordinator(model: model) }

    func makeNSView(context: Context) -> NSScrollView {
        let scrollView = NSScrollView()
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = false
        scrollView.autohidesScrollers = true
        scrollView.borderType = .noBorder
        scrollView.drawsBackground = true
        scrollView.backgroundColor = GalleryAppearance.canvas

        let collection = PhotoCollectionView()
        collection.backgroundColors = [GalleryAppearance.canvas]
        collection.isSelectable = true
        collection.allowsMultipleSelection = true
        collection.collectionViewLayout = context.coordinator.layout
        collection.dataSource = context.coordinator
        collection.delegate = context.coordinator
        collection.prefetchDataSource = context.coordinator
        collection.register(PhotoItem.self, forItemWithIdentifier: PhotoItem.identifier)
        collection.autoresizingMask = [.width]
        collection.setAccessibilityIdentifier("photo-gallery")
        collection.setAccessibilityLabel("Photos")
        collection.selectPhoto = { [weak coordinator = context.coordinator] path, modifiers, clicks in
            coordinator?.selectPhoto(at: path, modifiers: modifiers, clicks: clicks)
        }
        scrollView.documentView = collection
        context.coordinator.collection = collection
        context.coordinator.update(model)
        return scrollView
    }

    func updateNSView(_ view: NSScrollView, context: Context) { context.coordinator.update(model) }

    static func dismantleNSView(_ view: NSScrollView, coordinator: Coordinator) {
        coordinator.cancelPrefetch()
        coordinator.collection?.visibleItems().forEach { ($0 as? PhotoItem)?.cancelLoading() }
        coordinator.collection?.delegate = nil
        coordinator.collection?.dataSource = nil
    }

    @MainActor final class Coordinator: NSObject, NSCollectionViewDataSource, NSCollectionViewDelegate,
                                         NSCollectionViewPrefetching {
        var model: LibraryModel
        let layout = PhotoLayout()
        weak var collection: PhotoCollectionView?
        private var photos: [Photo] = []
        private var datasetID: UUID?
        private var selectionRequest: UUID?
        private var prefetch: [IndexPath: Task<Void, Never>] = [:]

        init(model: LibraryModel) { self.model = model }

        func update(_ model: LibraryModel) {
            self.model = model
            guard let collection else { return }
            let isNewDataset = datasetID != model.datasetID
            let changed = photos != model.photos
            let changedSize = layout.minimumColumnWidth != model.minimumColumnWidth
            let annotationsOnly = changed && !isNewDataset && photos.count == model.photos.count
                && zip(photos, model.photos).allSatisfy { $0.id == $1.id && $0.aspectRatio == $1.aspectRatio && $0.previewUrl == $1.previewUrl }
            if annotationsOnly {
                photos = model.photos
                for item in collection.visibleItems() {
                    guard let path = collection.indexPath(for: item), photos.indices.contains(path.item) else { continue }
                    (item as? PhotoItem)?.updateAnnotation(photos[path.item])
                }
            } else if changed || isNewDataset {
                cancelPrefetch()
                let previousCount = photos.count
                let isAppend = !isNewDataset && model.photos.count > previousCount
                    && model.photos.prefix(previousCount).elementsEqual(photos)
                photos = model.photos
                datasetID = model.datasetID
                layout.aspectRatios = photos.map(\.aspectRatio)
                layout.minimumColumnWidth = model.minimumColumnWidth
                layout.invalidateGeometry()
                if isAppend {
                    collection.insertItems(at: Set((previousCount..<photos.count).map { IndexPath(item: $0, section: 0) }))
                } else {
                    collection.reloadData()
                }
                if isNewDataset {
                    collection.enclosingScrollView?.contentView.scroll(to: .zero)
                    collection.enclosingScrollView?.reflectScrolledClipView(collection.enclosingScrollView!.contentView)
                }
            } else if changedSize {
                layout.minimumColumnWidth = model.minimumColumnWidth
                layout.invalidateGeometry()
            }
            let paths = Set(photos.enumerated().compactMap { index, photo in
                model.selectedIDs.contains(photo.id) ? IndexPath(item: index, section: 0) : nil
            })
            if collection.selectionIndexPaths != paths { collection.selectionIndexPaths = paths }
            if selectionRequest != model.selectionRequest {
                selectionRequest = model.selectionRequest
                if let id = model.selectedID, let index = photos.firstIndex(where: { $0.id == id }) {
                    collection.scrollToItems(at: [IndexPath(item: index, section: 0)], scrollPosition: .nearestVerticalEdge)
                }
            }

        }

        func collectionView(_ collectionView: NSCollectionView, numberOfItemsInSection section: Int) -> Int {
            photos.count
        }

        func collectionView(_ collectionView: NSCollectionView, itemForRepresentedObjectAt indexPath: IndexPath) -> NSCollectionViewItem {
            let item = collectionView.makeItem(withIdentifier: PhotoItem.identifier, for: indexPath) as! PhotoItem
            configure(item, at: indexPath, in: collectionView)
            return item
        }

        private func configure(_ item: PhotoItem, at indexPath: IndexPath, in collectionView: NSCollectionView) {
            let photo = photos[indexPath.item]
            item.isSelected = model.selectedIDs.contains(photo.id)
            item.configure(photo: photo, client: model.client) { [weak self, weak collectionView] in
                guard let self, let index = self.photos.firstIndex(where: { $0.id == photo.id }) else { return }
                collectionView?.selectionIndexPaths = [IndexPath(item: index, section: 0)]
                self.model.selectPhoto(photo.id)
                collectionView?.window?.makeFirstResponder(collectionView)
            }
        }

        func collectionView(_ collectionView: NSCollectionView, didSelectItemsAt indexPaths: Set<IndexPath>) {
            updateSelection(collectionView, primary: indexPaths.sorted().last?.item)
        }

        func collectionView(_ collectionView: NSCollectionView, didDeselectItemsAt indexPaths: Set<IndexPath>) {
            updateSelection(collectionView, primary: nil)
        }

        private func updateSelection(_ collection: NSCollectionView, primary: Int?) {
            let ids = Set(collection.selectionIndexPaths.compactMap { path in
                photos.indices.contains(path.item) ? photos[path.item].id : nil
            })
            model.setSelection(ids, primary: primary.flatMap { photos.indices.contains($0) ? photos[$0].id : nil })
        }

        func collectionView(_ collectionView: NSCollectionView, willDisplay item: NSCollectionViewItem,
                            forRepresentedObjectAt indexPath: IndexPath) {
            guard let item = item as? PhotoItem, photos.indices.contains(indexPath.item) else { return }
            if item.isPresentationRetired {
                // AppKit can redisplay a retained item without dequeuing it again.
                configure(item, at: indexPath, in: collectionView)
            } else {
                item.isSelected = model.selectedIDs.contains(photos[indexPath.item].id)
                item.updateAnnotation(photos[indexPath.item])
                item.startLoadingIfNeeded()
            }
            if indexPath.item >= photos.count - 35 {
                Task { model.loadMore() }
            }
        }

        func collectionView(_ collectionView: NSCollectionView, didEndDisplaying item: NSCollectionViewItem,
                            forRepresentedObjectAt indexPath: IndexPath) {
            (item as? PhotoItem)?.retirePresentation()
        }

        func collectionView(_ collectionView: NSCollectionView, prefetchItemsAt indexPaths: [IndexPath]) {
            // A small lookahead is enough; never turn a fast scroll into a library-wide fetch.
            for indexPath in indexPaths.prefix(24) where prefetch[indexPath] == nil {
                guard photos.indices.contains(indexPath.item),
                      let url = try? model.client.mediaURL(photos[indexPath.item].contactUrl) else { continue }
                prefetch[indexPath] = Task { [weak self] in
                    _ = try? await ImagePipeline.shared.image(at: url, maximumPixelSize: 512)
                    if !Task.isCancelled { self?.prefetch.removeValue(forKey: indexPath) }
                }
            }
        }

        func collectionView(_ collectionView: NSCollectionView, cancelPrefetchingForItemsAt indexPaths: [IndexPath]) {
            for indexPath in indexPaths { prefetch.removeValue(forKey: indexPath)?.cancel() }
        }

        func cancelPrefetch() {
            prefetch.values.forEach { $0.cancel() }
            prefetch.removeAll()
        }

        func selectPhoto(at path: IndexPath, modifiers: NSEvent.ModifierFlags, clicks: Int) {
            guard photos.indices.contains(path.item) else { return }
            model.selectPhoto(photos[path.item].id, extending: modifiers.contains(.shift),
                              toggling: modifiers.contains(.command))
            update(model)
            if clicks == 2 { model.previewSelection() }
        }
    }
}

@MainActor final class PhotoLayout: NSCollectionViewLayout {
    var aspectRatios: [Double] = []
    var minimumColumnWidth: CGFloat = 224
    private(set) var geometry: MasonryGeometry?
    private var needsGeometry = true
    private var lastWidth: CGFloat = 0
    private(set) var rebuildCount = 0

    func invalidateGeometry() {
        geometry = nil
        needsGeometry = true
        invalidateLayout()
    }

    override func prepare() {
        super.prepare()
        guard let collectionView else { return }
        let width = collectionView.enclosingScrollView?.contentSize.width ?? collectionView.bounds.width
        guard width > 0, needsGeometry || abs(width - lastWidth) > 0.5 else { return }
        geometry = MasonryGeometry(aspectRatios: aspectRatios, width: width,
                                   minimumColumnWidth: minimumColumnWidth,
                                   captionHeight: GalleryAppearance.captionHeight)
        lastWidth = width
        needsGeometry = false
        rebuildCount += 1
    }

    override var collectionViewContentSize: NSSize { geometry?.contentSize ?? .zero }

    override func layoutAttributesForElements(in rect: NSRect) -> [NSCollectionViewLayoutAttributes] {
        guard let geometry else { return [] }
        return geometry.indexes(intersecting: rect).compactMap {
            layoutAttributesForItem(at: IndexPath(item: $0, section: 0))
        }
    }

    override func layoutAttributesForItem(at indexPath: IndexPath) -> NSCollectionViewLayoutAttributes? {
        guard indexPath.section == 0, let geometry,
              geometry.placements.indices.contains(indexPath.item) else { return nil }
        // AppKit may ask for geometry between invalidation and accepting a reload.
        // A smart sequence can change from one item to zero during that interval.
        if let collectionView, collectionView.dataSource != nil {
            guard collectionView.numberOfSections > 0,
                  indexPath.item < collectionView.numberOfItems(inSection: 0) else { return nil }
        }
        let attributes = NSCollectionViewLayoutAttributes(forItemWith: indexPath)
        attributes.frame = geometry.placements[indexPath.item].frame
        return attributes
    }

    override func shouldInvalidateLayout(forBoundsChange newBounds: NSRect) -> Bool {
        abs(newBounds.width - lastWidth) > 0.5
    }
}

@MainActor final class PhotoCollectionView: NSCollectionView {
    var selectPhoto: ((IndexPath, NSEvent.ModifierFlags, Int) -> Void)?
    override func mouseDown(with event: NSEvent) {
        guard let path = indexPathForItem(at: convert(event.locationInWindow, from: nil)) else {
            super.mouseDown(with: event)
            return
        }
        window?.makeFirstResponder(self)
        selectPhoto?(path, event.modifierFlags, event.clickCount)
    }
}

@MainActor final class PhotoItem: NSCollectionViewItem {
    static let identifier = NSUserInterfaceItemIdentifier("LatentPhoto")
    private var photo: Photo?
    private var client: LibraryClient?
    private var imageTask: Task<Void, Never>?
    private var loadedPreview = false
    private(set) var isPresentationRetired = true
    private var cell: PhotoCell { view as! PhotoCell }

    override func loadView() { view = PhotoCell() }

    isolated deinit {
        // A configured item can be released without another end-display callback
        // while its view is still attached to the collection.
        cancelLoading()
        guard isViewLoaded else { return }
        retirePresentation()
        view.removeFromSuperview()
    }

    override var isSelected: Bool {
        didSet { if isViewLoaded && !isPresentationRetired { cell.selected = isSelected } }
    }

    override func prepareForReuse() {
        super.prepareForReuse()
        retirePresentation()
        photo = nil
        client = nil
        isSelected = false
    }

    func retirePresentation() {
        // End-display is earlier than prepareForReuse. Clear the cached drawing
        // while AppKit retains the item, even if it never dequeues that item again.
        isPresentationRetired = true
        cancelLoading()
        cell.isHidden = true
        cell.selected = false
        cell.name.stringValue = ""
        cell.date.stringValue = ""
        cell.onSelect = nil
        cell.setAccessibilityLabel(nil)
        cell.setAccessibilityIdentifier("")
        cell.picture.image = nil
        cell.picture.layer?.backgroundColor = nil
        cell.failure.isHidden = true
        cell.flag.isHidden = true
        cell.flag.image = nil
        cell.flag.layer?.backgroundColor = nil
        loadedPreview = false
    }

    func configure(photo: Photo, client: LibraryClient, onSelect: @escaping () -> Void) {
        cancelLoading()
        isPresentationRetired = false
        self.photo = photo
        self.client = client
        loadedPreview = false
        cell.picture.image = nil
        cell.picture.layer?.backgroundColor = NSColor(white: 0.075, alpha: 1).cgColor
        cell.name.stringValue = photo.name
        updateAnnotation(photo)
        cell.selected = isSelected
        cell.failure.isHidden = true
        cell.onSelect = onSelect
        cell.setAccessibilityIdentifier("photo-\(photo.id)")
        cell.isHidden = false
        startLoadingIfNeeded()
    }

    func updateAnnotation(_ photo: Photo) {
        self.photo = photo
        guard !isPresentationRetired else { return }
        cell.date.stringValue = String(repeating: "★", count: photo.rating ?? 0)
            + ((photo.rating ?? 0) > 0 ? "  ·  " : "") + PhotoFormatting.day(photo.captureDay)
            + (photo.timelapse.map { " · \($0.photoCount) frames" } ?? "")
        let flag = photo.flag ?? .unmarked
        cell.flag.isHidden = flag == .unmarked
        cell.flag.image = flag == .unmarked ? nil : NSImage(systemSymbolName: flag == .pick ? "flag.fill" : "xmark", accessibilityDescription: flag.rawValue)
        cell.flag.layer?.backgroundColor = flag == .unmarked ? nil : NSColor.black.withAlphaComponent(0.7).cgColor
        cell.flag.contentTintColor = flag == .pick ? .systemGreen : .systemRed
        cell.setAccessibilityLabel("\(photo.name), \(PhotoFormatting.day(photo.captureDay)), \(flag.rawValue), \(photo.rating ?? 0) stars"
            + (photo.timelapse.map { ", time-lapse group, \($0.photoCount) frames" } ?? ""))

    }

    func startLoadingIfNeeded() {
        guard !isPresentationRetired, imageTask == nil, !loadedPreview, let photo, let client,
              let contact = try? client.mediaURL(photo.contactUrl) else { return }
        let preview = photo.previewAvailable ? try? client.mediaURL(photo.previewUrl) : nil
        let logicalWidth = max(250, view.bounds.width)
        let scale = view.window?.backingScaleFactor ?? 2
        let pixels = Int(max(logicalWidth, logicalWidth / photo.aspectRatio) * scale)
        imageTask = Task { [weak self] in
            do {
                let small = try await ImagePipeline.shared.image(at: contact, maximumPixelSize: 512)
                try Task.checkCancellation()
                guard self?.photo?.id == photo.id else { return }
                self?.show(small)
                if let preview, pixels > 512 {
                    // A missing large cache entry must not replace a usable contact image with an error.
                    // Keep the owner weak across the await so release can cancel this consumer.
                    if let large = try? await ImagePipeline.shared.image(at: preview, maximumPixelSize: min(1024, pixels)),
                       !Task.isCancelled, self?.photo?.id == photo.id { self?.show(large) }
                }
                guard !Task.isCancelled, let self, self.photo?.id == photo.id else { return }
                self.loadedPreview = true
                self.imageTask = nil
            } catch {
                guard !Task.isCancelled, let self, self.photo?.id == photo.id else { return }
                self.cell.failure.isHidden = false
                self.imageTask = nil
            }
        }
    }

    func cancelLoading() {
        imageTask?.cancel()
        imageTask = nil
    }

    private func show(_ value: DecodedImage) {
        cell.picture.image = NSImage(cgImage: value.image,
                                     size: NSSize(width: value.image.width, height: value.image.height))
        cell.failure.isHidden = true
    }
}

@MainActor private final class PhotoCell: NSView {
    let picture = NSImageView()
    let name = NSTextField(labelWithString: "")
    let date = NSTextField(labelWithString: "")
    let failure = NSTextField(labelWithString: "Preview unavailable")
    let flag = NSImageView()
    var onSelect: (() -> Void)?
    var selected = false { didSet { updateSelection() } }
    override var isFlipped: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        picture.wantsLayer = true
        picture.imageScaling = .scaleProportionallyUpOrDown
        picture.imageAlignment = .alignCenter
        picture.layer?.backgroundColor = NSColor(white: 0.075, alpha: 1).cgColor
        name.font = .systemFont(ofSize: 12, weight: .medium)
        name.textColor = .labelColor
        name.lineBreakMode = .byTruncatingMiddle
        date.font = .systemFont(ofSize: 12)
        date.textColor = .secondaryLabelColor
        failure.font = .systemFont(ofSize: 12)
        failure.textColor = .secondaryLabelColor
        failure.alignment = .center
        failure.isHidden = true
        flag.isHidden = true
        flag.wantsLayer = true
        flag.layer?.cornerRadius = 6
        [picture, name, date, failure, flag].forEach(addSubview)
        setAccessibilityElement(true)
        setAccessibilityRole(.cell)
    }

    required init?(coder: NSCoder) { nil }

    override func accessibilityPerformPress() -> Bool {
        onSelect?()
        return onSelect != nil
    }

    override func layout() {
        super.layout()
        let imageHeight = max(0, bounds.height - GalleryAppearance.captionHeight)
        picture.frame = NSRect(x: 0, y: 0, width: bounds.width, height: imageHeight)
        flag.frame = NSRect(x: 8, y: 8, width: 26, height: 26)
        name.frame = NSRect(x: 0, y: imageHeight + 8, width: bounds.width, height: 16)
        date.frame = NSRect(x: 0, y: imageHeight + 25, width: bounds.width, height: 16)
        failure.frame = NSRect(x: 4, y: imageHeight / 2 - 9, width: max(0, bounds.width - 8), height: 18)
    }

    private func updateSelection() {
        picture.layer?.borderWidth = selected ? 2 : 0
        picture.layer?.borderColor = GalleryAppearance.accent.cgColor
        setAccessibilitySelected(selected)
    }
}
