import Foundation
import Testing
@testable import LatentCore

private let photoJSON = """
{"id":1,"name":"portrait.ARW","remote_path":"/Archive/portrait.ARW","size_bytes":44000,
 "capture_at":"2025:09:11 22:28:30","camera_model":"ILCE-7RM2","lens_model":null,
 "preview_width":1080,"preview_height":1616,
 "contact_url":"/media/contact/portrait.jpg","preview_url":"/media/preview/portrait.jpg",
 "preview_available":true}
"""

@Test func decodesOrientedPreviewDimensionsAndCaptureDay() throws {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    let photo = try decoder.decode(Photo.self, from: Data(photoJSON.utf8))
    #expect(photo.aspectRatio == 1080.0 / 1616)
    #expect(photo.captureDay == "2025-09-11")
    #expect(photo.lensModel == nil)
}

@Test func nativeClientOnlyAcceptsLocalServiceAndMedia() throws {
    for address in ["https://example.com", "http://example.com", "file:///tmp", "http://user:pass@localhost:8766", "http://localhost:8766/a"] {
        #expect(throws: LibraryError.self) { try LibraryClient(baseURL: URL(string: address)!) }
    }
    let client = try LibraryClient(baseURL: URL(string: "http://127.0.0.1:8766")!)
    #expect(try client.mediaURL("/media/contact/image.jpg").absoluteString == "http://127.0.0.1:8766/media/contact/image.jpg")
    for path in ["https://example.com/image.jpg", "//example.com/image.jpg", "/media/../private", "/api/search"] {
        #expect(throws: LibraryError.self) { try client.mediaURL(path) }
    }
}

private final class StubProtocol: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let url = request.url!
        let body: String
        if url.path == "/api/search" {
            let parts = URLComponents(url: url, resolvingAgainstBaseURL: false)!
            let values = Dictionary(uniqueKeysWithValues: parts.queryItems!.map { ($0.name, $0.value!) })
            guard values["q"] == "雪山 & blue", values["variety"] == "1", values["limit"] == "100" else {
                client?.urlProtocol(self, didFailWithError: URLError(.badURL)); return
            }
            body = "{\"total\":1,\"results\":[\(photoJSON)]}"
        } else if url.path == "/api/assets" {
            body = "{\"total\":2656,\"has_more\":true,\"next_offset\":250,\"assets\":[\(photoJSON)]}"
        } else if url.path == "/api/sequences/test-sequence/items/order", request.httpMethod == "PUT" {
            body = "{\"sequence\":{\"id\":\"test-sequence\",\"name\":\"Study\",\"note\":\"\",\"item_count\":0,\"items\":[]}}"
        } else {
            let response = HTTPURLResponse(url: url, statusCode: 503, httpVersion: nil, headerFields: nil)!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data("{\"message\":\"Library unavailable\"}".utf8))
            client?.urlProtocolDidFinishLoading(self)
            return
        }
        let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

private func stubClient() throws -> LibraryClient {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [StubProtocol.self]
    return try LibraryClient(baseURL: URL(string: "http://127.0.0.1:8766")!, session: URLSession(configuration: configuration))
}

@Test func searchEncodesUnicodeAndKeepsVarietyExplicit() async throws {
    let result = try await stubClient().search("雪山 & blue", variety: true)
    #expect(result.results.count == 1)
    #expect(result.results.first?.name == "portrait.ARW")
}

@Test func pagingPreservesTotalAndNextOffset() async throws {
    let page = try await stubClient().photos(date: "2025-12-13")
    #expect(page.total == 2656)
    #expect(page.hasMore)
    #expect(page.nextOffset == 250)
}

@Test func sequenceReorderingUsesWorkspaceContract() async throws {
    let result = try await stubClient().reorder(sequenceID: "test-sequence", itemIDs: [])
    #expect(result.name == "Study")
}

@Test func serviceErrorsAreReadable() async throws {
    do {
        _ = try await stubClient().library()
        Issue.record("Expected an unavailable-service error")
    } catch {
        #expect(error.localizedDescription == "Library unavailable")
    }
}
