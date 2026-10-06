import SwiftUI

struct SheetConnectionBanner: View {
    @Bindable var model: LibraryModel

    var body: some View {
        if !model.workspaceWritable {
            HStack(spacing: 10) {
                if model.service.isConnecting { ProgressView().controlSize(.small) }
                Text(model.service.isConnected
                     ? "Workspace access is unavailable. Your draft is kept while Latent is open."
                     : model.service.state.message)
                    .font(.callout).textSelection(.enabled)
                Spacer()
                Button("Retry connection") { Task { await model.reconnectSheets() } }
                    .disabled(model.service.isConnecting)
            }
            .padding(12)
            .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 10))
        }
    }
}
