import LatentCore
import SwiftUI

struct PhotoFilterControls: View {
    @Binding var filters: PhotoFilters
    @State private var calendarDate: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            dateRow("From", key: \.dateFrom)
            dateRow("Through", key: \.dateTo)
            if !filters.validDateRange {
                Text("From must be on or before Through.").font(.caption).foregroundStyle(.red)
            }
            Divider().padding(.vertical, 2)
            filterRow("Minimum rating") {
                Picker("Minimum rating", selection: Binding(get: { filters.ratingMin ?? 0 }, set: { filters.ratingMin = $0 == 0 ? nil : $0 })) {
                    Text("Any rating").tag(0)
                    ForEach(1...5, id: \.self) { value in Text("\(value) ★ or higher").tag(value) }
                }
            }
            filterRow("Starred") {
                Picker("Starred", selection: Binding(get: { filters.starred ?? "any" }, set: { filters.starred = $0 == "any" ? nil : $0 })) {
                    Text("Any").tag("any")
                    Text("Starred (1-5 stars)").tag("yes")
                    Text("Unstarred").tag("no")
                }
            }
            filterRow("Selection") {
                Picker("Selection", selection: Binding(get: { filters.flag ?? "any" }, set: { filters.flag = $0 == "any" ? nil : $0 })) {
                    Text("Any").tag("any")
                    Text("Picked").tag("pick")
                    Text("Rejected").tag("reject")
                    Text("Unmarked").tag("unmarked")
                }
            }
        }
        .pickerStyle(.menu)
        .controlSize(.small)
        .font(.system(size: 12))
    }

    private func filterRow<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        HStack(spacing: 12) {
            Text(title).frame(width: 108, alignment: .leading)
            content().labelsHidden().frame(maxWidth: .infinity, alignment: .leading)
        }.frame(minHeight: 24)
    }

    private func dateRow(_ title: String, key: WritableKeyPath<PhotoFilters, String?>) -> some View {
        HStack(spacing: 12) {
            Toggle(title, isOn: Binding(get: { filters[keyPath: key] != nil }, set: { enabled in
                filters[keyPath: key] = enabled ? (filters.dateFrom ?? filters.dateTo ?? Self.string(Date())) : nil
            })).toggleStyle(.checkbox).frame(width: 108, alignment: .leading)
            HStack(spacing: 6) {
                if filters[keyPath: key] != nil {
                    DatePicker(title, selection: dateBinding(key), displayedComponents: .date)
                        .datePickerStyle(.field).labelsHidden().fixedSize()
                        .accessibilityLabel(title + " date")
                } else {
                    Button("Any date") { openCalendar(title, key: key) }
                        .buttonStyle(.plain).foregroundStyle(.secondary)
                        .accessibilityLabel("Choose \(title.lowercased()) date")
                }
                Spacer(minLength: 0)
                Button { openCalendar(title, key: key) } label: { Image(systemName: "calendar") }
                    .buttonStyle(.plain).foregroundStyle(.secondary).accessibilityLabel("\(title) calendar")
                    .popover(isPresented: Binding(get: { calendarDate == title }, set: { if !$0 { calendarDate = nil } })) {
                        DatePicker(title, selection: dateBinding(key), displayedComponents: .date)
                            .datePickerStyle(.graphical).labelsHidden().padding(12).frame(width: 280)
                    }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }.frame(minHeight: 26)
    }

    private func dateBinding(_ key: WritableKeyPath<PhotoFilters, String?>) -> Binding<Date> {
        Binding(get: { Self.date(filters[keyPath: key]) ?? Date() }, set: { filters[keyPath: key] = Self.string($0) })
    }

    private func openCalendar(_ title: String, key: WritableKeyPath<PhotoFilters, String?>) {
        if filters[keyPath: key] == nil { filters[keyPath: key] = filters.dateFrom ?? filters.dateTo ?? Self.string(Date()) }
        calendarDate = title
    }

    private static var formatter: DateFormatter {
        let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "yyyy-MM-dd"
        return f
    }
    static func string(_ date: Date) -> String { formatter.string(from: date) }
    static func date(_ string: String?) -> Date? { string.flatMap { formatter.date(from: $0) } }
}

extension LibraryModel {
    var filtersForSearch: PhotoFilters {
        if let smart = activeSequence?.smartFilters { return smart }
        var result = searchFilters
        if source == .starred { result.ratingMin = max(result.ratingMin ?? 0, 1) }
        if let period = selectedDate {
            let padded = period.count == 4 ? period + "-01-01" : period.count == 7 ? period + "-01" : period
            if let start = PhotoFilterControls.date(padded) {
                let unit: Calendar.Component = period.count == 4 ? .year : period.count == 7 ? .month : .day
                let next = Calendar.current.date(byAdding: unit, value: 1, to: start)!
                let end = Calendar.current.date(byAdding: .day, value: -1, to: next)!
                result.dateFrom = max(result.dateFrom ?? "0001-01-01", PhotoFilterControls.string(start))
                result.dateTo = min(result.dateTo ?? "9999-12-31", PhotoFilterControls.string(end))
            }
        }
        return result
    }

    func applySearchFilters(_ filters: PhotoFilters) {
        if case .importBatch = source {
            navigate(to: source, filters: filters)
            return
        }
        navigate(to: isSemanticSearch ? source : .library(date: nil), filters: filters)
    }

    func newSmartSequence(filters: PhotoFilters? = nil, folderID: String? = nil) {
        let chosen = filters ?? filtersForSearch
        let draft = SequenceDraft(key: "smart:\(UUID().uuidString)", endpoint: client.baseURL,
            dataID: service.identity?.dataId, editing: nil, adding: [], folderID: folderID)
        draft.smartFilters = chosen
        draft.name = chosen.isEmpty ? "Smart sequence" : chosen.summary
        retainSmartSequenceDraft(draft)
    }
}
