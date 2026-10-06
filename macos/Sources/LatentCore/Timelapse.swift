import Foundation

public struct TimelapseBadge: Codable, Hashable, Sendable {
    public let id: String
    public let photoCount: Int
}

public struct TimelapseGroup: Decodable, Identifiable, Sendable {
    public let id: String
    public let confirmed: Bool
    public let revision: Int
    public let photoCount: Int
    public let availableCount: Int
    public let frameCount: Int
    public let startCaptureAt: String
    public let endCaptureAt: String
    public let firstFile: String
    public let lastFile: String
    public let cameraModel: String
    public let lensModel: String
    public let intervalSeconds: Double
    public let durationSeconds: Double
    public let warnings: [String]
    public let cover: Photo?

    public var day: String { String(startCaptureAt.prefix(10)).replacingOccurrences(of: ":", with: "-") }
    public var timeRange: String { String(startCaptureAt.suffix(8)) + " - " + String(endCaptureAt.suffix(8)) }
}

public struct TimelapseResponse: Decodable, Sendable {
    public let groups: [TimelapseGroup]
}
