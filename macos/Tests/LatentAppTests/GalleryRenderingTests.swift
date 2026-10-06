import AppKit
import LatentCore
import Testing
@testable import LatentApp

@MainActor private final class ReloadCountSource: NSObject, NSCollectionViewDataSource {
    var count = 0
    func collectionView(_ collectionView: NSCollectionView, numberOfItemsInSection section: Int) -> Int { count }
    func collectionView(_ collectionView: NSCollectionView, itemForRepresentedObjectAt indexPath: IndexPath) -> NSCollectionViewItem {
        NSCollectionViewItem()
    }
}

@MainActor @Suite(.serialized)
struct GalleryRenderingTests {
    @Test func pendingGeometryCannotPublishItemsBeforeCollectionAcceptsReload() {
        let source = ReloadCountSource()
        let collection = PhotoCollectionView(frame: NSRect(x: 0, y: 0, width: 848, height: 500))
        let layout = PhotoLayout()
        collection.dataSource = source
        collection.collectionViewLayout = layout
        collection.reloadData()
        layout.aspectRatios = [1.5]
        layout.prepare()
        #expect(collection.numberOfItems(inSection: 0) == 0)
        #expect(layout.geometry?.placements.count == 1)
        #expect(layout.layoutAttributesForItem(at: IndexPath(item: 0, section: 0)) == nil)
        source.count = 1
        collection.reloadData()
        layout.prepare()
        #expect(collection.numberOfItems(inSection: 0) == 1)
        #expect(layout.layoutAttributesForItem(at: IndexPath(item: 0, section: 0)) != nil)
        collection.dataSource = nil
    }

    private func photo(_ id: Int) throws -> Photo {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(Photo.self, from: Data("""
        {"id":\(id),"name":"SYNTHETIC_\(id).ARW","remote_path":"/synthetic/\(id).ARW",
         "provider":"synthetic","fingerprint":"fixture-\(id)","size_bytes":1024,
         "capture_at":"2026:08:29 12:00:00","preview_width":320,"preview_height":480,
         "contact_url":"https://invalid.example/never-requested.jpg",
         "preview_url":"https://invalid.example/never-requested.jpg","preview_available":false,
         "rating":5,"caption":""}
        """.utf8))
    }

    @Test func reusedPhotoCannotRetainItsOldDrawingOrSelectionAction() throws {
        // NSView-only lifecycle: no NSWindow, NSApplication, or media request.
        let item = PhotoItem()
        let client = try LibraryClient(baseURL: URL(string: "http://127.0.0.1:1")!)
        var oldPresses = 0
        item.configure(photo: try photo(3), client: client) { oldPresses += 1 }
        item.isSelected = true
        let view = item.view
        let picture = try #require(view.subviews.compactMap { $0 as? NSImageView }.first)
        #expect(picture.layer?.borderWidth == 2)
        #expect(view.accessibilityPerformPress())
        #expect(oldPresses == 1)

        item.prepareForReuse()
        #expect(view.isHidden)
        #expect(!item.isSelected)
        #expect(picture.image == nil && picture.layer?.borderWidth == 0)
        #expect(view.subviews.compactMap { $0 as? NSTextField }
            .filter { !$0.isHidden }.allSatisfy { $0.stringValue.isEmpty })
        #expect(view.accessibilityIdentifier().isEmpty)
        #expect(view.accessibilityLabel() == nil)
        #expect(!view.accessibilityPerformPress())
        #expect(oldPresses == 1)

        var newPresses = 0
        item.configure(photo: try photo(4), client: client) { newPresses += 1 }
        #expect(!view.isHidden)
        #expect(view.accessibilityIdentifier() == "photo-4")
        #expect(view.subviews.compactMap { $0 as? NSTextField }
            .contains { $0.stringValue == "SYNTHETIC_4.ARW" })
        #expect(view.accessibilityPerformPress())
        #expect(newPresses == 1 && oldPresses == 1)
        item.prepareForReuse()
    }

    @Test func releasingAnItemRemovesOnlyItsOwnAttachedPresentation() throws {
        // AppKit can release a configured item without another end-display callback,
        // while the collection still owns its view. Keep only that view alive here.
        let collection = PhotoCollectionView()
        let client = try LibraryClient(baseURL: URL(string: "http://127.0.0.1:1")!)
        let retained = PhotoItem()
        var retainedPresses = 0
        retained.configure(photo: try photo(4), client: client) { retainedPresses += 1 }
        retained.isSelected = true
        collection.addSubview(retained.view)
        let retainedPicture = try #require(retained.view.subviews.compactMap { $0 as? NSImageView }.first)
        let image = NSImage(size: NSSize(width: 2, height: 2))
        retainedPicture.image = image

        var released: PhotoItem? = PhotoItem()
        weak let owner = released
        var releasedPresses = 0
        released?.configure(photo: try photo(3), client: client) { releasedPresses += 1 }
        released?.isSelected = true
        let orphan = try #require(released?.view)
        let orphanPicture = try #require(orphan.subviews.compactMap { $0 as? NSImageView }.first)
        collection.addSubview(orphan)
        #expect(!orphan.isHidden && orphanPicture.layer?.borderWidth == 2)

        released = nil
        #expect(owner == nil)
        #expect(orphan.superview == nil)
        #expect(orphan.isHidden)
        #expect(orphanPicture.image == nil && orphanPicture.layer?.borderWidth == 0)
        #expect(orphan.subviews.compactMap { $0 as? NSTextField }
            .filter { !$0.isHidden }.allSatisfy { $0.stringValue.isEmpty })
        #expect(!orphan.accessibilityPerformPress() && releasedPresses == 0)

        #expect(collection.subviews.count == 1 && retained.view.superview === collection)
        #expect(!retained.view.isHidden && retained.isSelected)
        #expect(retainedPicture.image === image && retainedPicture.layer?.borderWidth == 2)
        #expect(retained.view.accessibilityIdentifier() == "photo-4")
        #expect(retained.view.accessibilityPerformPress() && retainedPresses == 1)
    }

    @Test(arguments: [false, true])
    func clearingResultsCannotVendOldLayoutAttributes(atZeroWidth: Bool) {
        let collection = PhotoCollectionView(frame: NSRect(x: 0, y: 0, width: 848, height: 500))
        let layout = PhotoLayout()
        collection.collectionViewLayout = layout
        layout.aspectRatios = Array(repeating: 2.0 / 3, count: 251)
        layout.prepare()
        #expect(layout.layoutAttributesForItem(at: IndexPath(item: 2, section: 0)) != nil)

        layout.aspectRatios = []
        layout.invalidateGeometry()
        #expect(layout.layoutAttributesForItem(at: IndexPath(item: 2, section: 0)) == nil)
        #expect(layout.layoutAttributesForElements(in: CGRect(x: 0, y: 0, width: 848, height: 500)).isEmpty)
        if atZeroWidth { collection.setFrameSize(NSSize(width: 0, height: 500)) }
        layout.prepare()
        #expect(layout.layoutAttributesForItem(at: IndexPath(item: 2, section: 0)) == nil)
        #expect(layout.layoutAttributesForElements(in: CGRect(x: 0, y: 0, width: 848, height: 500)).isEmpty)
    }

    @Test func shrinkingAndRestoringResultsRebuildsOnlyCurrentPhotoGeometry() {
        let collection = PhotoCollectionView(frame: NSRect(x: 0, y: 0, width: 848, height: 500))
        let layout = PhotoLayout()
        collection.collectionViewLayout = layout
        layout.aspectRatios = Array(repeating: 1.5, count: 251)
        layout.prepare()
        layout.aspectRatios.removeLast()
        layout.invalidateGeometry()
        #expect(layout.layoutAttributesForItem(at: IndexPath(item: 250, section: 0)) == nil)
        layout.prepare()
        #expect(layout.geometry?.placements.count == 250)
        #expect(layout.layoutAttributesForItem(at: IndexPath(item: 249, section: 0)) != nil)
        layout.aspectRatios = []
        layout.invalidateGeometry()
        layout.prepare()
        layout.aspectRatios = [1.5, 2.0 / 3]
        layout.invalidateGeometry()
        layout.prepare()
        #expect(layout.geometry?.placements.count == 2)
        #expect(layout.layoutAttributesForItem(at: IndexPath(item: 1, section: 0)) != nil)
        #expect(layout.layoutAttributesForItem(at: IndexPath(item: 2, section: 0)) == nil)
    }
}
