import Foundation

/// Flat storage and iterative traversal keep user-defined depth independent of the call stack.
public struct SequenceHierarchy: Sendable {
    public enum Item: Identifiable, Hashable, Sendable {
        case folder(SequenceFolder), sequence(PhotoSequence)
        public var id: String {
            switch self { case .folder(let value): "folder:\(value.id)"; case .sequence(let value): "sequence:\(value.id)" }
        }
        public var name: String {
            switch self { case .folder(let value): value.name; case .sequence(let value): value.name }
        }
        public var parentID: String? {
            switch self { case .folder(let value): value.parentId; case .sequence(let value): value.folderId }
        }
    }

    public struct Row: Identifiable, Sendable {
        public let item: Item
        public let depth: Int
        public var id: String { item.id }
    }

    public let folders: [String: SequenceFolder]
    private let children: [String: [Item]]
    private static let root = ""

    public init(folders: [SequenceFolder], sequences: [PhotoSequence]) {
        self.folders = Dictionary(folders.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        var children: [String: [Item]] = [:]
        for folder in folders { children[folder.parentId ?? Self.root, default: []].append(.folder(folder)) }
        for sequence in sequences { children[sequence.folderId ?? Self.root, default: []].append(.sequence(sequence)) }
        self.children = children.mapValues { items in
            items.sorted { left, right in
                if case .folder = left, case .sequence = right { return true }
                if case .sequence = left, case .folder = right { return false }
                let order = left.name.localizedStandardCompare(right.name)
                return order == .orderedSame ? left.id < right.id : order == .orderedAscending
            }
        }
    }

    public func contents(of folderID: String?) -> [Item] { children[folderID ?? Self.root] ?? [] }

    public func path(to folderID: String?) -> [SequenceFolder] {
        var path: [SequenceFolder] = []
        var visited = Set<String>()
        var current = folderID
        while let id = current, visited.insert(id).inserted, let folder = folders[id] {
            path.append(folder)
            current = folder.parentId
        }
        return path.reversed()
    }

    public func canMove(_ item: Item, to folderID: String?) -> Bool {
        guard folderID == nil || folders[folderID!] != nil else { return false }
        guard case .folder(let folder) = item else { return true }
        return !path(to: folderID).contains { $0.id == folder.id }
    }

    public func visibleRows(expanded: Set<String>, rootFolderID: String? = nil) -> [Row] {
        var result: [Row] = []
        var visited = Set<String>()
        var stack = contents(of: rootFolderID).reversed().map { Row(item: $0, depth: 0) }
        while let row = stack.popLast() {
            guard visited.insert(row.id).inserted else { continue }
            result.append(row)
            if case .folder(let folder) = row.item, expanded.contains(folder.id) {
                stack.append(contentsOf: contents(of: folder.id).reversed().map { Row(item: $0, depth: row.depth + 1) })
            }
        }
        return result
    }
}
