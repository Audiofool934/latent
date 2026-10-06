import LatentCore
import SwiftUI

struct PhotoTrashView: View {
    @Bindable var model: LibraryModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                Text("Latent Trash").font(.title2.bold())
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            Text("Photos stay in .Latent Trash inside their original source folder. Restore returns them to their original paths, with ratings and sequence membership intact.")
                .font(.callout).foregroundStyle(.secondary)
            SheetConnectionBanner(model: model)
            if let error = model.trashError { Text(error).foregroundStyle(.orange).textSelection(.enabled) }
            let entries = model.trashBatches.filter { $0.status != "restored" }
            if entries.isEmpty {
                ContentUnavailableView("Trash is empty", systemImage: "trash")
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 16) {
                        ForEach(entries) { batch in
                            VStack(alignment: .leading, spacing: 9) {
                                HStack {
                                    Text("\(batch.items.count) \(batch.items.count == 1 ? "photo" : "photos") · \(batch.label)").font(.headline)
                                    Spacer()
                                    if batch.transferring { ProgressView().controlSize(.small) }
                                    Button("Restore") { model.restoreTrash(batch) }
                                        .disabled(model.trashBusy || model.trashBatches.contains(where: \.transferring) || !model.workspaceWritable)
                                }
                                ForEach(batch.items, id: \.id) { item in
                                    Text(item.name).font(.callout).lineLimit(1).help(item.remotePath)
                                }
                                if let error = batch.error {
                                    Text(error).font(.callout).foregroundStyle(.orange).textSelection(.enabled)
                                    Button("Retry move to Trash") { model.restoreTrash(batch, restore: false) }
                                        .disabled(model.trashBusy || model.trashBatches.contains(where: \.transferring) || !model.workspaceWritable)
                                }
                            }.padding(16).background(.white.opacity(0.04), in: RoundedRectangle(cornerRadius: 12))
                        }
                    }
                }
            }
        }.padding(26).frame(width: 660, height: 570).preferredColorScheme(.dark)
            .task { await model.refreshTrash() }
    }
}
