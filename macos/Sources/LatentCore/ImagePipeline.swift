import Foundation
import ImageIO

public struct DecodedImage: @unchecked Sendable {
    // CGImage is immutable; NSImage creation stays on the main actor.
    public let image: CGImage
    public var cost: Int { image.bytesPerRow * image.height }
}

public actor ImagePipeline {
    public static let shared = ImagePipeline()
    private struct CachedImage {
        let value: DecodedImage
        var access: UInt64
    }
    private struct Job {
        let id: UUID
        let task: Task<DecodedImage, Error>
        var waiters: Set<UUID>
    }

    private var cache: [String: CachedImage] = [:]
    private var jobs: [String: Job] = [:]
    private var clock: UInt64 = 0
    private var cacheBytes = 0
    private var hits = 0
    private var decodes = 0
    private let byteBudget: Int
    private let session: URLSession

    public init(session: URLSession? = nil, byteBudget: Int = 128 * 1024 * 1024) {
        precondition(byteBudget > 0)
        self.byteBudget = byteBudget
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpMaximumConnectionsPerHost = 6
        configuration.timeoutIntervalForRequest = 15
        configuration.urlCache = nil
        self.session = session ?? URLSession(configuration: configuration)
    }

    public func image(at url: URL, maximumPixelSize: Int) async throws -> DecodedImage {
        try Task.checkCancellation()
        let size = [256, 512, 1024, 2048].first { $0 >= maximumPixelSize } ?? 2048
        let key = "\(url.absoluteString)#\(size)"
        clock &+= 1
        if var entry = cache[key] {
            hits += 1
            entry.access = clock
            cache[key] = entry
            return entry.value
        }
        let waiter = UUID()
        let job: Job
        if var existing = jobs[key] {
            existing.waiters.insert(waiter)
            jobs[key] = existing
            job = existing
        } else {
            let session = session
            let task = Task.detached(priority: .userInitiated) {
                let (data, response) = try await session.data(from: url)
                try Task.checkCancellation()
                guard let response = response as? HTTPURLResponse, response.statusCode == 200 else {
                    throw URLError(.badServerResponse)
                }
                let image = try await ImageDecoder.decode(data, maximumPixelSize: size)
                try Task.checkCancellation()
                return image
            }
            job = Job(id: UUID(), task: task, waiters: [waiter])
            jobs[key] = job
        }
        return try await withTaskCancellationHandler {
            defer { release(key: key, jobID: job.id, waiter: waiter) }
            let value = try await job.task.value
            try Task.checkCancellation()
            if cache[key] == nil, value.cost <= byteBudget {
                while cacheBytes + value.cost > byteBudget, let oldest = cache.min(by: { $0.value.access < $1.value.access }) {
                    cacheBytes -= oldest.value.value.cost
                    cache.removeValue(forKey: oldest.key)
                }
                clock &+= 1
                cache[key] = CachedImage(value: value, access: clock)
                cacheBytes += value.cost
                decodes += 1
            }
            return value
        } onCancel: {
            Task { await self.release(key: key, jobID: job.id, waiter: waiter) }
        }
    }

    public func statistics() -> [String: Int] {
        ["decodedCacheBytes": cacheBytes, "decodedCacheLimit": byteBudget,
         "cachedImages": cache.count, "inFlight": jobs.count, "cacheHits": hits, "decodes": decodes]
    }

    private func release(key: String, jobID: UUID, waiter: UUID) {
        guard var job = jobs[key], job.id == jobID else { return }
        job.waiters.remove(waiter)
        if job.waiters.isEmpty {
            job.task.cancel()
            jobs.removeValue(forKey: key)
        } else {
            jobs[key] = job
        }
    }
}

private enum ImageDecoder {
    static let queue: OperationQueue = {
        let queue = OperationQueue()
        queue.name = "Latent image decoding"
        queue.maxConcurrentOperationCount = 4
        queue.qualityOfService = .userInitiated
        return queue
    }()

    static func decode(_ data: Data, maximumPixelSize: Int) async throws -> DecodedImage {
        try await withCheckedThrowingContinuation { continuation in
            queue.addOperation {
                autoreleasepool {
                    let sourceOptions = [kCGImageSourceShouldCache: false] as CFDictionary
                    guard let source = CGImageSourceCreateWithData(data as CFData, sourceOptions) else {
                        continuation.resume(throwing: URLError(.cannotDecodeContentData))
                        return
                    }
                    let options: [CFString: Any] = [
                        kCGImageSourceCreateThumbnailFromImageAlways: true,
                        kCGImageSourceCreateThumbnailWithTransform: true,
                        kCGImageSourceThumbnailMaxPixelSize: maximumPixelSize,
                        kCGImageSourceShouldCacheImmediately: true,
                    ]
                    guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else {
                        continuation.resume(throwing: URLError(.cannotDecodeContentData))
                        return
                    }
                    continuation.resume(returning: DecodedImage(image: image))
                }
            }
        }
    }
}
