import AppKit
import SwiftUI

/// One window-scoped path serves the gallery and inline preview without intercepting text input.
struct GalleryKeyboard: NSViewRepresentable {
    var model: LibraryModel

    func makeNSView(context: Context) -> KeyboardView { KeyboardView(model: model) }
    func updateNSView(_ view: KeyboardView, context: Context) { view.model = model }
    static func dismantleNSView(_ view: KeyboardView, coordinator: ()) { view.stop() }

    @MainActor final class KeyboardView: NSView {
        var model: LibraryModel
        private var monitor: Any?

        init(model: LibraryModel) { self.model = model; super.init(frame: .zero) }
        required init?(coder: NSCoder) { nil }

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            stop()
            guard let window else { return }
            window.titleVisibility = .hidden
            window.titlebarAppearsTransparent = true
            window.titlebarSeparatorStyle = .none
            monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
                let handled = MainActor.assumeIsolated {
                    guard let self, event.window === self.window else { return false }
                    if event.keyCode == 53, self.model.sequenceDrag.payload != nil {
                        self.model.sequenceDrag.cancel()
                        return true
                    }
                    guard self.window?.attachedSheet == nil,
                          !(self.window?.firstResponder is NSTextView),
                          !(self.window?.firstResponder is NSTextField),
                          !self.model.showingEditing, !self.model.showingPhotoCaption, !self.model.showingImports,
                          self.model.sequenceEditor == nil,
                          !self.model.showingSequences else { return false }
                    return self.model.handleGalleryKey(event)
                }
                return handled ? nil : event
            }
        }

        func stop() { if let monitor { NSEvent.removeMonitor(monitor) }; monitor = nil }
        isolated deinit { stop() }
    }
}

extension LibraryModel {
    func handleGalleryKey(_ event: NSEvent) -> Bool {
        if service.isConnected, event.keyCode == 51,
           event.modifierFlags.intersection([.command, .control, .option]) == .command {
            requestTrashSelection()
            return true
        }
        guard service.isConnected,
              event.modifierFlags.intersection([.command, .control, .option]).isEmpty else { return false }
        let key = event.charactersIgnoringModifiers?.lowercased() ?? ""
        if let rating = Int(key), (0...5).contains(rating) { rateSelection(rating); return true }
        switch key {
        case "q": flagSelection(.pick)
        case "r": flagSelection(.reject)
        case "w": stepPhoto(by: -1, extending: event.modifierFlags.contains(.shift))
        case "e": stepPhoto(by: 1, extending: event.modifierFlags.contains(.shift))
        default:
            switch event.keyCode {
            case 49: togglePreview()
            case 36: previewSelection()
            case 53: guard previewPhoto != nil else { return false }; previewPhoto = nil
            case 123: stepPhoto(by: -1, extending: event.modifierFlags.contains(.shift))
            case 124: stepPhoto(by: 1, extending: event.modifierFlags.contains(.shift))
            default: return false
            }
        }
        return true
    }
}
