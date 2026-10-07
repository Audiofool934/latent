import Foundation
import Testing
@testable import LatentCore

// Captured from the Python service's /api/search-backends with a validated local fixture index.
private let enginesJSON = """
{"active":"gemini","switchable":true,"data_id":"library-1","backends":[
 {"key":"gemini","display_name":"Gemini Embedding 2","model_id":"gemini-embedding-2","dimensions":3072,
  "space_id":"gemini-embedding-2","provider":"gemini_api","local":false,"sends_previews":true,
  "image_text_queries":true,"requires_validation":false,"image_cost_usd":0.00012,"images_per_second":null,
  "active":true,"index":{"queued_assets":3,"indexed_assets":3,"stale_jobs":0,
  "jobs":{"pending":0,"running":0,"succeeded":3,"failed":0},"total_assets":3,"remaining_assets":0,
  "phase":"complete","semantic_ready":true},"available":true,"availability":null},
 {"key":"embeddinggemma","display_name":"EmbeddingGemma 2 (on this Mac)","model_id":"embeddinggemma-2",
  "dimensions":768,"space_id":"embeddinggemma-2@c5d0e65afc34799f","provider":"local_llama_cpp","local":true,
  "sends_previews":false,"image_text_queries":false,"requires_validation":true,"image_cost_usd":0.0,
  "images_per_second":1.45,"active":false,"index":{"queued_assets":3,"indexed_assets":2,"stale_jobs":0,
  "jobs":{"pending":1,"running":0,"succeeded":2,"failed":0},"total_assets":3,"remaining_assets":1,
  "phase":"paused","semantic_ready":true},"available":true,"availability":null,
  "runtime":{"state":"stopped","detail":null,"error":null,"started_at":null,"llama_server":"/opt/llama-server",
  "model_dir":"/opt/models"},"artifact":{"repository":"unsloth/embeddinggemma-2-GGUF",
  "revision":"ba3888272494be64ed88c9eb536ddc61a1be73d5"},
  "validation":{"status":"passed","id":"5219dd61-95ec-4867-af99-76ff6a60eb09","passed":true,
  "created_at":"2026-10-07T09:35:58.098745+00:00","index_revision":[2,"a","b",3072,3],
  "checks":[{"name":"vectors","passed":true,"detail":"2 vectors"},
            {"name":"self_retrieval","passed":true,"detail":"2 of 2 retrieved themselves"}]}}]}
"""

private func decoder() -> JSONDecoder {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    return decoder
}

@Test func searchEnginesDecodeTheServicePayloadAndGateActivation() throws {
    let list = try decoder().decode(SearchEngineList.self, from: Data(enginesJSON.utf8))
    #expect(list.activeEngine?.key == "gemini")
    let gemini = try #require(list.backends.first { $0.key == "gemini" })
    let local = try #require(list.backends.first { $0.key == "embeddinggemma" })
    #expect(!gemini.canActivate) // already active
    #expect(!gemini.canValidate) // Gemini needs no local validation
    #expect(local.local && !local.imageTextQueries && local.requiresValidation)
    #expect(local.coverage == "2 of 3 photos indexed")
    #expect(local.validation?.label == "Validated")
    #expect(local.validation?.failedChecks.isEmpty == true)
    #expect(local.runtime?.label == "Not loaded; starts when needed")
    #expect(local.canActivate && local.canValidate)
}

@Test func localEnginesNeedACurrentPassingValidation() throws {
    for status in ["not_run", "failed", "stale", "running", "error"] {
        let json = enginesJSON.replacingOccurrences(of: "\"status\":\"passed\"", with: "\"status\":\"\(status)\"")
        let local = try #require(try decoder().decode(SearchEngineList.self, from: Data(json.utf8))
            .backends.first { $0.key == "embeddinggemma" })
        #expect(!local.canActivate, "status \(status)")
        #expect(local.canValidate == (status != "running"), "status \(status)")
    }
    let empty = enginesJSON.replacingOccurrences(of: "\"indexed_assets\":2", with: "\"indexed_assets\":0")
    let local = try #require(try decoder().decode(SearchEngineList.self, from: Data(empty.utf8))
        .backends.first { $0.key == "embeddinggemma" })
    #expect(!local.canActivate && !local.canValidate)
    #expect(local.coverage == "No photos indexed")
}

@Test func embeddingRunsDecodeWithAndWithoutEngineFields() throws {
    let gemini = """
    {"id":"r1","created_at":"t","revision":"v","scope":"all","batch_id":null,"status":"prepared","error":null,
     "selected":10,"reused":2,"to_generate":8,"succeeded":0,"failed":0,"upload_bytes":4096,
     "estimated_cost_usd":0.00096,"model":"gemini-embedding-2"}
    """
    let old = try decoder().decode(PhotoEmbeddingRun.self, from: Data(gemini.utf8))
    #expect(!old.isLocal && old.engineName == "Gemini Embedding 2" && old.remainingSeconds == nil)
    let local = gemini
        .replacingOccurrences(of: "\"model\":\"gemini-embedding-2\"", with: """
        "model":"embeddinggemma-2","backend":"embeddinggemma","engine":"EmbeddingGemma 2 (on this Mac)",
        "local":true,"estimated_seconds":6
        """)
        .replacingOccurrences(of: "\"succeeded\":0", with: "\"succeeded\":4")
    let run = try decoder().decode(PhotoEmbeddingRun.self, from: Data(local.utf8))
    #expect(run.isLocal && run.engineName == "EmbeddingGemma 2 (on this Mac)")
    #expect(run.remainingSeconds == 3)
}

private final class EngineStub: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var bodies: [String: [String: String]] = [:]
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let url = request.url!
        if let stream = request.httpBodyStream {
            stream.open()
            var data = Data()
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                data.append(buffer, count: count)
            }
            stream.close()
            Self.bodies[url.path] = (try? JSONSerialization.jsonObject(with: data) as? [String: String]) ?? [:]
        }
        let body = url.path.hasSuffix("/validate") ? "{\"validation\":{\"status\":\"running\"}}" : enginesJSON
        let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@Test func engineActionsSendTheReviewedValidationAndLibraryIdentity() async throws {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [EngineStub.self]
    let client = try LibraryClient(baseURL: URL(string: "http://127.0.0.1:8766")!,
                                   session: URLSession(configuration: configuration))
    let list = try await client.searchEngines()
    let local = try #require(list.backends.first { $0.key == "embeddinggemma" })
    try await client.validateSearchEngine(local.key, dataID: "library-1")
    _ = try await client.activateSearchEngine(local, dataID: "library-1")
    _ = try? await client.prepareEmbeddings(id: "r", scope: "all", ids: nil, importID: nil,
                                            backend: "embeddinggemma", dataID: "library-1")
    #expect(EngineStub.bodies["/api/search-backends/embeddinggemma/validate"] == ["expected_data_id": "library-1"])
    #expect(EngineStub.bodies["/api/search-backends/activate"] == [
        "backend": "embeddinggemma", "validation_id": "5219dd61-95ec-4867-af99-76ff6a60eb09",
        "expected_data_id": "library-1",
    ])
    #expect(EngineStub.bodies["/api/embedding-runs"]?["backend"] == "embeddinggemma")
}
