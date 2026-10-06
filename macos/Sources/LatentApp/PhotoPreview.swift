import AppKit
import LatentCore
import SwiftUI

struct PhotoPreview: View {
    @Bindable var model: LibraryModel
    @State private var image: CGImage?
    @State private var errorMessage: String?

    func canMove(by offset: Int) -> Bool { model.canMovePreview(by: offset) }
    func move(by offset: Int) { model.movePreview(by: offset) }

    var body: some View {
        ZStack {
            Color(nsColor: GalleryAppearance.canvas)
            if let image {
                Image(decorative: image, scale: 1).resizable().scaledToFit()
                    .padding(.horizontal, 24).padding(.top, 12).padding(.bottom, 28)
            } else if let errorMessage {
                ContentUnavailableView("Preview unavailable", systemImage: "photo", description: Text(errorMessage))
            } else {
                ProgressView()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .overlay(alignment: .bottomLeading) {
            VStack(spacing: 8) {
                if let error = model.previewPagingError {
                    HStack {
                        Text(error).font(.callout).foregroundStyle(.orange)
                        Button("Retry next photo") { move(by: 1) }
                    }
                    .padding(12).glassEffect(.regular)
                }
                if let image, let photo = model.previewPhoto {
                    Text("\(image.width) × \(image.height) · \(photo.previewAvailable ? "Cached preview" : "Contact preview")")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 24).padding(.bottom, 10)
        }
        .accessibilityIdentifier("inline-photo-preview")
        .task(id: model.previewPhoto?.id) {
            image = nil
            errorMessage = nil
            guard let photo = model.previewPhoto else { return }
            do {
                let url = try model.client.mediaURL(photo.previewUrl)
                let result = try await ImagePipeline.shared.image(at: url, maximumPixelSize: 2048)
                try Task.checkCancellation()
                image = result.image
            } catch {
                if !Task.isCancelled { errorMessage = error.localizedDescription }
            }
        }
    }
}
