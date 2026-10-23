import AppKit
import SwiftUI

/// Zoom is relative to the fitted cached image, not to the original photo's pixels.
struct PhotoPreviewGeometry {
    static let maximumZoom: CGFloat = 8
    private(set) var imageSize: CGSize
    private(set) var viewportSize: CGSize
    private(set) var zoom: CGFloat = 1
    private(set) var offset: CGSize = .zero

    init(imageSize: CGSize = .zero, viewportSize: CGSize = .zero) {
        self.imageSize = imageSize
        self.viewportSize = viewportSize
    }

    var fittedSize: CGSize {
        guard imageSize.width > 0, imageSize.height > 0,
              viewportSize.width > 0, viewportSize.height > 0 else { return .zero }
        let fit = min(viewportSize.width / imageSize.width, viewportSize.height / imageSize.height)
        return CGSize(width: imageSize.width * fit, height: imageSize.height * fit)
    }

    var imageFrame: CGRect {
        let size = CGSize(width: fittedSize.width * zoom, height: fittedSize.height * zoom)
        return CGRect(x: (viewportSize.width - size.width) / 2 + offset.width,
                      y: (viewportSize.height - size.height) / 2 + offset.height,
                      width: size.width, height: size.height)
    }

    mutating func resize(to size: CGSize) {
        guard size != viewportSize else { return }
        let previousFit = fittedSize
        viewportSize = size
        let nextFit = fittedSize
        if previousFit.width > 0, previousFit.height > 0 {
            offset.width *= nextFit.width / previousFit.width
            offset.height *= nextFit.height / previousFit.height
        }
        pan(to: offset)
    }

    mutating func setZoom(_ requested: CGFloat, anchor: CGPoint? = nil) {
        guard requested.isFinite else { return }
        let next = min(Self.maximumZoom, max(1, requested))
        let center = CGPoint(x: viewportSize.width / 2, y: viewportSize.height / 2)
        let point = anchor ?? center
        let ratio = next / zoom
        let nextOffset = CGSize(width: (point.x - center.x) * (1 - ratio) + offset.width * ratio,
                                height: (point.y - center.y) * (1 - ratio) + offset.height * ratio)
        zoom = next
        pan(to: nextOffset)
    }

    mutating func pan(to requested: CGSize) {
        let limitX = max(0, (fittedSize.width * zoom - viewportSize.width) / 2)
        let limitY = max(0, (fittedSize.height * zoom - viewportSize.height) / 2)
        offset = CGSize(width: min(limitX, max(-limitX, requested.width)),
                        height: min(limitY, max(-limitY, requested.height)))
    }
}

struct PhotoPreviewViewport: NSViewRepresentable {
    var image: CGImage
    var photoID: Int
    @Binding var zoom: CGFloat

    func makeNSView(context: Context) -> PhotoPreviewCanvas { PhotoPreviewCanvas() }

    func updateNSView(_ view: PhotoPreviewCanvas, context: Context) {
        view.configure(image: image, photoID: photoID, zoom: zoom)
        view.onZoomChange = { zoom = $0 }
    }
}

/// This view owns only image gestures. Window-scoped gallery keys still use GalleryKeyboard.
@MainActor final class PhotoPreviewCanvas: NSView {
    private(set) var geometry = PhotoPreviewGeometry()
    private var photoID: Int?
    private var sourceImage: CGImage?
    private var drawingImage: NSImage?
    private var dragOrigin: (point: CGPoint, offset: CGSize)?
    private var isDragging = false
    var onZoomChange: ((CGFloat) -> Void)?

    override var isFlipped: Bool { true }
    override var acceptsFirstResponder: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.masksToBounds = true
        setAccessibilityElement(true)
        setAccessibilityRole(.image)
        setAccessibilityLabel("Photo preview")
        setAccessibilityIdentifier("photo-preview-canvas")
        toolTip = "Pinch or Option-scroll to zoom. Drag or scroll to pan. Double-click to zoom or fit."
        setAccessibilityHelp(toolTip)
    }

    required init?(coder: NSCoder) { nil }

    func configure(image: CGImage, photoID: Int, zoom: CGFloat) {
        if self.photoID != photoID {
            self.photoID = photoID
            dragOrigin = nil
            isDragging = false
            geometry = PhotoPreviewGeometry(imageSize: CGSize(width: image.width, height: image.height),
                                            viewportSize: bounds.size)
        }
        if sourceImage !== image {
            sourceImage = image
            drawingImage = NSImage(cgImage: image, size: CGSize(width: image.width, height: image.height))
            needsDisplay = true
        }
        geometry.resize(to: bounds.size)
        if geometry.zoom != zoom {
            dragOrigin = nil
            geometry.setZoom(zoom)
        }
        updatePresentation()
    }

    override func layout() {
        super.layout()
        geometry.resize(to: bounds.size)
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        NSGraphicsContext.current?.imageInterpolation = .high
        drawingImage?.draw(in: geometry.imageFrame, from: .zero, operation: .sourceOver,
                           fraction: 1, respectFlipped: true, hints: nil)
    }

    override func resetCursorRects() {
        addCursorRect(bounds, cursor: geometry.zoom > 1 ? .openHand : .zoomIn)
    }

    private func updatePresentation() {
        needsDisplay = true
        window?.invalidateCursorRects(for: self)
        setAccessibilityValue(geometry.zoom == 1 ? "Fit to window" : String(format: "%.1f times fit", geometry.zoom))
    }

    func setZoom(_ zoom: CGFloat, anchor: CGPoint? = nil) {
        dragOrigin = nil
        isDragging = false
        geometry.resize(to: bounds.size)
        geometry.setZoom(zoom, anchor: anchor)
        updatePresentation()
        onZoomChange?(geometry.zoom)
    }

    private func toggleZoom(at point: CGPoint) {
        setZoom(geometry.zoom > 1 ? 1 : 2, anchor: point)
    }

    override func magnify(with event: NSEvent) {
        setZoom(geometry.zoom * (1 + event.magnification), anchor: convert(event.locationInWindow, from: nil))
    }

    override func smartMagnify(with event: NSEvent) {
        toggleZoom(at: convert(event.locationInWindow, from: nil))
    }

    override func scrollWheel(with event: NSEvent) {
        if event.modifierFlags.contains(.option) {
            setZoom(geometry.zoom * exp(event.scrollingDeltaY * 0.01),
                    anchor: convert(event.locationInWindow, from: nil))
        } else if geometry.zoom > 1 {
            let multiplier: CGFloat = event.hasPreciseScrollingDeltas ? 1 : 10
            geometry.pan(to: CGSize(width: geometry.offset.width + event.scrollingDeltaX * multiplier,
                                    height: geometry.offset.height + event.scrollingDeltaY * multiplier))
            needsDisplay = true
        }
        // Never forward a preview scroll into the retained gallery underneath it.
    }

    override func mouseDown(with event: NSEvent) {
        window?.makeFirstResponder(self)
        let point = convert(event.locationInWindow, from: nil)
        if event.clickCount == 2 {
            toggleZoom(at: point)
        } else if geometry.zoom > 1 {
            dragOrigin = (point, geometry.offset)
            isDragging = false
        }
    }

    override func mouseDragged(with event: NSEvent) {
        guard let origin = dragOrigin else { return }
        let point = convert(event.locationInWindow, from: nil)
        let delta = CGSize(width: point.x - origin.point.x, height: point.y - origin.point.y)
        guard isDragging || hypot(delta.width, delta.height) >= 4 else { return }
        isDragging = true
        NSCursor.closedHand.set()
        geometry.pan(to: CGSize(width: origin.offset.width + delta.width,
                                height: origin.offset.height + delta.height))
        needsDisplay = true
    }

    override func mouseUp(with event: NSEvent) {
        dragOrigin = nil
        isDragging = false
        updatePresentation()
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if window == nil {
            dragOrigin = nil
            isDragging = false
        }
    }
}
