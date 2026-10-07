import Foundation

/// Embedding engines the service can search with. Each keeps its own index and vector space.
public struct SearchEngineList: Decodable, Sendable {
    public let active: String
    public let switchable: Bool
    public let backends: [SearchEngine]
    public let dataId: String

    public var activeEngine: SearchEngine? { backends.first { $0.key == active } }
}

public struct SearchEngine: Decodable, Identifiable, Sendable {
    public let key: String
    public let displayName: String
    public let modelId: String
    public let dimensions: Int
    public let local: Bool
    public let imageTextQueries: Bool
    public let requiresValidation: Bool
    public let imagesPerSecond: Double?
    public let active: Bool
    public let available: Bool
    public let availability: String?
    public let index: SearchEngineIndex
    public let runtime: SearchEngineRuntime?
    public let validation: SearchEngineValidation?
    public var id: String { key }

    public var coverage: String {
        index.indexedAssets == 0
            ? "No photos indexed"
            : "\(index.indexedAssets.formatted()) of \(index.totalAssets.formatted()) photos indexed"
    }

    /// Activation needs vectors and, for engines that require it, a passing current validation.
    public var canActivate: Bool {
        guard !active, index.indexedAssets > 0 else { return false }
        return !requiresValidation || validation?.status == "passed"
    }

    public var canValidate: Bool {
        requiresValidation && available && index.indexedAssets > 0 && validation?.status != "running"
    }
}

public struct SearchEngineIndex: Decodable, Sendable {
    public let indexedAssets: Int
    public let totalAssets: Int
    public let phase: String
}

public struct SearchEngineRuntime: Decodable, Sendable {
    public let state: String
    public let detail: String?
    public let error: String?

    public var label: String {
        switch state {
        case "ready": "Loaded"
        case "starting": detail ?? "Starting"
        case "error": "Stopped after an error"
        default: "Not loaded; starts when needed"
        }
    }
}

public struct SearchEngineValidation: Decodable, Sendable {
    public let status: String
    public let id: String?
    public let createdAt: String?
    public let error: String?
    public let checks: [SearchEngineCheck]?

    public var label: String {
        switch status {
        case "passed": "Validated"
        case "failed": "Validation failed"
        case "stale": "Index changed since validation"
        case "running": "Validating…"
        case "error": "Validation could not run"
        default: "Not validated"
        }
    }

    public var failedChecks: [SearchEngineCheck] { (checks ?? []).filter { !$0.passed } }
}

public struct SearchEngineCheck: Decodable, Identifiable, Sendable {
    public let name: String
    public let passed: Bool
    public let detail: String
    public var id: String { name }
}
