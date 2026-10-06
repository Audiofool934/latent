import AppKit
import SwiftUI

/// AppKit owns toolbar focus so Cmd-F also works when the collection view is first responder.
struct NativeSearchField: NSViewRepresentable {
    @Binding var text: String
    var focusRequest: UUID
    var placeholder = "Search indexed photos"
    var onSubmit: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeNSView(context: Context) -> NSTextField {
        let field = NSTextField()
        field.isBordered = false
        field.drawsBackground = false
        field.focusRingType = .none
        field.font = .systemFont(ofSize: 14)
        field.textColor = .labelColor
        field.placeholderString = placeholder
        field.lineBreakMode = .byTruncatingTail
        field.cell?.usesSingleLineMode = true
        field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        field.setAccessibilityIdentifier("semantic-search")
        field.setAccessibilityLabel(placeholder)
        field.toolTip = "Search indexed photos with words. Press Return to search."
        field.delegate = context.coordinator
        return field
    }

    func updateNSView(_ field: NSTextField, context: Context) {
        context.coordinator.parent = self
        field.placeholderString = placeholder
        field.setAccessibilityLabel(placeholder)
        if field.stringValue != text { field.stringValue = text }
        if context.coordinator.lastFocusRequest != focusRequest {
            context.coordinator.lastFocusRequest = focusRequest
            // Toolbar updates can run before AppKit finishes restoring the prior first responder.
            DispatchQueue.main.async { [weak field] in
                guard let field, let window = field.window else { return }
                window.makeFirstResponder(field)
                field.selectText(nil)
            }
        }
    }

    @MainActor final class Coordinator: NSObject, NSTextFieldDelegate {
        var parent: NativeSearchField
        var lastFocusRequest: UUID

        init(_ parent: NativeSearchField) {
            self.parent = parent
            lastFocusRequest = parent.focusRequest
        }

        func controlTextDidChange(_ notification: Notification) {
            guard let field = notification.object as? NSTextField else { return }
            parent.text = field.stringValue
        }

        func control(_ control: NSControl, textView: NSTextView, doCommandBy commandSelector: Selector) -> Bool {
            if commandSelector == #selector(NSResponder.insertNewline(_:)) {
                parent.text = control.stringValue
                parent.onSubmit()
                control.window?.makeFirstResponder(nil)
                return true
            }
            if commandSelector == #selector(NSResponder.cancelOperation(_:)) {
                control.window?.makeFirstResponder(nil)
                return true
            }
            return false
        }
    }
}
