import LatentCore
import SwiftUI

struct SearchHistoryView: View {
    @Bindable var model: LibraryModel
    let reopen: (SearchHistoryEntry) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Search history").font(.headline)
                Spacer()
                if model.searchHistoryLoading { ProgressView().controlSize(.small) }
                else if !model.searchHistory.isEmpty {
                    Text("\(model.searchHistory.count)").font(.caption).foregroundStyle(.secondary)
                }
            }
            if let error = model.searchHistoryError {
                Text(error).font(.caption).foregroundStyle(.orange)
            }
            if model.searchHistory.isEmpty && !model.searchHistoryLoading {
                VStack(spacing: 8) {
                    Image(systemName: "clock.arrow.circlepath").font(.system(size: 24)).foregroundStyle(.secondary)
                    Text("Your searches, kept here").font(.system(size: 13, weight: .medium))
                    Text("Text and image searches will appear here after you use them.")
                        .font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.center)
                }.frame(maxWidth: .infinity).padding(.vertical, 22)
            } else {
                ScrollView {
                    LazyVStack(spacing: 3) {
                        ForEach(model.searchHistory) { entry in
                            SearchHistoryRow(entry: entry, imageURL: model.client.searchHistoryImageURL(id: entry.id), reopen: {
                                reopen(entry)
                            }, remove: {
                                Task { await model.removeSearchHistory(entry) }
                            })
                        }
                    }
                }.frame(height: min(360, CGFloat(model.searchHistory.reduce(0) {
                    $0 + ($1.filters.isEmpty ? 70 : 84) + ($1.reusable ? 0 : 20)
                })))
            }
            Divider()
            Label("Reopening a saved search makes no new API call.", systemImage: "internaldrive")
                .font(.system(size: 11)).foregroundStyle(.secondary)
        }
        .padding(16).frame(width: 404)
        .controlSize(.small)
        .task { await model.refreshSearchHistory() }
    }
}

private struct SearchHistoryRow: View {
    let entry: SearchHistoryEntry
    let imageURL: URL
    let reopen: () -> Void
    let remove: () -> Void
    @State private var hovered = false

    var body: some View {
        HStack(spacing: 8) {
            Button(action: reopen) {
                HStack(alignment: .center, spacing: 10) {
                    Group {
                        if entry.kind == "image" {
                            AsyncImage(url: imageURL) { image in image.resizable().scaledToFill() } placeholder: {
                                Image(systemName: "photo").foregroundStyle(.secondary)
                            }
                        } else {
                            Image(systemName: "text.magnifyingglass").font(.system(size: 18)).foregroundStyle(.secondary)
                        }
                    }
                    .frame(width: 42, height: 42)
                    .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 7))
                    .clipShape(RoundedRectangle(cornerRadius: 7))
                    VStack(alignment: .leading, spacing: 3) {
                        Text(entry.title).font(.system(size: 12, weight: .medium)).lineLimit(2)
                        Text(detail).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
                        if !entry.filters.isEmpty {
                            Text(entry.filters.summary).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
                        }
                        if !entry.reusable { Text("Model changed; submit a new search").font(.caption2).foregroundStyle(.orange) }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain).disabled(!entry.reusable)
            .accessibilityLabel("Reopen \(entry.kind) search: \(entry.title)")
            Button(action: remove) { Image(systemName: "xmark").font(.system(size: 9)).frame(width: 20, height: 24) }
                .buttonStyle(.plain).foregroundStyle(.secondary)
                .accessibilityLabel("Remove saved search: \(entry.title)")
                .help("Remove this search and its cached query")
        }
        .padding(8)
        .background(hovered ? Color.primary.opacity(0.06) : .clear, in: RoundedRectangle(cornerRadius: 9))
        .onHover { hovered = $0 }
    }

    private var detail: String {
        let kind = entry.kind == "image" ? (entry.imageName ?? "Image search") : "Text search"
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        guard let date = parser.date(from: entry.usedAt) else { return "\(kind) · \(entry.order.label)" }
        return "\(kind) · \(date.formatted(date: .abbreviated, time: .shortened))"
    }
}
