import Foundation
import ImageIO
import SwiftUI
import UniformTypeIdentifiers

// Render the approved vector artwork into a static macOS ICNS layout.
// Its transparent inset and continuous corners must be present in each PNG.
guard CommandLine.arguments.count == 3 else {
    fatalError("Usage: render-macos-icon.swift input.pdf output.iconset")
}
let sourceURL = URL(fileURLWithPath: CommandLine.arguments[1])
let destinationURL = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
guard let document = CGPDFDocument(sourceURL as CFURL), let page = document.page(at: 1) else {
    fatalError("Cannot read the logo PDF: \(sourceURL.path)")
}
let sourceBounds = page.getBoxRect(.mediaBox)
let tile = CGRect(x: 100, y: 100, width: 824, height: 824)
let outline = RoundedRectangle(cornerRadius: 185, style: .continuous).path(in: tile).cgPath
let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!

for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        guard let context = CGContext(data: nil, width: pixels, height: pixels,
            bitsPerComponent: 8, bytesPerRow: pixels * 4, space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
            fatalError("Cannot create \(pixels)-pixel icon context")
        }
        let renderScale = CGFloat(pixels) / 1024
        context.scaleBy(x: renderScale, y: renderScale)

        context.saveGState()
        // Quartz shadow dimensions do not inherit the drawing transform.
        context.setShadow(offset: CGSize(width: 0, height: -8 * renderScale), blur: 16 * renderScale,
            color: CGColor(gray: 0, alpha: 0.18))
        context.setFillColor(CGColor(gray: 1, alpha: 1))
        context.addPath(outline)
        context.fillPath()
        context.restoreGState()

        context.addPath(outline)
        context.clip()
        context.translateBy(x: tile.minX, y: tile.minY)
        context.scaleBy(x: tile.width / sourceBounds.width, y: tile.height / sourceBounds.height)
        context.translateBy(x: -sourceBounds.minX, y: -sourceBounds.minY)
        context.drawPDFPage(page)

        let suffix = scale == 2 ? "@2x" : ""
        let output = destinationURL.appendingPathComponent("icon_\(size)x\(size)\(suffix).png")
        guard let image = context.makeImage(),
            let destination = CGImageDestinationCreateWithURL(output as CFURL,
                UTType.png.identifier as CFString, 1, nil) else {
            fatalError("Cannot create icon PNG: \(output.path)")
        }
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else {
            fatalError("Cannot save icon PNG: \(output.path)")
        }
    }
}
