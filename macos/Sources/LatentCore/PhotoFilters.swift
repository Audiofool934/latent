import Foundation

public struct PhotoFilters: Codable, Hashable, Sendable {
    public var validDateRange: Bool { dateFrom == nil || dateTo == nil || dateFrom! <= dateTo! }
    public var dateFrom: String?
    public var dateTo: String?
    public var ratingMin: Int?
    public var starred: String?
    public var flag: String?

    public init(dateFrom: String? = nil, dateTo: String? = nil, ratingMin: Int? = nil,
                starred: String? = nil, flag: String? = nil) {
        self.dateFrom = dateFrom; self.dateTo = dateTo; self.ratingMin = ratingMin
        self.starred = starred; self.flag = flag
    }

    public var isEmpty: Bool { queryItems.isEmpty }
    public var queryItems: [URLQueryItem] {
        var result: [URLQueryItem] = []
        if let dateFrom, !dateFrom.isEmpty { result.append(.init(name: "date_from", value: dateFrom)) }
        if let dateTo, !dateTo.isEmpty { result.append(.init(name: "date_to", value: dateTo)) }
        if let ratingMin, ratingMin > 0 { result.append(.init(name: "rating_min", value: String(ratingMin))) }
        if let starred, starred != "any" { result.append(.init(name: "starred", value: starred)) }
        if let flag, flag != "any" { result.append(.init(name: "flag", value: flag)) }
        return result
    }

    public var summary: String {
        var parts: [String] = []
        if let dateFrom { parts.append("From \(dateFrom)") }
        if let dateTo { parts.append("Through \(dateTo)") }
        if let ratingMin, ratingMin > 0 { parts.append("≥ \(ratingMin) ★") }
        if starred == "yes" { parts.append("Starred") }
        if starred == "no" { parts.append("Unstarred") }
        if let flag, flag != "any" { parts.append(flag == "pick" ? "Picked" : flag == "reject" ? "Rejected" : "Unmarked") }
        return parts.isEmpty ? "All photos" : parts.joined(separator: " · ")
    }

    private enum EncodedKeys: String, CodingKey { case dateFrom = "date_from", dateTo = "date_to", ratingMin = "rating_min", starred, flag }
    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: EncodedKeys.self)
        try c.encodeIfPresent(dateFrom, forKey: .dateFrom)
        try c.encodeIfPresent(dateTo, forKey: .dateTo)
        try c.encodeIfPresent(ratingMin, forKey: .ratingMin)
        try c.encodeIfPresent(starred, forKey: .starred)
        try c.encodeIfPresent(flag, forKey: .flag)
    }
}
