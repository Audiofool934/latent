import Foundation

public struct PhotoTrashItem: Decodable, Sendable {
    public let id: Int
    public let name: String
    public let remotePath: String
    public let state: String
}

public struct PhotoTrashBatch: Decodable, Identifiable, Sendable {
    public let id: String
    public let createdAt: String
    public let updatedAt: String
    public let status: String
    public let error: String?
    public let items: [PhotoTrashItem]
    public var transferring: Bool { ["queued", "moving", "restoring"].contains(status) }
    public var label: String {
        switch status {
        case "queued", "moving": "Moving to Trash"
        case "trashed": "In Trash"
        case "restoring": "Restoring photos"
        case "restored": "Restored"
        default: "Needs attention"
        }
    }
}
public struct PhotoTrashList: Decodable, Sendable { public let batches: [PhotoTrashBatch] }
public struct PhotoTrashResponse: Decodable, Sendable { public let batch: PhotoTrashBatch }

public struct PhotoTrashSelection: Identifiable, Sendable {
    public let id = UUID().uuidString.lowercased()
    public let photos: [Photo]
    public let dataID: String
    public init(photos: [Photo], dataID: String) { self.photos = photos; self.dataID = dataID }
}
