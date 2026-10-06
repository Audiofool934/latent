import CoreGraphics
import Foundation
import ImageIO
import Synchronization
import Testing
@testable import LatentCore

private struct CancellableWork: @unchecked Sendable { let item: DispatchWorkItem }

private final class ImageProtocol: URLProtocol, @unchecked Sendable {
    static let requests = Mutex(0)
    private let work = Mutex<CancellableWork?>(nil)
    static let png: Data = {
        let context = CGContext(data: nil, width: 64, height: 32, bitsPerComponent: 8,
                                bytesPerRow: 64 * 4, space: CGColorSpaceCreateDeviceRGB(),
                                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        let image = context.makeImage()!
        let data = NSMutableData()
        let destination = CGImageDestinationCreateWithData(data, "public.png" as CFString, 1, nil)!
        CGImageDestinationAddImage(destination, image, nil)
        precondition(CGImageDestinationFinalize(destination))
        return data as Data
    }()

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.requests.withLock { $0 += 1 }
        let item = DispatchWorkItem { [weak self] in
            guard let self else { return }
            let response = HTTPURLResponse(url: self.request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            self.client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            self.client?.urlProtocol(self, didLoad: Self.png)
            self.client?.urlProtocolDidFinishLoading(self)
        }
        work.withLock { $0 = CancellableWork(item: item) }
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.08, execute: item)
    }
    override func stopLoading() { work.withLock { $0?.item.cancel() } }
}

@Suite(.serialized)
struct ImagePipelineTests {
    private func pipeline(budget: Int = 128 * 1024 * 1024) -> ImagePipeline {
        ImageProtocol.requests.withLock { $0 = 0 }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ImageProtocol.self]
        return ImagePipeline(session: URLSession(configuration: configuration), byteBudget: budget)
    }

    @Test func concurrentConsumersShareOneDecodeAndThenHitCache() async throws {
        let pipeline = pipeline()
        let url = URL(string: "http://localhost/image.png")!
        async let first = pipeline.image(at: url, maximumPixelSize: 256)
        async let second = pipeline.image(at: url, maximumPixelSize: 256)
        let (a, b) = try await (first, second)
        #expect(a.image === b.image)
        #expect(a.image.width == 64 && a.image.height == 32)
        _ = try await pipeline.image(at: url, maximumPixelSize: 256)
        #expect(ImageProtocol.requests.withLock { $0 } == 1)
        let statistics = await pipeline.statistics()
        #expect(statistics["inFlight"] == 0)
        #expect(statistics["cacheHits"] == 1)
    }

    @Test func cancellingOneConsumerKeepsTheVisibleConsumerAlive() async throws {
        let pipeline = pipeline()
        let url = URL(string: "http://localhost/shared.png")!
        let first = Task { try await pipeline.image(at: url, maximumPixelSize: 256) }
        let second = Task { try await pipeline.image(at: url, maximumPixelSize: 256) }
        try await Task.sleep(for: .milliseconds(20))
        first.cancel()
        do {
            _ = try await first.value
            Issue.record("Cancelled consumer returned an image")
        } catch {}
        let visible = try await second.value
        #expect(visible.image.width == 64)
        #expect(ImageProtocol.requests.withLock { $0 } == 1)
        #expect(await pipeline.statistics()["inFlight"] == 0)
    }

    @Test func cancellingTheLastConsumerReleasesTheRequest() async throws {
        let pipeline = pipeline()
        let request = Task { try await pipeline.image(at: URL(string: "http://localhost/cancel.png")!, maximumPixelSize: 256) }
        try await Task.sleep(for: .milliseconds(20))
        request.cancel()
        do { _ = try await request.value; Issue.record("Cancelled request completed") } catch {}
        #expect(await pipeline.statistics()["inFlight"] == 0)
        #expect(await pipeline.statistics()["cachedImages"] == 0)
    }

    @Test func decodedImagesStayWithinTheByteBudget() async throws {
        let pipeline = pipeline(budget: 20_000)
        for index in 0..<6 {
            _ = try await pipeline.image(at: URL(string: "http://localhost/\(index).png")!, maximumPixelSize: 256)
        }
        let statistics = await pipeline.statistics()
        #expect(statistics["decodedCacheBytes"]! <= 20_000)
        #expect(statistics["decodedCacheBytes"]! > 0)
        #expect(statistics["cachedImages"]! < 6)
    }
}
