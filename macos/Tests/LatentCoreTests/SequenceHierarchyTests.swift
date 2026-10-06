import Foundation
import Testing
@testable import LatentCore

@Test func foldersHaveNoFixedDepthAndMovesCannotCreateAncestorCycles() {
    let folders = (0..<1_100).map { SequenceFolder(id: "f\($0)", name: "Level \($0)", parentId: $0 == 0 ? nil : "f\($0 - 1)") }
    let hierarchy = SequenceHierarchy(folders: folders, sequences: [])
    let rows = hierarchy.visibleRows(expanded: Set(folders.map(\.id)))
    #expect(rows.count == 1_100)
    #expect(rows.last?.depth == 1_099)
    #expect(hierarchy.path(to: "f1099").count == 1_100)
    #expect(!hierarchy.canMove(.folder(folders[0]), to: "f1099"))
    #expect(!hierarchy.canMove(.folder(folders[1]), to: "f1"))
    #expect(hierarchy.canMove(.folder(folders[1_099]), to: nil))
    #expect(!hierarchy.canMove(.folder(folders[0]), to: "missing"))
    #expect(hierarchy.visibleRows(expanded: []).map(\.id) == ["folder:f0"])
}

@Test func folderTraversalSortsSiblingsAndKeepsSequenceLeavesInTheirFolder() throws {
    let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
    let response = try decoder.decode(SequencesResponse.self, from: Data("""
    {"folders":[{"id":"a","name":"Trip 2","parent_id":null},{"id":"b","name":"Trip 10","parent_id":null},
    {"id":"c","name":"Night","parent_id":"a"}],"sequences":[
    {"id":"s","name":"Night sequence","note":"","item_count":2,"folder_id":"c"},
    {"id":"root","name":"First","note":"","item_count":0}]}
    """.utf8))
    let hierarchy = SequenceHierarchy(folders: response.folders!, sequences: response.sequences)
    #expect(hierarchy.visibleRows(expanded: ["a", "c"]).map(\.id) == ["folder:a", "folder:c", "sequence:s", "folder:b", "sequence:root"])
    #expect(hierarchy.path(to: "c").map(\.name) == ["Trip 2", "Night"])
    #expect(hierarchy.contents(of: "c").map(\.id) == ["sequence:s"])
    let subtree = hierarchy.visibleRows(expanded: ["c"], rootFolderID: "a")
    #expect(subtree.map(\.id) == ["folder:c", "sequence:s"])
    #expect(subtree.map(\.depth) == [0, 1])
    #expect(hierarchy.visibleRows(expanded: [], rootFolderID: "a").map(\.id) == ["folder:c"])
}

private final class FolderMoveProtocol: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var data = request.httpBody ?? Data()
        if data.isEmpty, let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var bytes = [UInt8](repeating: 0, count: 1024)
            while stream.hasBytesAvailable {
                let count = stream.read(&bytes, maxLength: bytes.count)
                if count <= 0 { break }
                data.append(contentsOf: bytes.prefix(count))
            }
        }
        let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        let folder = request.url!.path.contains("sequence-folders")
        let valid = request.httpMethod == "PATCH" && body[folder ? "parent_id" : "folder_id"] is NSNull
            && body["expected_data_id"] as? String == "owned-library" && body.count == 2
        let response: String
        if !valid { response = "{\"message\":\"Root must be an explicit null with a data identity\"}" }
        else if folder { response = "{\"folder\":{\"id\":\"f\",\"name\":\"Folder\",\"parent_id\":null}}" }
        else { response = "{\"sequence\":{\"id\":\"s\",\"name\":\"Sequence\",\"note\":\"Keep\",\"item_count\":3,\"folder_id\":null}}" }
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: valid ? 200 : 400, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(response.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@Test func movingBackToRootEncodesNullAndDoesNotOverwriteMetadata() async throws {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [FolderMoveProtocol.self]
    let client = try LibraryClient(baseURL: URL(string: "http://localhost:8766")!, session: URLSession(configuration: configuration))
    let folder = try await client.moveSequenceFolder(id: "f", to: nil, expectedDataID: "owned-library")
    let sequence = try await client.moveSequence(id: "s", to: nil, expectedDataID: "owned-library")
    #expect(folder.parentId == nil)
    #expect(sequence.folderId == nil && sequence.note == "Keep" && sequence.itemCount == 3)
}
