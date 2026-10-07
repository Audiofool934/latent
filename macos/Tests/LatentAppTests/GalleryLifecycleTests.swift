import AppKit
import LatentCore
import SwiftUI
import Testing
@testable import LatentApp

@MainActor @Suite(.serialized)
struct GalleryLifecycleTests {
    @MainActor private struct Gallery {
        let model: LibraryModel
        let window: NSWindow
        let host: NSHostingView<MasonryGallery>
        let scroll: NSScrollView
        let collection: PhotoCollectionView

        func close() {
            (collection.dataSource as? MasonryGallery.Coordinator)?.cancelPrefetch()
            collection.visibleItems().forEach { ($0 as? PhotoItem)?.cancelLoading() }
            collection.delegate = nil
            collection.dataSource = nil
            model.shutdownService()
            window.close()
        }
    }

    private func photo(_ id: Int, rating: Int = 5) throws -> Photo {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(Photo.self, from: Data("""
        {"id":\(id),"name":"SYNTHETIC_\(id).ARW","remote_path":"/synthetic/\(id).ARW",
         "provider":"synthetic","fingerprint":"fixture-\(id)","size_bytes":1024,
         "capture_at":"2026:08:29 12:00:00","preview_width":\(id % 3 == 0 ? 360 : 480),
         "preview_height":\(id % 3 == 0 ? 480 : 320),
         "contact_url":"https://invalid.example/never-requested.jpg",
         "preview_url":"https://invalid.example/never-requested.jpg","preview_available":false,
         "rating":\(rating),"caption":""}
        """.utf8))
    }

    @Test func shiftClickSelectsTheOrderedRangeAndCommandClickTogglesOnePhoto() async throws {
        let gallery = try await makeGallery()
        defer { gallery.close() }
        func click(_ index: Int, modifiers: NSEvent.ModifierFlags = []) throws {
            let path = IndexPath(item: index, section: 0)
            let frame = try #require(gallery.collection.collectionViewLayout?.layoutAttributesForItem(at: path)?.frame)
            let point = NSPoint(x: frame.midX, y: frame.midY)
            #expect(gallery.collection.indexPathForItem(at: point) == path)
            let event = try #require(NSEvent.mouseEvent(with: .leftMouseDown,
                location: gallery.collection.convert(point, to: nil), modifierFlags: modifiers,
                timestamp: 0, windowNumber: gallery.window.windowNumber, context: nil,
                eventNumber: 1, clickCount: 1, pressure: 1))
            gallery.collection.mouseDown(with: event)
        }
        try click(0)
        try click(2, modifiers: .shift)
        #expect(gallery.model.selectedIDs == [3, 4, 5])
        try click(1, modifiers: .shift)
        #expect(gallery.model.selectedIDs == [3, 4])
        try click(3, modifiers: .command)
        #expect(gallery.model.selectedIDs == [3, 4, 6])
        try click(0, modifiers: .command)
        #expect(gallery.model.selectedIDs == [4, 6])
        try await settle(gallery)
        #expect(gallery.collection.selectionIndexPaths == [IndexPath(item: 1, section: 0), IndexPath(item: 3, section: 0)])
    }

    private func descendants(_ view: NSView) -> [NSView] {
        view.subviews.flatMap { [$0] + descendants($0) }
    }

    private func settle(_ gallery: Gallery) async throws {
        for _ in 0..<3 {
            gallery.host.layoutSubtreeIfNeeded()
            gallery.collection.prepareContent(in: gallery.collection.visibleRect)
            gallery.collection.needsLayout = true
            gallery.scroll.layoutSubtreeIfNeeded()
            gallery.collection.layoutSubtreeIfNeeded()
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(!gallery.window.isVisible && !gallery.window.isKeyWindow && !gallery.window.isMainWindow)
    }

    private func makeGallery() async throws -> Gallery {
        // NSCollectionView installs its real scroll observers only in a window.
        // This owned test window is never ordered on screen; app activation is prohibited.
        NSApplication.shared.setActivationPolicy(.prohibited)
        let client = try LibraryClient(baseURL: URL(string: "http://127.0.0.1:1")!)
        let model = LibraryModel(client: client, preferences: MemoryPreferences())
        model.source = .starred
        model.photos = try (Array(3...251) + [1]).map { try photo($0) }
        model.total = 251
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 800, height: 430),
                              styleMask: .borderless, backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        var returnedGallery = false
        defer {
            if !returnedGallery {
                model.shutdownService()
                window.close()
            }
        }
        let host = NSHostingView(rootView: MasonryGallery(model: model))
        window.contentView = host
        for _ in 0..<5 {
            host.layoutSubtreeIfNeeded()
            try await Task.sleep(for: .milliseconds(10))
        }
        let collection = try #require(descendants(host).compactMap { $0 as? PhotoCollectionView }.first)
        let scroll = try #require(collection.enclosingScrollView)
        let gallery = Gallery(model: model, window: window, host: host,
                              scroll: scroll, collection: collection)
        try await settle(gallery)
        #expect(collection.numberOfItems(inSection: 0) == 250)
        #expect(!collection.visibleItems().isEmpty)
        returnedGallery = true
        return gallery
    }

    private func scrollToEnd(_ gallery: Gallery) async throws {
        let layout = try #require(gallery.collection.collectionViewLayout)
        let end = max(0, layout.collectionViewContentSize.height - gallery.scroll.contentSize.height)
        gallery.scroll.contentView.scroll(to: NSPoint(x: 0, y: end))
        gallery.scroll.reflectScrolledClipView(gallery.scroll.contentView)
        try await settle(gallery)
        // Prevent the false positive from a detached view whose cells never actually scroll.
        #expect(gallery.collection.visibleItems().contains { $0.view.accessibilityIdentifier() == "photo-251" })
    }

    @Test func retiredSelectedDrawingIsClearedWhenResultsAreEmptied() async throws {
        let gallery = try await makeGallery()
        defer { gallery.close() }
        let original = try #require(gallery.collection.item(at: IndexPath(item: 0, section: 0)) as? PhotoItem)
        #expect(original.view.accessibilityPerformPress())
        #expect(gallery.model.selectedID == 3 && original.isSelected)
        #expect(gallery.window.firstResponder === gallery.collection)
        gallery.model.photos[0] = try photo(3, rating: 0)
        try await settle(gallery)
        try await scrollToEnd(gallery)

        // Later state notifications must not repaint a cached, retired item.
        #expect(original.view.isHidden)
        #expect(!gallery.collection.visibleItems().contains { $0 === original })
        original.isSelected = false
        original.isSelected = true
        original.updateAnnotation(try photo(3, rating: 3))

        // Retained, hidden views are still backing-store inputs. Inspect their content,
        // not just visibleItems/AX, which both excluded the card in the GUI failure.
        let retired = gallery.collection.subviews.filter { $0.isHiddenOrHasHiddenAncestor }
        #expect(!retired.isEmpty)
        #expect(retired.flatMap(descendants).compactMap { $0 as? NSTextField }
            .allSatisfy { $0.isHidden || $0.stringValue.isEmpty })
        #expect(retired.flatMap(descendants).compactMap { $0 as? NSImageView }
            .allSatisfy { $0.image == nil && $0.layer?.borderWidth == 0 && $0.layer?.backgroundColor == nil })

        gallery.model.photos = try (Array(4...251) + [1, 2]).map { try photo($0) }
        gallery.model.total = 250
        gallery.model.selectedID = 4
        try await settle(gallery)
        let last = try #require(gallery.collection.item(at: IndexPath(item: 249, section: 0)))
        #expect(last.view.accessibilityPerformPress())
        #expect(gallery.model.selectedID == 2)
        let search = NSTextField(frame: NSRect(x: 0, y: 0, width: 160, height: 22))
        gallery.host.addSubview(search)
        #expect(gallery.window.makeFirstResponder(search))
        gallery.model.source = .search(query: "missing", order: .closest)
        gallery.model.photos = []
        gallery.model.total = 0
        gallery.model.selectedID = nil
        gallery.model.datasetID = UUID()
        try await settle(gallery)
        #expect(gallery.collection.numberOfItems(inSection: 0) == 0)
        #expect(gallery.collection.visibleItems().isEmpty)
        #expect(gallery.collection.selectionIndexPaths.isEmpty)
        #expect(descendants(gallery.collection).compactMap { $0 as? NSTextField }
            .allSatisfy { $0.isHidden || $0.stringValue.isEmpty })
        #expect(descendants(gallery.collection).compactMap { $0 as? NSImageView }
            .allSatisfy { $0.image == nil && $0.layer?.borderWidth == 0 && $0.layer?.backgroundColor == nil })

        gallery.model.photos = try (101...112).map { try photo($0) }
        gallery.model.datasetID = UUID()
        gallery.model.errorMessage = nil
        gallery.window.setContentSize(NSSize(width: 640, height: 430))
        try await settle(gallery)
        #expect(!gallery.collection.visibleItems().isEmpty)
        for item in gallery.collection.visibleItems() {
            let index = try #require(gallery.collection.indexPath(for: item))
            #expect(!item.view.isHidden)
            #expect(item.view.accessibilityIdentifier() == "photo-\(gallery.model.photos[index.item].id)")
        }
    }

    @Test func returningToARetainedCellRestoresItsCurrentAnnotationAndSelection() async throws {
        let gallery = try await makeGallery()
        defer { gallery.close() }
        let first = try #require(gallery.collection.item(at: IndexPath(item: 0, section: 0)))
        #expect(first.view.accessibilityPerformPress())
        try await scrollToEnd(gallery)
        gallery.model.photos[0] = try photo(3, rating: 2)
        try await settle(gallery)
        gallery.scroll.contentView.scroll(to: .zero)
        gallery.scroll.reflectScrolledClipView(gallery.scroll.contentView)
        try await settle(gallery)
        let returned = try #require(gallery.collection.item(at: IndexPath(item: 0, section: 0)))
        #expect(!returned.view.isHidden && returned.isSelected)
        #expect(returned.view.accessibilityIdentifier() == "photo-3")
        #expect(returned.view.subviews.compactMap { $0 as? NSTextField }
            .contains { $0.stringValue.hasPrefix("★★  ·") })
        #expect(returned.view.subviews.compactMap { $0 as? NSImageView }
            .contains { $0.layer?.borderWidth == 2 && $0.layer?.backgroundColor != nil })
        #expect(returned.view.accessibilityPerformPress())
        #expect(gallery.model.selectedID == 3)
    }
}
