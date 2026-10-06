import CryptoKit
import Darwin
import Foundation
import Observation

public struct ServiceIdentity: Codable, Equatable, Sendable {
    public let status: String
    public let service: String
    public let protocolVersion: Int
    public let serviceVersion: String
    public let instanceId: String
    public let pid: Int32
    public let dataId: String

    public func validate() throws {
        guard service == "latent", status == "ok", UUID(uuidString: instanceId) != nil, pid > 0 else {
            throw ServiceConnectionError("This address is not a ready Latent service. Check the port and retry.")
        }
        guard protocolVersion == 1 else {
            throw ServiceConnectionError("The app and library service use incompatible versions. Update the service, then retry; it has not been stopped.")
        }
    }
}

public struct ServiceConnectionError: LocalizedError, Sendable {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var errorDescription: String? { message }
}

public struct ServiceLaunchConfiguration: Sendable {
    public let python: URL
    public let stateDirectory: URL
    public let embeddingDirectory: URL
    public let workspaceDirectory: URL

    public init(python: URL, stateDirectory: URL, embeddingDirectory: URL, workspaceDirectory: URL) throws {
        let paths = [python, stateDirectory, embeddingDirectory, workspaceDirectory]
        guard paths.allSatisfy({ $0.isFileURL && $0.path.hasPrefix("/") }) else {
            throw ServiceConnectionError("Managed service paths must be absolute local paths.")
        }
        // Keep the venv entry point: resolving its symlink can bypass pyvenv.cfg.
        self.python = python.standardizedFileURL
        self.stateDirectory = Self.canonicalDirectory(stateDirectory)
        self.embeddingDirectory = Self.canonicalDirectory(embeddingDirectory)
        self.workspaceDirectory = Self.canonicalDirectory(workspaceDirectory)
    }

    public var dataID: String {
        let paths = [stateDirectory, embeddingDirectory, workspaceDirectory].map(\.path).joined(separator: "\0")
        return SHA256.hash(data: Data(paths.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    private static func canonicalDirectory(_ directory: URL) -> URL {
        // Foundation hides /private in some macOS paths. Use POSIX realpath on the
        // existing ancestor to match Python Path.resolve(), including new directories.
        var ancestor = directory.standardizedFileURL
        var suffix: [String] = []
        while !FileManager.default.fileExists(atPath: ancestor.path), ancestor.path != "/" {
            suffix.append(ancestor.lastPathComponent)
            ancestor.deleteLastPathComponent()
        }
        if let resolved = realpath(ancestor.path, nil) {
            defer { free(resolved) }
            ancestor = URL(fileURLWithPath: String(cString: resolved))
        }
        return suffix.reversed().reduce(ancestor) { $0.appendingPathComponent($1) }
    }

    public static func from(arguments: [String], environment: [String: String] = [:],
                            bundledConfigurationURL: URL? = nil) throws -> Self? {
        let flags = ["--service-python", "--service-state-dir", "--service-embedding-dir", "--service-workspace-dir"]
        guard flags.contains(where: arguments.contains) else {
            // An explicitly selected endpoint keeps the external-service contract.
            // Finder and Dock launches instead use the local build's runtime and library.
            guard !arguments.contains("--server-url"), environment["LATENT_SERVER_URL"] == nil,
                  let bundledConfigurationURL else { return nil }
            struct LocalService: Decodable {
                let python: String
                let stateDirectory: String
                let embeddingDirectory: String
                let workspaceDirectory: String
            }
            do {
                let local = try JSONDecoder().decode(LocalService.self,
                    from: Data(contentsOf: bundledConfigurationURL))
                let paths = [local.python, local.stateDirectory, local.embeddingDirectory, local.workspaceDirectory]
                return try from(arguments: ["Latent"] + zip(flags, paths).flatMap { [$0.0, $0.1] })
            } catch {
                throw ServiceConnectionError("The app's local library configuration is invalid. Rebuild Latent to restore it. \(error.localizedDescription)")
            }
        }
        let paths = try flags.map { flag in
            guard let index = arguments.firstIndex(of: flag), arguments.indices.contains(index + 1),
                  arguments[index + 1].hasPrefix("/") else {
                throw ServiceConnectionError("Explicit managed mode requires \(flag) with an absolute path.")
            }
            return URL(fileURLWithPath: arguments[index + 1])
        }
        return try Self(python: paths[0], stateDirectory: paths[1], embeddingDirectory: paths[2], workspaceDirectory: paths[3])
    }

    func serviceEnvironment(inheriting environment: [String: String]) -> [String: String] {
        var result = environment
        // Finder's PATH omits Homebrew, where the local build's ExifTool lives.
        let paths = [python.deletingLastPathComponent().path, "/opt/homebrew/bin", "/usr/local/bin",
                     environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"]
        result["PATH"] = paths.joined(separator: ":")
        return result
    }
}

@MainActor protocol ServiceChild: AnyObject {
    var isRunning: Bool { get }
    var pid: Int32 { get }
    var exitStatus: Int32 { get }
    func requestStop()
}

/// EOF is the shutdown signal. A reused service never receives this private pipe.
@MainActor private final class OwnedServiceChild: ServiceChild {
    private let process = Process()
    private let input = Pipe()
    var isRunning: Bool { process.isRunning }
    var pid: Int32 { process.processIdentifier }
    var exitStatus: Int32 { process.terminationStatus }

    init(configuration: ServiceLaunchConfiguration, endpoint: URL) throws {
        guard endpoint.host == "127.0.0.1", let port = endpoint.port, port > 0 else {
            throw ServiceConnectionError("Managed mode needs an explicit http://127.0.0.1:<port> address.")
        }
        guard FileManager.default.isExecutableFile(atPath: configuration.python.path) else {
            throw ServiceConnectionError("Latent's local Python runtime is missing. Rebuild the app after restoring its project environment.")
        }
        process.executableURL = configuration.python
        process.environment = configuration.serviceEnvironment(inheriting: ProcessInfo.processInfo.environment)
        process.arguments = ["-m", "latent", "serve", "--host", "127.0.0.1", "--port", String(port),
            "--state-dir", configuration.stateDirectory.path,
            "--embedding-dir", configuration.embeddingDirectory.path,
            "--workspace-dir", configuration.workspaceDirectory.path,
            "--ready-json", "--exit-on-stdin-close"]
        process.standardInput = input
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
    }

    func requestStop() { try? input.fileHandleForWriting.close() }
}

@MainActor @Observable public final class ServiceConnection {
    public enum State: Equatable, Sendable {
        case idle, checking, starting, stopping, connected(owned: Bool), failed(String), stopped
        public var message: String {
            switch self {
            case .idle: "Library service is not connected."
            case .checking: "Connecting to the local library…"
            case .starting: "Starting the local library…"
            case .stopping: "Waiting for the previous library service to stop…"
            case .connected(let owned): owned ? "Connected to the app's library service." : "Connected to the existing library service."
            case .failed(let message): message
            case .stopped: "Library connection closed."
            }
        }
    }

    public private(set) var state: State = .idle
    public private(set) var identity: ServiceIdentity?
    public var isConnected: Bool { if case .connected = state { true } else { false } }
    public var isConnecting: Bool { state == .checking || state == .starting || state == .stopping }
    @ObservationIgnored private let probe: @MainActor () async throws -> ServiceIdentity
    @ObservationIgnored private let launch: (@MainActor () throws -> any ServiceChild)?
    @ObservationIgnored private let expectedDataID: String?
    @ObservationIgnored private let startupTimeout: Duration
    @ObservationIgnored private let pollInterval: Duration
    @ObservationIgnored private var child: (any ServiceChild)?
    @ObservationIgnored private var attempt: Task<Void, Error>?
    @ObservationIgnored private var monitor: Task<Void, Never>?
    @ObservationIgnored private var stopped = false
    @ObservationIgnored private var childStopRequested = false

    public convenience init(client: LibraryClient, configuration: ServiceLaunchConfiguration? = nil) {
        let factory: (@MainActor () throws -> any ServiceChild)?
        if let configuration {
            factory = { try OwnedServiceChild(configuration: configuration, endpoint: client.baseURL) }
        } else { factory = nil }
        self.init(probe: { try await client.serviceIdentity() }, expectedDataID: configuration?.dataID, launch: factory)
    }

    init(probe: @escaping @MainActor () async throws -> ServiceIdentity, expectedDataID: String? = nil,
         launch: (@MainActor () throws -> any ServiceChild)? = nil,
         startupTimeout: Duration = .seconds(8), pollInterval: Duration = .milliseconds(250)) {
        self.probe = probe
        self.launch = launch
        self.expectedDataID = expectedDataID
        self.startupTimeout = startupTimeout
        self.pollInterval = pollInterval
    }

    public func connect() async throws {
        guard !stopped else { throw CancellationError() }
        if let attempt { return try await attempt.value }
        if isConnected { return }
        monitor?.cancel()
        let task = Task { try await establish() }
        attempt = task
        defer { attempt = nil }
        do { try await task.value }
        catch {
            stopChild()
            identity = nil
            if !stopped { state = .failed(error.localizedDescription) }
            throw error
        }
    }

    private func checkedIdentity() async throws -> ServiceIdentity {
        let identity = try await probe()
        try Task.checkCancellation()
        try identity.validate()
        if let expectedDataID, identity.dataId != expectedDataID {
            throw ServiceConnectionError("This service uses a different catalog or workspace. Check the service address and paths; no new service was started.")
        }
        return identity
    }

    private func establish() async throws {
        try Task.checkCancellation()
        if childStopRequested, let child, child.isRunning {
            state = .stopping
            let deadline = ContinuousClock.now.advanced(by: startupTimeout)
            while child.isRunning {
                try Task.checkCancellation()
                guard ContinuousClock.now < deadline else {
                    throw ServiceConnectionError("The previous app-owned service is still stopping. Retry shortly; a duplicate service has not been started.")
                }
                try await Task.sleep(for: pollInterval)
            }
        }
        try Task.checkCancellation()
        state = .checking
        if let child, !child.isRunning { self.child = nil; childStopRequested = false }
        let deadline = ContinuousClock.now.advanced(by: startupTimeout)
        var launched = child != nil
        while true {
            try Task.checkCancellation()
            if let child, !child.isRunning {
                throw ServiceConnectionError("The library service exited (status \(child.exitStatus)). Check the Python runtime, workspace, and port, then retry.")
            }
            do {
                let identity = try await checkedIdentity()
                if let child, identity.pid != child.pid {
                    throw ServiceConnectionError("Another service reached this port during startup. Retry to verify it; the existing service has not been stopped.")
                }
                self.identity = identity
                state = .connected(owned: child != nil)
                beginMonitoring(identity)
                return
            } catch let error as URLError where error.code == .cannotConnectToHost {
                try Task.checkCancellation()
                if !launched, let launch {
                    child = try launch()
                    launched = true
                    state = .starting
                }
                // Refused connections may mean an external service is still starting.
            } catch let error as URLError where [.timedOut, .networkConnectionLost].contains(error.code) {
                // An occupied, unresponsive port is never permission to launch a child.
            }
            guard ContinuousClock.now < deadline else {
                throw ServiceConnectionError("The local library did not become ready in time. Check that the service is running at the selected address, then retry.")
            }
            try await Task.sleep(for: pollInterval)
        }
    }

    private func beginMonitoring(_ expected: ServiceIdentity) {
        monitor = Task { [weak self] in
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(2)) } catch { return }
                guard let self, !stopped else { return }
                do {
                    if let child, !child.isRunning {
                        throw ServiceConnectionError("The library service stopped unexpectedly. Retry to reconnect or start a replacement.")
                    }
                    let current = try await checkedIdentity()
                    guard current.instanceId == expected.instanceId else {
                        throw ServiceConnectionError("The service at this address changed. Retry to verify the connection.")
                    }
                } catch {
                    guard !Task.isCancelled, !stopped else { return }
                    stopChild()
                    identity = nil
                    state = .failed("Library disconnected. \(error.localizedDescription)")
                    return
                }
            }
        }
    }

    public func shutdown() {
        stopped = true
        attempt?.cancel()
        monitor?.cancel()
        stopChild()
        identity = nil
        state = .stopped
    }

    private func stopChild() {
        guard let child, child.isRunning, !childStopRequested else { return }
        childStopRequested = true
        child.requestStop()
    }
}
