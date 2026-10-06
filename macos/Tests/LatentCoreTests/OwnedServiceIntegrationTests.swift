import Darwin
import Foundation
import Testing
@testable import LatentCore

private func unusedLoopbackPort() throws -> UInt16 {
    let descriptor = socket(AF_INET, SOCK_STREAM, 0)
    guard descriptor >= 0 else { throw URLError(.cannotCreateFile) }
    defer { close(descriptor) }
    var address = sockaddr_in()
    address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    address.sin_family = sa_family_t(AF_INET)
    address.sin_addr.s_addr = inet_addr("127.0.0.1")
    var size = socklen_t(MemoryLayout<sockaddr_in>.size)
    let result = withUnsafeMutablePointer(to: &address) { pointer in
        pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            guard bind(descriptor, $0, size) == 0 else { return Int32(-1) }
            return getsockname(descriptor, $0, &size)
        }
    }
    guard result == 0 else { throw URLError(.cannotConnectToHost) }
    return UInt16(bigEndian: address.sin_port)
}

@MainActor private func waitForServiceExit(_ client: LibraryClient) async throws {
    for _ in 0..<100 {
        do { _ = try await client.serviceIdentity() }
        catch let error as URLError where error.code == .cannotConnectToHost { return }
        try await Task.sleep(for: .milliseconds(40))
    }
    throw ServiceConnectionError("The test-owned service did not exit after its pipe closed.")
}

/// Explicit opt-in: set LATENT_TEST_PYTHON and PYTHONPATH to the isolated source.
@MainActor @Test(.enabled(if: ProcessInfo.processInfo.environment["LATENT_TEST_PYTHON"] != nil))
func nativeOwnerStartsReusesAndStopsARealIsolatedPythonService() async throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent("LatentOwnedService-\(UUID().uuidString)")
    let client = try LibraryClient(baseURL: URL(string: "http://127.0.0.1:\(try unusedLoopbackPort())")!)
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    let bundledConfiguration = root.appendingPathComponent("LocalService.json")
    try JSONSerialization.data(withJSONObject: [
        "python": ProcessInfo.processInfo.environment["LATENT_TEST_PYTHON"]!,
        "stateDirectory": root.appendingPathComponent("state").path,
        "embeddingDirectory": root.appendingPathComponent("embeddings").path,
        "workspaceDirectory": root.appendingPathComponent("workspace").path,
    ]).write(to: bundledConfiguration)
    let configuration = try #require(try ServiceLaunchConfiguration.from(arguments: ["Latent"],
        bundledConfigurationURL: bundledConfiguration))
    let owner = ServiceConnection(client: client, configuration: configuration)
    let visitor = ServiceConnection(client: client, configuration: configuration)
    let reopened = ServiceConnection(client: client, configuration: configuration)
    do {
        try await owner.connect()
        #expect(owner.state == .connected(owned: true))
        let first = try await client.serviceIdentity()
        #expect(first.dataId == configuration.dataID)
        try await visitor.connect()
        #expect(visitor.state == .connected(owned: false))
        visitor.shutdown()
        #expect(try await client.serviceIdentity().instanceId == first.instanceId)
        owner.shutdown()
        try await waitForServiceExit(client)
        try await reopened.connect()
        #expect(reopened.state == .connected(owned: true))
        let second = try await client.serviceIdentity()
        #expect(second.instanceId != first.instanceId)
        #expect(second.dataId == first.dataId)
        reopened.shutdown()
        try await waitForServiceExit(client)
        try FileManager.default.removeItem(at: root)
    } catch {
        visitor.shutdown()
        owner.shutdown()
        reopened.shutdown()
        try? await waitForServiceExit(client)
        throw error
    }
}
