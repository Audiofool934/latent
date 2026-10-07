import Foundation

public struct ImportSource: Decodable, Sendable {
    public let id: String
    public let name: String
    public let label: String
    public let folder: FolderLocation
}

public struct ImportDestination: Decodable, Sendable {
    public let path: String
    public let cloud: ImportCloudDestination?
}

public struct ImportCloudDestination: Decodable, Sendable {
    public let remotePath: String
}

public struct ImportFailure: Decodable, Sendable {
    public let name: String
    public let message: String
}

public struct PhotoImport: Decodable, Identifiable, Sendable {
    public let id: String
    public let createdAt: String
    public let revision: String
    public let status: String
    public let phase: String
    public let sources: [ImportSource]
    public let destination: ImportDestination?
    public let error: String?
    public let currentPath: String
    public let fileCount: Int
    public let photoCount: Int
    public let sidecarCount: Int
    public let otherCount: Int
    public let ignored: Int
    public let bytesTotal: Int64
    public let previewReady: Int
    public let previewFailed: Int
    public let archived: Int
    public let copied: Int
    public let skipped: Int
    public let archivedBytes: Int64
    public let undated: Int
    public let failures: [ImportFailure]
    public let extensions: [String: Int]

    public var title: String { sources.map(\.name).joined(separator: " + ") }
    public var active: Bool { ["indexing", "archiving", "verifying"].contains(status) }
    public var previewsFinished: Bool { previewReady + previewFailed + skipped >= photoCount }
    public var label: String {
        switch status {
        case "prepared": "Ready to import"
        case "indexing": "Building previews"
        case "ready": "Ready to browse"
        case "archiving": "Copying originals"
        case "verifying": "Confirming cloud copy"
        case "waiting_for_cloud": "Waiting for cloud confirmation"
        case "archived": destination?.cloud == nil ? "Archive verified" : "Cloud archive verified"
        case "paused": "Paused"
        case "interrupted": "Interrupted"
        default: "Needs attention"
        }
    }
}

public struct ImportList: Decodable, Sendable {
    public let batches: [PhotoImport]
    public let dataId: String
}
public struct ImportResponse: Decodable, Sendable {
    public let batch: PhotoImport
    public let dataId: String
}

public struct PhotoEmbeddingRun: Decodable, Identifiable, Sendable {
    public let id: String
    public let createdAt: String
    public let revision: String
    public let scope: String
    public let batchId: String?
    public let status: String
    public let error: String?
    public let selected: Int
    public let reused: Int
    public let toGenerate: Int
    public let succeeded: Int
    public let failed: Int
    public let uploadBytes: Int64
    public let estimatedCostUsd: Double
    public let model: String
    public let backend: String?
    public let engine: String?
    public let local: Bool?
    public let estimatedSeconds: Int?
    public var remaining: Int { max(0, toGenerate - succeeded) }
    public var isLocal: Bool { local ?? false }
    public var engineName: String { engine ?? "Gemini Embedding 2" }
    public var remainingSeconds: Int? {
        guard let estimatedSeconds, toGenerate > 0 else { return nil }
        return Int((Double(estimatedSeconds) * Double(remaining) / Double(toGenerate)).rounded(.up))
    }
    public var remainingCost: Double { toGenerate == 0 ? 0 : estimatedCostUsd * Double(remaining) / Double(toGenerate) }
    public var title: String {
        switch scope {
        case "all": "All photos"
        case "incremental": "Photos without embeddings"
        case "import": "Import batch"
        default: "Selected photos"
        }
    }
    public var label: String {
        switch status {
        case "prepared": "Awaiting your confirmation"
        case "running": "Generating embeddings"
        case "complete": "Ready for AI search"
        case "paused": "Paused"
        case "interrupted": "Interrupted"
        default: "Needs attention"
        }
    }
}

public struct EmbeddingRunList: Decodable, Sendable {
    public let runs: [PhotoEmbeddingRun]
    public let dataId: String
}
public struct EmbeddingRunResponse: Decodable, Sendable {
    public let run: PhotoEmbeddingRun
    public let dataId: String
}
