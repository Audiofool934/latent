import CoreGraphics
import Foundation
import LatentCore

@main struct NativeBenchmark {
    static func milliseconds(_ duration: Duration) -> Double {
        let parts = duration.components
        return Double(parts.seconds) * 1000 + Double(parts.attoseconds) / 1e15
    }

    static func main() async throws {
        let endpoint = CommandLine.arguments.dropFirst().first ?? "http://127.0.0.1:8766"
        let client = try LibraryClient(baseURL: URL(string: endpoint)!)
        let catalogStart = ContinuousClock.now
        var photos: [Photo] = []
        var offset = 0
        while true {
            let page = try await client.photos(date: nil, offset: offset)
            photos.append(contentsOf: page.assets)
            guard let next = page.nextOffset else { break }
            guard next > offset else { throw LibraryError.invalidResponse }
            offset = next
        }
        let catalogMs = milliseconds(catalogStart.duration(to: .now))
        let layoutStart = ContinuousClock.now
        let geometry = MasonryGeometry(aspectRatios: photos.map(\.aspectRatio), width: 1200, minimumColumnWidth: 224)
        let layoutMs = milliseconds(layoutStart.duration(to: .now))
        var maxVisible = 0
        let lookupStart = ContinuousClock.now
        for step in 0..<10_000 {
            let rect = CGRect(x: 0, y: Double(step) / 10_000 * geometry.contentSize.height,
                              width: 1200, height: 800)
            maxVisible = max(maxVisible, geometry.indexes(intersecting: rect).count)
        }
        let lookupMicroseconds = milliseconds(lookupStart.duration(to: .now)) * 1000 / 10_000
        let pipeline = ImagePipeline()
        let firstScreen = Array(photos.prefix(24))
        let imageStart = ContinuousClock.now
        try await loadImages(firstScreen, client: client, pipeline: pipeline)
        let firstScreenMs = milliseconds(imageStart.duration(to: .now))
        let warmStart = ContinuousClock.now
        try await loadImages(firstScreen, client: client, pipeline: pipeline)
        let warmScreenMs = milliseconds(warmStart.duration(to: .now))
        let stressStart = ContinuousClock.now
        try await loadImages(Array(photos.prefix(256)), client: client, pipeline: pipeline)
        let stressMs = milliseconds(stressStart.duration(to: .now))
        let imageStatistics = await pipeline.statistics()
        let similarityStart = ContinuousClock.now
        if let first = photos.first { _ = try await client.similar(to: first.id) }
        let similarityMs = milliseconds(similarityStart.duration(to: .now))
        let warmSimilarityStart = ContinuousClock.now
        if let first = photos.first { _ = try await client.similar(to: first.id) }
        let warmSimilarityMs = milliseconds(warmSimilarityStart.duration(to: .now))
        let results: [String: Any] = [
            "catalogPhotos": photos.count,
            "catalogFetchMs": catalogMs,
            "layoutMs": layoutMs,
            "visibleLookupMeanMicroseconds": lookupMicroseconds,
            "visibleLookupIterations": 10_000,
            "maximumVisibleItems": maxVisible,
            "first24ContactImagesMs": firstScreenMs,
            "cached24ContactImagesMs": warmScreenMs,
            "contactImageStressCount": min(256, photos.count),
            "contactImageStressMs": stressMs,
            "imagePipeline": imageStatistics,
            "similarityRequestMs": similarityMs,
            "warmSimilarityRequestMs": warmSimilarityMs,
            "measures": "Real catalog geometry, local cached JPEG fetch/decode and local similarity. Not a frame-rate measurement.",
            "archiveAccess": false,
            "geminiQueries": 0,
            "os": ProcessInfo.processInfo.operatingSystemVersionString,
        ]
        let data = try JSONSerialization.data(withJSONObject: results, options: [.prettyPrinted, .sortedKeys])
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data("\n".utf8))
    }

    static func loadImages(_ photos: [Photo], client: LibraryClient, pipeline: ImagePipeline) async throws {
        try await withThrowingTaskGroup(of: Void.self) { group in
            var iterator = photos.makeIterator()
            func add(_ photo: Photo) {
                group.addTask {
                    let url = try client.mediaURL(photo.contactUrl)
                    _ = try await pipeline.image(at: url, maximumPixelSize: 512)
                }
            }
            for _ in 0..<6 { if let photo = iterator.next() { add(photo) } }
            while try await group.next() != nil {
                if let photo = iterator.next() { add(photo) }
            }
        }
    }
}
