import AppKit
import SwiftUI

/// A nonactivating inspector can move outside the library without taking its review shortcuts.
struct PhotoInfoPanel: NSViewRepresentable {
    let model: LibraryModel
    let requested: Bool
    let resetRequest: UUID

    func makeCoordinator() -> Coordinator { Coordinator(model: model) }

    func makeNSView(context: Context) -> AnchorView {
        let view = AnchorView()
        view.changedWindow = { [weak coordinator = context.coordinator] view in coordinator?.attach(view) }
        return view
    }

    func updateNSView(_ view: AnchorView, context: Context) {
        context.coordinator.model = model
        context.coordinator.requested = requested
        context.coordinator.resetIfNeeded(resetRequest)
        context.coordinator.attach(view)
    }

    static func dismantleNSView(_ view: AnchorView, coordinator: Coordinator) { coordinator.close() }

    @MainActor final class AnchorView: NSView {
        var changedWindow: ((AnchorView) -> Void)?
        override func viewDidMoveToWindow() { super.viewDidMoveToWindow(); changedWindow?(self) }
        override func layout() { super.layout(); changedWindow?(self) }
        override func hitTest(_ point: NSPoint) -> NSView? { nil }
    }

    @MainActor final class Panel: NSPanel {
        override var canBecomeKey: Bool { false }
        override var canBecomeMain: Bool { false }
    }

    @MainActor final class Coordinator: NSObject, NSWindowDelegate {
        var model: LibraryModel
        var requested = false
        weak var anchor: AnchorView?
        weak var owner: NSWindow?
        private var panel: Panel?
        private var movedByUser = false
        private var positioning = false
        private var lastReset: UUID
        private var savedFrameKey = "photoInfoPanelFrame"

        init(model: LibraryModel) {
            self.model = model
            lastReset = model.photoInfoPositionReset
            super.init()
            let center = NotificationCenter.default
            center.addObserver(self, selector: #selector(refreshVisibility), name: NSApplication.didBecomeActiveNotification, object: nil)
            center.addObserver(self, selector: #selector(screenChanged), name: NSApplication.didChangeScreenParametersNotification, object: nil)
        }

        func attach(_ view: AnchorView) {
            anchor = view
            guard let window = view.window else { return }
            if owner !== window {
                if let owner { NotificationCenter.default.removeObserver(self, name: nil, object: owner) }
                owner = window
                for name in [NSWindow.didMoveNotification, NSWindow.didResizeNotification,
                             NSWindow.didBecomeKeyNotification,
                             NSWindow.didMiniaturizeNotification, NSWindow.didDeminiaturizeNotification] {
                    NotificationCenter.default.addObserver(self, selector: #selector(refreshVisibility), name: name, object: window)
                }
                NotificationCenter.default.addObserver(self, selector: #selector(ownerClosing), name: NSWindow.willCloseNotification, object: window)
            }
            refreshVisibility()
        }

        func resetIfNeeded(_ request: UUID) {
            guard request != lastReset else { return }
            lastReset = request
            movedByUser = false
            model.preferences.removeObject(forKey: savedFrameKey)
            positionAtGallery()
        }

        @objc private func refreshVisibility() {
            guard requested, let owner, owner.isVisible, !owner.isMiniaturized,
                  owner.attachedSheet == nil else { panel?.orderOut(nil); return }
            if panel == nil { createPanel() }
            if !movedByUser { positionAtGallery() }
            panel?.orderFront(nil)
        }

        private func createPanel() {
            let panel = Panel(contentRect: NSRect(x: 0, y: 0, width: 312, height: 358),
                              styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            panel.title = "Photo Info"
            panel.isFloatingPanel = true
            // Keep the inspector with ordinary app windows, without covering other apps.
            panel.level = .normal
            panel.hidesOnDeactivate = false
            panel.isOpaque = false
            panel.backgroundColor = .clear
            panel.hasShadow = true
            panel.isReleasedWhenClosed = false
            panel.isMovableByWindowBackground = false
            panel.collectionBehavior = [.fullScreenAuxiliary]
            panel.contentView = NSHostingView(rootView: PhotoInfoCard(model: model))
            panel.delegate = self
            self.panel = panel
            owner?.addChildWindow(panel, ordered: .above)
            if let saved = model.preferences.string(forKey: savedFrameKey) {
                let frame = NSRectFromString(saved)
                if frame.width > 0, frame.height > 0, frame.origin.x.isFinite, frame.origin.y.isFinite {
                    movedByUser = true
                    position(NSRect(origin: frame.origin, size: NSSize(width: 312, height: 358)))
                }
            }
        }

        private func positionAtGallery() {
            guard let anchor, let owner else { return }
            let rect = owner.convertToScreen(anchor.convert(anchor.bounds, to: nil))
            position(NSRect(x: rect.maxX - 336, y: rect.minY + 12, width: 312, height: 358))
        }

        private func position(_ frame: NSRect) {
            positioning = true
            panel?.setFrame(Self.visibleFrame(frame, screens: NSScreen.screens.map(\.visibleFrame), fallback: owner?.screen?.visibleFrame), display: true)
            positioning = false
        }

        static func visibleFrame(_ frame: NSRect, screens: [NSRect], fallback: NSRect?) -> NSRect {
            let target = screens.max { a, b in
                let one = a.intersection(frame), two = b.intersection(frame)
                return max(0, one.width) * max(0, one.height) < max(0, two.width) * max(0, two.height)
            }.flatMap { $0.intersects(frame) ? $0 : nil } ?? fallback ?? screens.first
            guard let target else { return frame }
            return NSRect(x: min(max(frame.minX, target.minX + 8), target.maxX - frame.width - 8),
                          y: min(max(frame.minY, target.minY + 8), target.maxY - frame.height - 8),
                          width: frame.width, height: frame.height)
        }

        func windowDidMove(_ notification: Notification) {
            guard !positioning, let panel, panel.isVisible else { return }
            movedByUser = true
            model.preferences.set(NSStringFromRect(panel.frame), forKey: savedFrameKey)
        }

        @objc private func screenChanged() {
            if let panel { position(panel.frame) }
        }

        @objc private func ownerClosing() { panel?.orderOut(nil) }

        func close() {
            NotificationCenter.default.removeObserver(self)
            if let panel { owner?.removeChildWindow(panel) }
            panel?.delegate = nil
            panel?.close()
            panel = nil
        }
    }
}

struct PhotoInfoDragHandle: NSViewRepresentable {
    func makeNSView(context: Context) -> DragView { DragView() }
    func updateNSView(_ view: DragView, context: Context) {}

    final class DragView: NSView {
        private var grabOffset: NSPoint?
        override var mouseDownCanMoveWindow: Bool { false }
        override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
        override func resetCursorRects() { addCursorRect(bounds, cursor: .openHand) }
        override func mouseDown(with event: NSEvent) { grabOffset = event.locationInWindow }
        override func mouseDragged(with event: NSEvent) {
            guard let window, let grabOffset else { return }
            let cursor = window.convertPoint(toScreen: event.locationInWindow)
            window.setFrameOrigin(NSPoint(x: cursor.x - grabOffset.x, y: cursor.y - grabOffset.y))
        }
        override func mouseUp(with event: NSEvent) {
            grabOffset = nil
            guard let window else { return }
            window.setFrame(PhotoInfoPanel.Coordinator.visibleFrame(window.frame,
                screens: NSScreen.screens.map(\.visibleFrame), fallback: window.screen?.visibleFrame), display: true)
        }
    }
}
