import Foundation
import CoreGraphics

/// Geometry only: no image decoding, view creation, or layout work during scrolling.
public struct MasonryGeometry: Sendable {
    public struct Placement: Sendable {
        public let frame: CGRect
        public let imageHeight: CGFloat
        public let column: Int
    }

    public let placements: [Placement]
    public let columns: [[Int]]
    public let contentSize: CGSize
    public let columnWidth: CGFloat

    public init(aspectRatios: [Double], width: CGFloat, minimumColumnWidth: CGFloat = 240,
                gap: CGFloat = 16, inset: CGFloat = 24, captionHeight: CGFloat = 44) {
        let available = max(1, width - 2 * inset)
        let count = max(1, Int((available + gap) / (max(1, minimumColumnWidth) + gap)))
        columnWidth = (available - CGFloat(count - 1) * gap) / CGFloat(count)
        var bottoms = [CGFloat](repeating: inset, count: count)
        var placements: [Placement] = []
        var columns = [[Int]](repeating: [], count: count)
        placements.reserveCapacity(aspectRatios.count)
        for (index, rawRatio) in aspectRatios.enumerated() {
            let ratio = rawRatio.isFinite && rawRatio > 0 ? rawRatio : 1
            let column = bottoms.indices.min { bottoms[$0] < bottoms[$1] }!
            let imageHeight: CGFloat = columnWidth / CGFloat(ratio)
            let x: CGFloat = inset + CGFloat(column) * (columnWidth + gap)
            let frame = CGRect(x: x,
                               y: bottoms[column], width: columnWidth,
                               height: imageHeight + captionHeight)
            placements.append(Placement(frame: frame, imageHeight: imageHeight, column: column))
            columns[column].append(index)
            bottoms[column] = frame.maxY + gap
        }
        self.placements = placements
        self.columns = columns
        contentSize = CGSize(width: max(1, width), height: max(1, (bottoms.max() ?? inset) + inset - gap))
    }

    /// Binary search each column rather than scanning the entire photo library.
    public func indexes(intersecting rect: CGRect) -> [Int] {
        var result: [Int] = []
        for column in columns {
            var lower = 0
            var upper = column.count
            while lower < upper {
                let middle = (lower + upper) / 2
                if placements[column[middle]].frame.maxY < rect.minY {
                    lower = middle + 1
                } else {
                    upper = middle
                }
            }
            for position in lower..<column.count {
                let index = column[position]
                let frame = placements[index].frame
                if frame.minY > rect.maxY { break }
                if frame.intersects(rect) { result.append(index) }
            }
        }
        return result
    }
}
