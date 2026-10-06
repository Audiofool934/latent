import AppKit
import LatentCore
import SwiftUI

/// The same controls stay mounted while the canvas switches between grid and preview.
struct PhotoReviewBar: View {
    @Bindable var model: LibraryModel
    @Binding var caption: String
    var compact: Bool
    @FocusState private var captionFocused: Bool

    private var photo: Photo? { model.selectedPhoto }
    private var selection: [Photo] { model.gradingPhotos }
    private var single: Bool { selection.count == 1 }
    private var editable: Bool { model.workspaceWritable && photo != nil }
    private var rating: Int? {
        let values = Set(selection.map { $0.rating ?? 0 })
        return values.count == 1 ? values.first : nil
    }
    private var flag: PhotoFlag? {
        let values = Set(selection.map { $0.flag ?? .unmarked })
        return values.count == 1 ? values.first : nil
    }

    var body: some View {
        HStack(spacing: 6) {
            VStack(alignment: .leading, spacing: 3) {
                Text(single ? (photo?.name ?? "Select a photo") : "\(selection.count) photos selected")
                    .font(.system(size: 12, weight: .medium)).lineLimit(1).truncationMode(.middle)
                Text(summary).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
            }
            .frame(width: compact ? 132 : 158, alignment: .leading)
            .accessibilityIdentifier("review-photo-summary")
            separator
            HStack(spacing: 0) {
                ForEach(1...5, id: \.self) { value in
                    icon("Rate selection \(value) stars", symbol: value <= (rating ?? 0) ? "star.fill" : "star",
                         color: value <= (rating ?? 0) ? .orange : .secondary, width: 23) { model.rateSelection(value) }
                        .disabled(!editable)
                }
                icon("Clear rating", symbol: "xmark", width: 23) { model.rateSelection(0) }.disabled(!editable)
            }
            HStack(spacing: 0) {
                icon("Pick selection", symbol: "flag.fill", color: flag == .pick ? .green : .secondary, width: 27) {
                    model.flagSelection(.pick)
                }.help("Pick (Q)")
                icon("Reject selection", symbol: "xmark.circle.fill", color: flag == .reject ? .red : .secondary, width: 27) {
                    model.flagSelection(.reject)
                }.help("Reject (R); clears a Pick first")
                icon("Clear selection flag", symbol: "minus.circle", width: 27) { model.flagSelection(.unmarked) }
                    .disabled(flag == .unmarked)
            }.disabled(!editable)
            separator
            if compact {
                icon("Edit caption", symbol: photo?.caption?.isEmpty == false ? "text.bubble.fill" : "text.bubble") {
                    model.showingPhotoCaption.toggle()
                }
                .disabled(!single || !editable)
            } else {
                HStack(spacing: 4) {
                    captionField
                    icon("Save caption", symbol: "checkmark.circle", width: 24) { saveCaption() }
                        .disabled(!single || !editable || caption == (photo?.caption ?? ""))
                }.frame(minWidth: 90, maxWidth: .infinity)
            }
            Spacer(minLength: 0)
            icon("Find similar", symbol: "photo.badge.magnifyingglass") { model.findSimilar() }
            icon("Edit in PhotoLab", symbol: "slider.horizontal.3") { model.beginEditing() }
                .disabled(model.editingBusy || !editable || model.batches.contains(where: \.transferring))
                .accessibilityIdentifier("edit-in-photolab")
            Menu {
                SequenceAddMenu(model: model, folderID: nil)
                if !model.sequences.isEmpty { Divider() }
                Button("New sequence…") { model.openSequenceEditorForSelection() }
            } label: { Image(systemName: "rectangle.stack.badge.plus").frame(width: 30, height: 30) }
                .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
                .help("Add to sequence").accessibilityLabel("Add to sequence")
                .accessibilityIdentifier("add-to-sequence").disabled(!editable || model.isSaving)
            Menu {
                if let group = photo?.timelapse {
                    Button("View \(group.photoCount) time-lapse frames") { model.openTimelapse(group.id) }
                    Divider()
                }
                if let sequence = model.activeSequence, sequence.smartFilters == nil {
                    Button("Move earlier in sequence") { model.moveSelected(by: -1) }.disabled(!model.canMoveSelected(by: -1))
                    Button("Move later in sequence") { model.moveSelected(by: 1) }.disabled(!model.canMoveSelected(by: 1))
                    Button("Remove from sequence", role: .destructive) { model.removeSelectedFromSequence() }
                    Divider()
                }
                Button("Copy source path") {
                    guard let photo else { return }
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(photo.remotePath, forType: .string)
                }.disabled(!single)
                Divider()
                Button("Move selected photos to Trash…", role: .destructive) { model.requestTrashSelection() }
                    .disabled(!model.canTrashSelection)
            } label: { Image(systemName: "ellipsis").frame(width: 30, height: 30) }
                .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
                .help("Photo actions").accessibilityLabel("Photo actions")
            separator
            icon("Photo info", symbol: model.showingPhotoInfo ? "info.circle.fill" : "info.circle",
                 color: model.showingPhotoInfo ? .accentColor : .secondary) { model.showingPhotoInfo.toggle() }
                .help("Photo info (⌘I)").accessibilityIdentifier("toggle-photo-info")
                .accessibilityValue(model.showingPhotoInfo ? "Shown" : "Hidden")
                .contextMenu {
                    Button("Reset info position", systemImage: "arrow.counterclockwise") {
                        model.photoInfoPositionReset = UUID()
                        model.showingPhotoInfo = true
                    }
                }
        }
        .font(.system(size: 14))
        .padding(.horizontal, 13).frame(height: 56)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 20))
        .disabled(!model.service.isConnected || photo == nil)
        .onDisappear { model.showingPhotoCaption = false }
        .onChange(of: compact) { _, compact in if !compact { model.showingPhotoCaption = false } }
        .accessibilityElement(children: .contain).accessibilityIdentifier("photo-review-bar")
    }

    private var summary: String {
        guard single, let photo else {
            return PhotoFormatting.bytes(selection.reduce(0) { $0 + $1.sizeBytes }) + (rating == nil ? " · Mixed ratings" : "")
        }
        if let group = photo.timelapse { return "Time-lapse · \(group.photoCount) frames · Cover selected" }
        if let exif = photo.exif, !exif.summary.isEmpty { return exif.summary }
        return photo.cameraModel ?? PhotoFormatting.capture(photo)
    }

    private var captionField: some View {
        TextField(single ? "Add a caption…" : "Multiple photos selected", text: $caption)
            .textFieldStyle(.plain).font(.system(size: 12))
            .focused($captionFocused)
            .disabled(!single || !editable).onSubmit {
                saveCaption()
                model.showingPhotoCaption = false
                captionFocused = false
            }
            .accessibilityIdentifier("photo-caption")
    }

    private func saveCaption() {
        guard single, let photo, editable else { return }
        model.annotate(ids: [photo.id], caption: caption)
        NSApp.keyWindow?.makeFirstResponder(nil)
    }

    private var separator: some View { Divider().frame(height: 23) }

    private func icon(_ label: String, symbol: String, color: Color = .secondary,
                      width: CGFloat = 30, action: @escaping () -> Void) -> some View {
        Button {
            NSApp.keyWindow?.makeFirstResponder(nil)
            action()
        } label: { Image(systemName: symbol).foregroundStyle(color).frame(width: width, height: 30) }
            .buttonStyle(.plain).contentShape(Rectangle()).help(label).accessibilityLabel(label)
    }
}

struct PhotoCaptionCard: View {
    @Bindable var model: LibraryModel
    @Binding var caption: String
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Caption").font(.system(size: 13, weight: .semibold))
                Spacer()
                Button { close() } label: { Image(systemName: "xmark").frame(width: 24, height: 24) }
                    .buttonStyle(.plain).foregroundStyle(.secondary).accessibilityLabel("Close caption editor")
            }
            TextField("Add a caption…", text: $caption).textFieldStyle(.roundedBorder)
                .focused($focused).onSubmit { save() }.accessibilityIdentifier("photo-caption")
            HStack {
                Text("Return to save").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Save") { save() }.buttonStyle(.glass)
                    .disabled(!model.workspaceWritable)
            }
        }
        .padding(18).frame(width: 312)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 22))
        .task { focused = true }
        .onKeyPress(.escape) { close(); return .handled }
        .accessibilityElement(children: .contain).accessibilityIdentifier("photo-caption-card")
    }

    private func save() {
        guard let photo = model.selectedPhoto, model.workspaceWritable else { return }
        model.annotate(ids: [photo.id], caption: caption)
        close()
    }
    private func close() {
        model.showingPhotoCaption = false
        focused = false
        NSApp.keyWindow?.makeFirstResponder(nil)
    }
}

/// A nonmodal overlay in the library window. Selection and preview never dismiss it.
struct PhotoInfoCard: View {
    @Bindable var model: LibraryModel
    private var photos: [Photo] { model.gradingPhotos }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                HStack {
                    Label("Photo Info", systemImage: "info.circle").font(.system(size: 13, weight: .semibold))
                    Spacer()
                    Image(systemName: "line.3.horizontal").font(.system(size: 10)).foregroundStyle(.tertiary)
                }
                .contentShape(Rectangle())
                .overlay(PhotoInfoDragHandle())
                .help("Drag to move Photo Info anywhere on screen")
                Button { model.showingPhotoInfo = false } label: {
                    Image(systemName: "xmark").font(.system(size: 11, weight: .medium)).frame(width: 24, height: 24)
                }.buttonStyle(.plain).foregroundStyle(.secondary).help("Close photo info (⌘I)")
                    .accessibilityLabel("Close photo info")
            }
            Text(photos.count == 1 ? photos[0].name : "\(photos.count) photos selected")
                .font(.system(size: 12, weight: .medium)).lineLimit(1).truncationMode(.middle)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            Divider()
            VStack(spacing: 0) {
                row("Captured", value: shared { PhotoFormatting.capture($0) })
                row("Camera", value: shared { $0.cameraModel })
                row("Lens", value: shared { $0.lensModel }, height: 44)
            }
            HStack(spacing: 0) {
                exposure("Shutter", value: shared { $0.exif?.shutter })
                exposure("Aperture", value: shared { $0.exif?.aperture })
            }
            HStack(spacing: 0) {
                exposure("Sensitivity", value: shared { $0.exif?.sensitivity })
                exposure("Focal length", value: shared { $0.exif?.focal })
            }
            Divider()
            HStack {
                Text(shared { ($0.name as NSString).pathExtension.uppercased() })
                Spacer()
                Text(PhotoFormatting.bytes(photos.reduce(0) { $0 + $1.sizeBytes }))
            }.font(.system(size: 11)).foregroundStyle(.secondary)
            HStack {
                Text("Image size").font(.system(size: 10)).foregroundStyle(.secondary)
                Spacer()
                Text(shared { $0.exif?.dimensions }).font(.system(size: 10)).foregroundStyle(.secondary)
            }
        }
        .padding(18).frame(width: 312, height: 358)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 22))
        .accessibilityElement(children: .contain).accessibilityIdentifier("photo-info-card")
    }

    private func shared(_ value: (Photo) -> String?) -> String {
        guard let first = photos.first else { return "Unavailable" }
        let initial = value(first)
        guard photos.allSatisfy({ value($0) == initial }) else { return "Multiple values" }
        return initial?.isEmpty == false ? initial! : "Unavailable"
    }

    private func row(_ label: String, value: String, height: CGFloat = 28) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Text(label).foregroundStyle(.secondary).frame(width: 58, alignment: .leading)
            Text(value).lineLimit(2).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
        }.font(.system(size: 11)).frame(height: height, alignment: .top)
    }

    private func exposure(_ label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.system(size: 10)).foregroundStyle(.secondary)
            Text(value).font(.system(size: 13, weight: .medium)).lineLimit(1).textSelection(.enabled)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}
