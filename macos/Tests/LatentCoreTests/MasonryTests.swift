import Foundation
import CoreGraphics
import Testing
@testable import LatentCore

@Test func photographsKeepTheirRatiosAcrossWindowSizes() {
    let ratios = [2.0 / 3, 3.0 / 2, 1, 3.2, 0.34, 1616.0 / 1080]
    for width in [320.0, 960, 1440, 2560] {
        let layout = MasonryGeometry(aspectRatios: ratios, width: width)
        for (index, placement) in layout.placements.enumerated() {
            #expect(abs(placement.frame.width / placement.imageHeight - ratios[index]) < 0.000_001)
            #expect(placement.frame.minX >= 0)
            #expect(placement.frame.maxX <= width)
        }
        #expect(layout.columnWidth >= 240 || layout.columns.count == 1)
        for column in layout.columns {
            for pair in zip(column, column.dropFirst()) {
                #expect(layout.placements[pair.0].frame.maxY < layout.placements[pair.1].frame.minY)
            }
        }
    }
}

@Test func appendingPagesDoesNotMoveExistingPhotographs() {
    let first = [2.0 / 3, 1.5, 1.5, 0.66, 1.5, 1, 2.4]
    let before = MasonryGeometry(aspectRatios: first, width: 1200)
    let after = MasonryGeometry(aspectRatios: first + Array(repeating: 1.5, count: 250), width: 1200)
    for index in first.indices { #expect(before.placements[index].frame == after.placements[index].frame) }
    #expect(after.contentSize.height > before.contentSize.height)
}

@Test func visibleLookupMatchesBruteForceAcrossFullLibrary() {
    let ratios = (0..<25_793).map { [2.0 / 3, 1.5, 1, 2.4][$0 % 4] }
    let layout = MasonryGeometry(aspectRatios: ratios, width: 1200)
    for fraction in stride(from: 0.0, through: 1.0, by: 0.025) {
        let rect = CGRect(x: 0, y: (layout.contentSize.height - 800) * fraction, width: 1200, height: 800)
        let indexed = Set(layout.indexes(intersecting: rect))
        let expected = Set(layout.placements.indices.filter { layout.placements[$0].frame.intersects(rect) })
        #expect(indexed == expected)
        #expect(indexed.count < 40)
    }
}

@Test func invalidDimensionsHaveFiniteFallbackGeometry() {
    let layout = MasonryGeometry(aspectRatios: [0, -.infinity, .nan, -1], width: 1000)
    #expect(layout.contentSize.height.isFinite)
    #expect(layout.placements.allSatisfy { $0.frame.width == $0.imageHeight })
    #expect(MasonryGeometry(aspectRatios: [], width: 1000).indexes(intersecting: .infinite).isEmpty)
}

@Test func fullLibraryGeometryBenchmark() {
    let ratios = (0..<25_793).map { $0 % 3 == 0 ? 2.0 / 3 : 1.5 }
    let start = ContinuousClock.now
    let layout = MasonryGeometry(aspectRatios: ratios, width: 1200)
    let built = ContinuousClock.now
    var maxVisible = 0
    for step in 0..<10_000 {
        let rect = CGRect(x: 0, y: Double(step) / 10_000 * layout.contentSize.height, width: 1200, height: 800)
        maxVisible = max(maxVisible, layout.indexes(intersecting: rect).count)
    }
    print("MASONRY_BENCHMARK photos=25793 build=\(start.duration(to: built)) queries=10000 queryTime=\(built.duration(to: .now)) maxVisible=\(maxVisible)")
    #expect(maxVisible < 40)
}
