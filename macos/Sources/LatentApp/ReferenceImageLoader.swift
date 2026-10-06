import Foundation
import ImageIO
import LatentCore
import UniformTypeIdentifiers

/// Decode phone formats with ImageIO, apply orientation, and send only a small JPEG without source metadata.
enum ReferenceImageLoader {
    static func load(_ url: URL) throws -> SearchReference {
        guard url.isFileURL else { throw ServiceConnectionError("Choose an image saved on this Mac.") }
        let access = url.startAccessingSecurityScopedResource()
        defer { if access { url.stopAccessingSecurityScopedResource() } }
        let values = try url.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey])
        guard values.isRegularFile == true, (values.fileSize ?? 0) <= 100 * 1024 * 1024,
              let source = CGImageSourceCreateWithURL(url as CFURL, [kCGImageSourceShouldCache: false] as CFDictionary),
              let image = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true,
                kCGImageSourceThumbnailMaxPixelSize: 512,
                kCGImageSourceShouldCacheImmediately: true,
              ] as CFDictionary) else {
            throw ServiceConnectionError("Choose a readable image under 100 MB, such as JPEG, HEIC, PNG, or TIFF.")
        }
        let data = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(data, UTType.jpeg.identifier as CFString, 1, nil) else {
            throw ServiceConnectionError("The image could not be prepared for search.")
        }
        CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: 0.86] as CFDictionary)
        guard CGImageDestinationFinalize(destination), data.length <= 1024 * 1024 else {
            throw ServiceConnectionError("The image could not be prepared for search.")
        }
        return SearchReference(name: url.lastPathComponent, jpeg: data as Data)
    }
}
