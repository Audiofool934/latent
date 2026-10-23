import AppKit
import LatentCore
import SwiftUI

struct PhotoPreview: View {
    @Bindable var model: LibraryModel
    @State private var image: CGImage?
    @State private var imagePhotoID: Int?
    @State private var errorMessage: String?
    @State private var zoom: CGFloat = 1

    func canMove(by offset: Int) -> Bool { model.canMovePreview(by: offset) }
    func move(by offset: Int) { model.movePreview(by: offset) }

    var body: some View {
        ZStack {
            Color(nsColor: GalleryAppearance.canvas)
            if let image, let photo = model.previewPhoto, imagePhotoID == photo.id {
                PhotoPreviewViewport(image: image, photoID: photo.id, zoom: $zoom)
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
                if let image, let photo = model.previewPhoto, imagePhotoID == photo.id {
                    HStack {
                        Text("\(image.width) × \(image.height) · \(photo.previewAvailable ? "Cached preview" : "Contact preview")")
                            .font(.caption).foregroundStyle(.secondary)
                        Spacer(minLength: 12)
                        zoomControls
                    }
                }
            }
            .padding(.horizontal, 24).padding(.bottom, 10)
        }
        .accessibilityIdentifier("inline-photo-preview")
        .task(id: model.previewPhoto?.id) {
            image = nil
            imagePhotoID = nil
            errorMessage = nil
            zoom = 1
            guard let photo = model.previewPhoto else { return }
            do {
                let url = try model.client.mediaURL(photo.previewUrl)
                let result = try await ImagePipeline.shared.image(at: url, maximumPixelSize: 2048)
                try Task.checkCancellation()
                image = result.image
                imagePhotoID = photo.id
            } catch {
                if !Task.isCancelled { errorMessage = error.localizedDescription }
            }
        }
    }

    private var zoomControls: some View {
        HStack(spacing: 8) {
            Button { zoom = max(1, zoom / 1.5) } label: { Image(systemName: "minus.magnifyingglass") }
                .disabled(zoom <= 1).accessibilityLabel("Zoom out")
                .help("Zoom out")
            Text(zoom == 1 ? "Fit" : String(format: "%.1f×", zoom))
                .font(.caption.monospacedDigit()).foregroundStyle(.secondary).frame(width: 32)
                .accessibilityLabel("Zoom relative to fit")
            Button { zoom = min(PhotoPreviewGeometry.maximumZoom, zoom * 1.5) } label: { Image(systemName: "plus.magnifyingglass") }
                .disabled(zoom >= PhotoPreviewGeometry.maximumZoom).accessibilityLabel("Zoom in")
                .help("Zoom in. Pinch or Option-scroll over the photo to zoom around the pointer.")
            Button("Fit") { zoom = 1 }
                .disabled(zoom <= 1).accessibilityLabel("Fit photo to window")
                .help("Fit the whole photo in the preview")
        }
        .buttonStyle(.borderless).controlSize(.small)
        .accessibilityIdentifier("photo-preview-zoom-controls")
    }
}
