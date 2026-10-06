import Foundation
import Testing
@testable import LatentCore

@MainActor private final class FakeServiceChild: ServiceChild {
    var isRunning = true
    var pid: Int32 = 42
    var exitStatus: Int32 = 2
    var stops = 0
    var responds = false
    func requestStop() { stops += 1 }
}

private func identity(protocolVersion: Int = 1, pid: Int32 = 42, dataID: String = "fixture") -> ServiceIdentity {
    ServiceIdentity(status: "ok", service: "latent", protocolVersion: protocolVersion,
        serviceVersion: "0.1.0", instanceId: "f699d3ac-607a-4c96-9bf9-14cd7206feaa", pid: pid, dataId: dataID)
}

@MainActor @Suite(.serialized)
struct ServiceConnectionTests {
    @Test func reusesExistingServiceAndNeverStopsIt() async throws {
        var launches = 0
        let unrelated = FakeServiceChild()
        let connection = ServiceConnection(probe: { identity() }, launch: {
            launches += 1; return unrelated
        })
        try await connection.connect()
        #expect(connection.state == .connected(owned: false))
        connection.shutdown()
        #expect(launches == 0)
        #expect(unrelated.stops == 0)
    }

    @Test func concurrentRetriesCreateOneChildAndShutdownOnlyThatChild() async throws {
        var launches = 0
        let child = FakeServiceChild()
        let unrelated = FakeServiceChild()
        let connection = ServiceConnection(probe: {
            if launches == 0 { throw URLError(.cannotConnectToHost) }
            await Task.yield()
            return identity()
        }, launch: { launches += 1; return child }, pollInterval: .milliseconds(1))
        async let first: Void = connection.connect()
        async let second: Void = connection.connect()
        async let third: Void = connection.connect()
        _ = try await (first, second, third)
        #expect(launches == 1)
        #expect(connection.state == .connected(owned: true))
        connection.shutdown()
        #expect(child.stops == 1)
        #expect(unrelated.stops == 0)
    }

    @Test func wrongProtocolOrDataNeverLaunchesOrStopsAnExistingService() async {
        for response in [identity(protocolVersion: 2), identity(dataID: "other")] {
            var launches = 0
            let unrelated = FakeServiceChild()
            let connection = ServiceConnection(probe: { response }, expectedDataID: "fixture", launch: {
                launches += 1; return unrelated
            })
            await #expect(throws: ServiceConnectionError.self) { try await connection.connect() }
            #expect(launches == 0)
            #expect(unrelated.stops == 0)
            #expect(!connection.isConnecting)
            connection.shutdown()
        }
    }

    @Test func anOccupiedUnresponsivePortDoesNotAuthorizeAChild() async {
        var launches = 0
        let connection = ServiceConnection(probe: { throw URLError(.timedOut) }, launch: {
            launches += 1; return FakeServiceChild()
        }, startupTimeout: .milliseconds(20), pollInterval: .milliseconds(2))
        await #expect(throws: ServiceConnectionError.self) { try await connection.connect() }
        #expect(launches == 0)
        #expect(connection.state.message.contains("did not become ready"))
        connection.shutdown()
    }

    @Test func delayedExternalStartupWaitsWithoutSpawningAnything() async throws {
        var checks = 0
        let connection = ServiceConnection(probe: {
            checks += 1
            if checks < 4 { throw URLError(.cannotConnectToHost) }
            return identity()
        }, startupTimeout: .seconds(1), pollInterval: .milliseconds(2))
        try await connection.connect()
        #expect(checks == 4)
        #expect(connection.state == .connected(owned: false))
        connection.shutdown()
    }

    @Test func timeoutRetainsOwnershipUntilTheOldChildExits() async {
        var launches = 0
        let child = FakeServiceChild()
        let connection = ServiceConnection(probe: { throw URLError(.cannotConnectToHost) }, launch: {
            launches += 1; return child
        }, startupTimeout: .milliseconds(20), pollInterval: .milliseconds(2))
        await #expect(throws: ServiceConnectionError.self) { try await connection.connect() }
        #expect(child.stops == 1)
        await #expect(throws: ServiceConnectionError.self) { try await connection.connect() }
        #expect(launches == 1)
        child.isRunning = false
        connection.shutdown()
    }

    @Test func processCrashRequiresExplicitRetryInsteadOfAutomaticRestart() async throws {
        var launches = 0
        let child = FakeServiceChild()
        child.isRunning = false
        let connection = ServiceConnection(probe: {
            if !child.isRunning { throw URLError(.cannotConnectToHost) }
            return identity()
        }, launch: { launches += 1; child.isRunning = true; return child }, pollInterval: .milliseconds(1))
        try await connection.connect()
        child.isRunning = false
        try await Task.sleep(for: .milliseconds(2100))
        #expect(!connection.isConnected)
        #expect(connection.state.message.contains("stopped unexpectedly"))
        #expect(launches == 1)
        try await connection.connect()
        #expect(launches == 2)
        #expect(connection.isConnected)
        connection.shutdown()
    }

    @Test func retryNeverReconnectsToAChildWhoseStopWasRequested() async {
        let child = FakeServiceChild()
        var probes = 0
        var launches = 0
        let connection = ServiceConnection(probe: {
            probes += 1
            if child.responds { return identity() }
            throw URLError(.cannotConnectToHost)
        }, launch: { launches += 1; return child },
        startupTimeout: .milliseconds(20), pollInterval: .milliseconds(2))
        await #expect(throws: ServiceConnectionError.self) { try await connection.connect() }
        child.responds = true // A draining child can still answer health briefly.
        let before = probes
        await #expect(throws: ServiceConnectionError.self) { try await connection.connect() }
        #expect(probes == before)
        #expect(launches == 1)
        #expect(child.stops == 1)
        #expect(connection.state.message.contains("still stopping"))
        connection.shutdown()
    }

    @Test func shutdownDuringStartupCannotLaunchOrReconnectAfterwards() async {
        var launches = 0
        let child = FakeServiceChild()
        let connection = ServiceConnection(probe: { throw URLError(.cannotConnectToHost) }, launch: {
            launches += 1; return child
        }, pollInterval: .milliseconds(20))
        let pending = Task { try await connection.connect() }
        while launches == 0 { await Task.yield() }
        connection.shutdown()
        _ = await pending.result
        #expect(connection.state == .stopped)
        await #expect(throws: CancellationError.self) { try await connection.connect() }
        #expect(launches == 1)
    }

    @Test func aPortRaceCannotAdoptOrStopAnotherProcess() async {
        var launched = false
        let child = FakeServiceChild()
        let unrelated = FakeServiceChild()
        let connection = ServiceConnection(probe: {
            if !launched { throw URLError(.cannotConnectToHost) }
            return identity(pid: 99)
        }, launch: { launched = true; return child }, pollInterval: .milliseconds(1))
        await #expect(throws: ServiceConnectionError.self) { try await connection.connect() }
        #expect(child.stops == 1)
        #expect(unrelated.stops == 0)
        #expect(connection.state.message.contains("Another service"))
        connection.shutdown()
    }

    @Test func aRefusedProbeReturningAfterShutdownCannotSpawnAChild() async {
        var pendingProbe: CheckedContinuation<ServiceIdentity, any Error>?
        var launches = 0
        let connection = ServiceConnection(probe: {
            try await withCheckedThrowingContinuation { pendingProbe = $0 }
        }, launch: { launches += 1; return FakeServiceChild() })
        let pending = Task { try await connection.connect() }
        while pendingProbe == nil { await Task.yield() }
        connection.shutdown()
        pendingProbe?.resume(throwing: URLError(.cannotConnectToHost))
        _ = await pending.result
        #expect(launches == 0)
        #expect(connection.state == .stopped)
    }

    @Test func launchIsExplicitAndDataIdentityMatchesThePythonContract() throws {
        #expect(try ServiceLaunchConfiguration.from(arguments: ["Latent"]) == nil)
        #expect(throws: ServiceConnectionError.self) {
            try ServiceLaunchConfiguration.from(arguments: ["Latent", "--service-python", "relative-python"])
        }
        let configuration = try ServiceLaunchConfiguration.from(arguments: ["Latent",
            "--service-python", "/example/.venv/bin/python", "--service-state-dir", "/catalog",
            "--service-embedding-dir", "/embeddings", "--service-workspace-dir", "/workspace"])
        #expect(configuration?.dataID == "d974d771dd92aaaea452e3dda192b74fba294fda0ffd9bd736c0509d6161a887")
        #expect(configuration?.python.path == "/example/.venv/bin/python")
    }

    @Test func normalLaunchReadsThePackagedLibraryWithoutCommandLineArguments() throws {
        let file = FileManager.default.temporaryDirectory.appendingPathComponent("LatentLocalService-\(UUID()).json")
        defer { try? FileManager.default.removeItem(at: file) }
        try Data("""
        {"python":"/example/.venv/bin/python","stateDirectory":"/catalog",
         "embeddingDirectory":"/embeddings","workspaceDirectory":"/workspace"}
        """.utf8).write(to: file)
        let configuration = try #require(try ServiceLaunchConfiguration.from(arguments: ["Latent"],
            bundledConfigurationURL: file))
        #expect(configuration.dataID == "d974d771dd92aaaea452e3dda192b74fba294fda0ffd9bd736c0509d6161a887")
        #expect(configuration.python.path == "/example/.venv/bin/python")
        let environment = configuration.serviceEnvironment(inheriting: ["PATH": "/usr/bin:/bin", "LOCAL_SETTING": "kept"])
        #expect(environment["PATH"] == "/example/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
        #expect(environment["LOCAL_SETTING"] == "kept")
    }

    @Test func anExplicitEndpointNeverUsesTheBundledLibrary() throws {
        let missing = URL(fileURLWithPath: "/does-not-exist/LocalService.json")
        #expect(try ServiceLaunchConfiguration.from(arguments: ["Latent", "--server-url", "http://127.0.0.1:9876"],
            bundledConfigurationURL: missing) == nil)
        #expect(try ServiceLaunchConfiguration.from(arguments: ["Latent"],
            environment: ["LATENT_SERVER_URL": "http://127.0.0.1:9876"], bundledConfigurationURL: missing) == nil)
        let explicit = try ServiceLaunchConfiguration.from(arguments: ["Latent",
            "--service-python", "/custom/python", "--service-state-dir", "/custom/catalog",
            "--service-embedding-dir", "/custom/embeddings", "--service-workspace-dir", "/custom/workspace"],
            bundledConfigurationURL: missing)
        #expect(explicit?.stateDirectory.path == "/custom/catalog")
    }

    @Test func invalidPackagedPathsCannotSilentlySelectAnotherLibrary() throws {
        let file = FileManager.default.temporaryDirectory.appendingPathComponent("LatentInvalidService-\(UUID()).json")
        defer { try? FileManager.default.removeItem(at: file) }
        for contents in ["{}", """
        {"python":"relative/python","stateDirectory":"/catalog",
         "embeddingDirectory":"/embeddings","workspaceDirectory":"/workspace"}
        """] {
            try Data(contents.utf8).write(to: file)
            #expect(throws: ServiceConnectionError.self) {
                try ServiceLaunchConfiguration.from(arguments: ["Latent"], bundledConfigurationURL: file)
            }
        }
    }
}
