import Foundation

public enum PhotoFlag: String, Codable, Sendable { case unmarked, pick, reject }

public struct Photo: Codable, Identifiable, Hashable, Sendable {
    public let id: Int
    public let name: String
    public let remotePath: String
    public let provider: String?
    public let fingerprint: String?
    public let sizeBytes: Int64
    public let captureAt: String?
    public let cameraModel: String?
    public let lensModel: String?
    public let previewWidth: Int?
    public let previewHeight: Int?
    public let contactUrl: String
    public let previewUrl: String
    public let previewAvailable: Bool
    public var rating: Int?
    public var caption: String?
    public var flag: PhotoFlag?
    public var similarity: Double?
    public var timelapse: TimelapseBadge?
    public var exif: PhotoEXIF?

    /// The cached preview has already had EXIF orientation applied.
    public var aspectRatio: Double {
        guard let width = previewWidth, let height = previewHeight,
              width > 0, height > 0 else { return 1 }
        return Double(width) / Double(height)
    }

    public var captureDay: String? {
        guard let captureAt, captureAt.count >= 10 else { return nil }
        return String(captureAt.prefix(10)).replacingOccurrences(of: ":", with: "-")
    }
}

public struct PhotoEXIF: Codable, Hashable, Sendable {
    public var exposureTime: Double?
    public var fNumber: Double?
    public var iso: Double?
    public var focalLength: Double?
    public var imageWidth: Double?
    public var imageHeight: Double?

    public var shutter: String? {
        guard let exposureTime, exposureTime.isFinite, exposureTime > 0 else { return nil }
        if exposureTime <= 0.5 {
            let reciprocal = 1 / exposureTime
            let denominator = reciprocal.rounded()
            if denominator.isFinite, abs(denominator - reciprocal) <= reciprocal * 0.01 {
                return String(format: "1/%.0f s", denominator)
            }
        }
        return String(format: "%g s", exposureTime)
    }
    public var aperture: String? { fNumber.map { String(format: "ƒ/%g", $0) } }
    public var sensitivity: String? { iso.map { String(format: "ISO %g", $0) } }
    public var focal: String? { focalLength.map { String(format: "%g mm", $0) } }
    public var dimensions: String? {
        guard let imageWidth, let imageHeight else { return nil }
        return String(format: "%.0f × %.0f", imageWidth, imageHeight)
    }
    public var summary: String { [shutter, aperture, sensitivity].compactMap { $0 }.joined(separator: " · ") }
}

public struct CaptureDay: Decodable, Identifiable, Hashable, Sendable {
    public let captureDate: String
    public let assetCount: Int
    public var id: String { captureDate }
    public var year: String { String(captureDate.prefix(4)) }
    public var month: String { String(captureDate.prefix(7)) }
}

public struct LibrarySummary: Decodable, Sendable {
    public let dates: [CaptureDay]
    public let cachedAssets: Int
    public let workspace: WorkspaceSummary
    public let embeddingIndex: SearchIndexSummary?
    public var searchPlaceholder: String {
        embeddingIndex?.phase == "complete" ? "Search all photos" : "Search indexed photos"
    }
}

public struct SearchIndexSummary: Decodable, Sendable {
    public let phase: String
}

public struct WorkspaceSummary: Decodable, Sendable {
    public let writable: Bool
}

public struct PhotoPage: Decodable, Sendable {
    public let total: Int
    public let hasMore: Bool
    public let nextOffset: Int?
    public let assets: [Photo]
    public let expandedTotal: Int?
}

public struct SearchResults: Decodable, Sendable {
    public let total: Int
    public let results: [Photo]
    public let historyId: String?
    public let queryCached: Bool?
}

public struct SearchHistoryEntry: Decodable, Hashable, Identifiable, Sendable {
    public let id: String
    public let kind: String
    public let query: String
    public let imageName: String?
    public let filters: PhotoFilters
    public let order: PhotoSearchOrder
    public let usedAt: String
    public let reusable: Bool
    public var title: String { query.isEmpty ? (imageName ?? "Image search") : query }
}

public struct PhotoSequence: Decodable, Identifiable, Hashable, Sendable {
    public let id: String
    public let name: String
    public let note: String
    public let itemCount: Int
    public let items: [SequenceItem]?
    public let folderId: String?
    public let smartFilters: PhotoFilters?
    public let nextOffset: Int?
}

public struct SequenceFolder: Decodable, Identifiable, Hashable, Sendable {
    public let id: String
    public let name: String
    public let parentId: String?

    public init(id: String, name: String, parentId: String?) {
        self.id = id; self.name = name; self.parentId = parentId
    }
}

public struct SequenceItem: Decodable, Identifiable, Hashable, Sendable {
    public let id: String
    public let name: String
    public let libraryStatus: String?
    public let asset: Photo?
}

public struct SequencesResponse: Decodable, Sendable {
    public let sequences: [PhotoSequence]
    public let folders: [SequenceFolder]?
}

public struct SequenceFolderResponse: Decodable, Sendable { public let folder: SequenceFolder }

public struct SequenceResponse: Decodable, Sendable {
    public let sequence: PhotoSequence
    public let added: Int?
    public let skipped: Int?
}

public enum PhotoSearchOrder: String, CaseIterable, Codable, Hashable, Sendable {
    case closest, leastSimilar = "least_similar", variety
    public var label: String {
        switch self {
        case .closest: "Most similar"
        case .leastSimilar: "Least similar"
        case .variety: "More variety"
        }
    }
}

public struct SearchReference: Hashable, Identifiable, Sendable {
    public let id = UUID()
    public let name: String
    public let jpeg: Data
    public init(name: String, jpeg: Data) { self.name = name; self.jpeg = jpeg }
}

public enum GallerySource: Hashable, Sendable {
    case library(date: String?)
    case starred
    case search(query: String, order: PhotoSearchOrder)
    case imageSearch(reference: SearchReference, query: String, order: PhotoSearchOrder)
    case similar(id: Int, name: String, order: PhotoSearchOrder = .closest)
    case sequence(id: String)
    case timelapse(id: String)
    case importBatch(id: String, name: String)
}


public struct AnnotationResponse: Decodable, Sendable { public let assets: [Photo] }

public struct AnnotationTarget: Codable, Equatable, Sendable {
    public let id: Int
    public let provider: String
    public let remotePath: String
    public let fingerprint: String
    enum CodingKeys: String, CodingKey { case id, provider, remotePath = "remote_path", fingerprint }

    public init(photo: Photo) throws {
        guard let provider = photo.provider, let fingerprint = photo.fingerprint,
              !provider.isEmpty, !fingerprint.isEmpty else {
            throw ServiceConnectionError("This service does not provide photo identities for safe saves. Update the service and refresh the library, then retry the edit.")
        }
        id = photo.id
        self.provider = provider
        remotePath = photo.remotePath
        self.fingerprint = fingerprint
    }

    public var isValid: Bool { id > 0 && !provider.isEmpty && !remotePath.isEmpty && !fingerprint.isEmpty }
    public func matches(_ photo: Photo) -> Bool {
        id == photo.id && provider == photo.provider && remotePath == photo.remotePath && fingerprint == photo.fingerprint
    }
}

public struct AnnotationExpectation: Encodable, Sendable {
    public let dataID: String
    public let assets: [AnnotationTarget]
    enum CodingKeys: String, CodingKey { case dataID = "data_id", assets }
    public init(dataID: String, assets: [AnnotationTarget]) { self.dataID = dataID; self.assets = assets }
}

public struct EditingResponse: Decodable, Sendable { public let batch: EditingBatch }
public struct EditingList: Decodable, Sendable { public let batches: [EditingBatch] }
public struct EditingBatch: Decodable, Identifiable, Sendable {
    public let id: String
    public let createdAt: String
    public let updatedAt: String?
    public let status: String
    public let error: String?
    public let folder: String
    public let items: [EditingItem]
    public let uploads: [EditingUpload]
    public let bytesDone: Int64
    public let bytesTotal: Int64
    public let uploadDestination: String?
    public let cleanupPolicy: String?
    public let phase: String
    public let outputMode: String?
    public var usesFolderOutput: Bool { outputMode == "filesystem" }
    public var transferring: Bool { ["queued", "downloading", "uploading", "verifying", "cleaning", "copying"].contains(status) }
    public var label: String {
        switch status {
        case "queued", "downloading": "Preparing working copies"
        case "ready": "Ready to edit"
        case "prepared": usesFolderOutput ? "Review finished photos" : "Ready to upload"
        case "copying": "Saving finished photos"
        case "saved": "Saved to output folder"
        case "uploading": "Uploading edits"
        case "verifying": "Verifying cloud copies"
        case "uploaded", "cloud_verified": "Cloud copies verified"
        case "waiting_cloud": "Waiting for CloudDrive"
        case "cleaning": "Cleaning working copies"
        case "complete": "Complete"
        default: "Needs attention"
        }
    }
}
public struct EditingItem: Decodable, Sendable {
    public let assetId: Int
    public let name: String
    public let localPath: String
    public let downloaded: Bool
    public let archiveRelative: String?
}
public struct EditingUpload: Decodable, Sendable {
    public let localPath: String
    public let remotePath: String
    public let sizeBytes: Int64
    public let verified: Bool
    public let assetId: Int?
}

public struct ExportSelection: Encodable, Sendable {
    public let local_path: String
    public let asset_id: Int
    public init(path: String, assetID: Int) { local_path = path; asset_id = assetID }
}

public struct FolderLocation: Decodable, Sendable {
    public let path: String
}

public struct LibrarySource: Decodable, Identifiable, Sendable {
    public let id: String
    public let name: String
    public let logicalRoot: String
    public let folder: FolderLocation?
    public let legacy: Bool
}

public struct FolderIndexing: Decodable, Sendable {
    public let status: String
    public let discovered: Int?
    public let indexed: Int?
    public let failed: Int?
    public let error: String?
    public let currentPath: String?
}

public struct SequenceLinkResult: Decodable, Sendable {
    public let folder: String
    public let links: Int
    public let sequences: Int
}

public struct LibraryLocations: Decodable, Sendable {
    public let revision: String
    public let dataId: String
    public let sources: [LibrarySource]
    public let activeSourceId: String?
    public let output: FolderLocation?
    public let sequenceFolder: FolderLocation?
    public let indexing: FolderIndexing
    public let sequenceResult: SequenceLinkResult?
    public var activeSource: LibrarySource? { sources.first { $0.id == activeSourceId } }
}
