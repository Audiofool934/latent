import AppKit
import Testing
@testable import LatentApp

private final class PreviewGestureEvent: NSEvent {
    private let point: CGPoint
    private let magnifyDelta: CGFloat
    private let scrollDelta: CGSize
    private let precise: Bool
    private let modifiers: NSEvent.ModifierFlags

    init(point: CGPoint = CGPoint(x: 400, y: 300), magnification: CGFloat = 0,
         scroll: CGSize = .zero, precise: Bool = true, modifiers: NSEvent.ModifierFlags = []) {
        self.point = point
        self.magnifyDelta = magnification
        self.scrollDelta = scroll
        self.precise = precise
        self.modifiers = modifiers
        super.init()
    }

    required init?(coder: NSCoder) { nil }

    override var locationInWindow: CGPoint { point }
    override var magnification: CGFloat { magnifyDelta }
    override var scrollingDeltaX: CGFloat { scrollDelta.width }
    override var scrollingDeltaY: CGFloat { scrollDelta.height }
    override var hasPreciseScrollingDeltas: Bool { precise }
    override var modifierFlags: NSEvent.ModifierFlags { modifiers }
}

@MainActor private final class PreviewScrollRecorder: NSView {
    var receivedScrolls = 0
    override func scrollWheel(with event: NSEvent) { receivedScrolls += 1 }
}

struct PhotoPreviewGeometryTests {
    private func imagePoint(_ point: CGPoint, in geometry: PhotoPreviewGeometry) -> CGPoint {
        let frame = geometry.imageFrame
        return CGPoint(x: (point.x - frame.minX) / frame.width,
                       y: (point.y - frame.minY) / frame.height)
    }

    @Test func repeatedZoomKeepsTheImagePointUnderThePointerWithoutDrift() {
        var geometry = PhotoPreviewGeometry(imageSize: CGSize(width: 2048, height: 1536),
                                            viewportSize: CGSize(width: 800, height: 600))
        let pointer = CGPoint(x: 510, y: 240)
        let original = imagePoint(pointer, in: geometry)
        for step in 1...100 {
            geometry.setZoom(1 + CGFloat(step) / 25, anchor: pointer)
            let point = imagePoint(pointer, in: geometry)
            #expect(abs(point.x - original.x) < 0.000001)
            #expect(abs(point.y - original.y) < 0.000001)
        }
        for step in (0..<100).reversed() {
            geometry.setZoom(1 + CGFloat(step) / 25, anchor: pointer)
        }
        #expect(geometry.zoom == 1 && geometry.offset == .zero)
        #expect(geometry.imageFrame == CGRect(x: 0, y: 0, width: 800, height: 600))
    }

    @Test(arguments: [CGSize(width: 2048, height: 768), CGSize(width: 768, height: 2048), CGSize(width: 1600, height: 1600)])
    func panningNeverExposesAnEdgeAndKeepsSmallAxesCentered(size: CGSize) {
        var geometry = PhotoPreviewGeometry(imageSize: size, viewportSize: CGSize(width: 800, height: 600))
        geometry.setZoom(2)
        for direction: CGFloat in [-1, 1] {
            geometry.pan(to: CGSize(width: direction * 10000, height: direction * 10000))
            let frame = geometry.imageFrame
            if frame.width >= 800 {
                #expect(frame.minX <= 0 && frame.maxX >= 800)
            } else {
                #expect(frame.midX == 400 && geometry.offset.width == 0)
            }
            if frame.height >= 600 {
                #expect(frame.minY <= 0 && frame.maxY >= 600)
            } else {
                #expect(frame.midY == 300 && geometry.offset.height == 0)
            }
        }
        geometry.setZoom(1)
        #expect(geometry.offset == .zero)
    }

    @Test func windowResizePreservesTheCenteredImagePointWhileKeepingZoom() {
        var geometry = PhotoPreviewGeometry(imageSize: CGSize(width: 2048, height: 1536),
                                            viewportSize: CGSize(width: 800, height: 600))
        geometry.setZoom(3)
        geometry.pan(to: CGSize(width: -240, height: 90))
        let original = imagePoint(CGPoint(x: 400, y: 300), in: geometry)
        geometry.resize(to: CGSize(width: 1200, height: 800))
        let resized = imagePoint(CGPoint(x: 600, y: 400), in: geometry)
        #expect(geometry.zoom == 3)
        #expect(abs(resized.x - original.x) < 0.000001)
        #expect(abs(resized.y - original.y) < 0.000001)
    }

    @Test func zoomLimitsAndAnInitiallyUnlaidOutViewStayFinite() {
        var geometry = PhotoPreviewGeometry(imageSize: CGSize(width: 2048, height: 1536))
        geometry.setZoom(.nan)
        geometry.setZoom(.infinity)
        #expect(geometry.zoom == 1 && geometry.offset == .zero)
        geometry.resize(to: CGSize(width: 800, height: 600))
        geometry.setZoom(100)
        #expect(geometry.zoom == PhotoPreviewGeometry.maximumZoom)
        geometry.setZoom(-1)
        #expect(geometry.zoom == 1 && geometry.offset == .zero)
        #expect(geometry.imageFrame.width == 800)
    }
}

@MainActor @Suite(.serialized)
struct PhotoPreviewCanvasTests {
    private func image() throws -> CGImage {
        let context = try #require(CGContext(data: nil, width: 64, height: 48, bitsPerComponent: 8,
            bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue))
        return try #require(context.makeImage())
    }

    private func pointer(_ type: NSEvent.EventType, x: CGFloat, clicks: Int = 1) throws -> NSEvent {
        try #require(NSEvent.mouseEvent(with: type, location: CGPoint(x: x, y: 300), modifierFlags: [],
            timestamp: 0, windowNumber: 0, context: nil, eventNumber: 1, clickCount: clicks, pressure: 1))
    }

    @Test func realDragHandlerUsesTotalMovementOnlyOnceAndRetainsPositionDuringUpdates() throws {
        let canvas = PhotoPreviewCanvas(frame: CGRect(x: 0, y: 0, width: 800, height: 600))
        let photo = try image()
        canvas.configure(image: photo, photoID: 1, zoom: 3)
        canvas.mouseDown(with: try pointer(.leftMouseDown, x: 400))
        canvas.mouseDragged(with: try pointer(.leftMouseDragged, x: 402))
        #expect(canvas.geometry.offset == .zero)
        for x: CGFloat in [405, 410, 415] {
            canvas.mouseDragged(with: try pointer(.leftMouseDragged, x: x))
            #expect(canvas.geometry.offset.width == x - 400)
        }
        canvas.mouseUp(with: try pointer(.leftMouseUp, x: 415))
        // A rating updates the SwiftUI representable without changing the photo or zoom.
        canvas.configure(image: photo, photoID: 1, zoom: 3)
        #expect(canvas.geometry.zoom == 3 && canvas.geometry.offset.width == 15)
        canvas.configure(image: photo, photoID: 2, zoom: 1)
        #expect(canvas.geometry.zoom == 1 && canvas.geometry.offset == .zero)
        canvas.mouseDragged(with: try pointer(.leftMouseDragged, x: 450))
        #expect(canvas.geometry.offset == .zero)
    }

    @Test func doubleClickTogglesZoomAndFitAndPublishesTheControlValue() throws {
        let canvas = PhotoPreviewCanvas(frame: CGRect(x: 0, y: 0, width: 800, height: 600))
        canvas.configure(image: try image(), photoID: 1, zoom: 1)
        var reported: [CGFloat] = []
        canvas.onZoomChange = { reported.append($0) }
        canvas.mouseDown(with: try pointer(.leftMouseDown, x: 520, clicks: 2))
        #expect(canvas.geometry.zoom == 2)
        #expect(canvas.geometry.offset.width != 0)
        canvas.mouseDown(with: try pointer(.leftMouseDown, x: 520, clicks: 2))
        #expect(canvas.geometry.zoom == 1 && canvas.geometry.offset == .zero)
        #expect(reported == [2, 1])
    }

    @Test func pinchKeepsItsAnchorAndPublishesZoomInBothDirections() throws {
        let canvas = PhotoPreviewCanvas(frame: CGRect(x: 0, y: 0, width: 800, height: 600))
        canvas.configure(image: try image(), photoID: 1, zoom: 1)
        var reported: [CGFloat] = []
        canvas.onZoomChange = { reported.append($0) }
        let event = PreviewGestureEvent(point: CGPoint(x: 520, y: 240), magnification: 0.5)
        let anchor = canvas.convert(event.locationInWindow, from: nil)
        let original = canvas.geometry.imageFrame
        canvas.magnify(with: event)
        let magnified = canvas.geometry.imageFrame
        #expect(canvas.geometry.zoom == 1.5)
        #expect(abs((anchor.x - magnified.minX) / magnified.width - (anchor.x - original.minX) / original.width) < 0.000001)
        #expect(abs((anchor.y - magnified.minY) / magnified.height - (anchor.y - original.minY) / original.height) < 0.000001)
        canvas.magnify(with: PreviewGestureEvent(point: event.locationInWindow, magnification: -1.0 / 3))
        #expect(abs(canvas.geometry.zoom - 1) < 0.000001)
        #expect(abs(canvas.geometry.offset.width) < 0.000001 && abs(canvas.geometry.offset.height) < 0.000001)
        #expect(reported.count == 2 && reported[0] == 1.5 && abs(reported[1] - 1) < 0.000001)
    }

    @Test func optionScrollZoomsAroundItsPointerAndPublishesTheControlValue() throws {
        let canvas = PhotoPreviewCanvas(frame: CGRect(x: 0, y: 0, width: 800, height: 600))
        canvas.configure(image: try image(), photoID: 1, zoom: 1)
        var reported: [CGFloat] = []
        canvas.onZoomChange = { reported.append($0) }
        let event = PreviewGestureEvent(point: CGPoint(x: 520, y: 240),
            scroll: CGSize(width: 25, height: 70), modifiers: .option)
        let anchor = canvas.convert(event.locationInWindow, from: nil)
        let original = canvas.geometry.imageFrame
        canvas.scrollWheel(with: event)
        let magnified = canvas.geometry.imageFrame
        #expect(canvas.geometry.zoom > 1)
        #expect(abs((anchor.x - magnified.minX) / magnified.width - (anchor.x - original.minX) / original.width) < 0.000001)
        #expect(abs((anchor.y - magnified.minY) / magnified.height - (anchor.y - original.minY) / original.height) < 0.000001)
        #expect(reported == [canvas.geometry.zoom])
        canvas.scrollWheel(with: PreviewGestureEvent(point: event.locationInWindow,
            scroll: CGSize(width: -25, height: -70), modifiers: .option))
        #expect(abs(canvas.geometry.zoom - 1) < 0.000001)
        #expect(abs(canvas.geometry.offset.width) < 0.000001 && abs(canvas.geometry.offset.height) < 0.000001)
        #expect(reported.count == 2 && abs(reported[1] - 1) < 0.000001)
    }

    @Test func scrollingPansInBothAxesOnlyWhileZoomedAndNeverReachesTheGallery() throws {
        let parent = PreviewScrollRecorder(frame: CGRect(x: 0, y: 0, width: 800, height: 600))
        let canvas = PhotoPreviewCanvas(frame: parent.bounds)
        parent.addSubview(canvas)
        let photo = try image()
        canvas.configure(image: photo, photoID: 1, zoom: 1)
        var reported: [CGFloat] = []
        canvas.onZoomChange = { reported.append($0) }
        let precise = PreviewGestureEvent(scroll: CGSize(width: 12, height: -9))
        canvas.scrollWheel(with: precise)
        #expect(canvas.geometry.zoom == 1 && canvas.geometry.offset == .zero)
        #expect(parent.receivedScrolls == 0)
        canvas.configure(image: photo, photoID: 1, zoom: 3)
        canvas.scrollWheel(with: precise)
        #expect(canvas.geometry.offset == CGSize(width: 12, height: -9))
        canvas.scrollWheel(with: PreviewGestureEvent(scroll: CGSize(width: -12, height: 9)))
        #expect(canvas.geometry.offset == .zero)
        canvas.scrollWheel(with: PreviewGestureEvent(scroll: CGSize(width: 2, height: -3), precise: false))
        #expect(canvas.geometry.offset == CGSize(width: 20, height: -30))
        #expect(canvas.geometry.zoom == 3 && reported.isEmpty)
        #expect(parent.receivedScrolls == 0)
    }
}
