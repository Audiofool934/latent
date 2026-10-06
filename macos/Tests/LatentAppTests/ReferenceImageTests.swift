import Foundation
import ImageIO
import LatentCore
import Testing
import UniformTypeIdentifiers
@testable import LatentApp

struct ReferenceImageTests {
    @Test func largeOrientedPhoneImageBecomesSmallJPEGWithoutLocationMetadata() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("phone.jpg")
        let colorSpace = CGColorSpaceCreateDeviceRGB()
        let context = try #require(CGContext(data: nil, width: 1800, height: 1200, bitsPerComponent: 8,
            bytesPerRow: 0, space: colorSpace, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue))
        let image = try #require(context.makeImage())
        let destination = try #require(CGImageDestinationCreateWithURL(url as CFURL, UTType.jpeg.identifier as CFString, 1, nil))
        CGImageDestinationAddImage(destination, image, [
            kCGImagePropertyOrientation: 6,
            kCGImagePropertyExifDictionary: [kCGImagePropertyExifDateTimeOriginal: "2026:08:29 12:00:00"],
            kCGImagePropertyTIFFDictionary: [kCGImagePropertyTIFFModel: "Private camera metadata"],
            kCGImagePropertyGPSDictionary: [kCGImagePropertyGPSLatitude: 1.3, kCGImagePropertyGPSLongitude: 103.8],
        ] as CFDictionary)
        #expect(CGImageDestinationFinalize(destination))
        let reference = try ReferenceImageLoader.load(url)
        let source = try #require(CGImageSourceCreateWithData(reference.jpeg as CFData, nil))
        let result = try #require(CGImageSourceCreateImageAtIndex(source, 0, nil))
        #expect(result.width < result.height && result.height == 512)
        #expect(reference.name == "phone.jpg" && reference.jpeg.count < 1024 * 1024)
        let properties = try #require(CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [String: Any])
        #expect(properties[kCGImagePropertyGPSDictionary as String] == nil)
        let exif = properties[kCGImagePropertyExifDictionary as String] as? [String: Any]
        let tiff = properties[kCGImagePropertyTIFFDictionary as String] as? [String: Any]
        #expect(exif?[kCGImagePropertyExifDateTimeOriginal as String] == nil)
        #expect(tiff?[kCGImagePropertyTIFFModel as String] == nil)
    }

    @Test func nonFilesCannotBecomeReferenceImages() {
        #expect(throws: (any Error).self) { try ReferenceImageLoader.load(URL(string: "https://example.invalid/photo.jpg")!) }
    }
}
