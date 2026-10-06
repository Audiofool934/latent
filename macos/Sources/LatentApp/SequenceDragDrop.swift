import LatentCore
import Observation
import SwiftUI

struct SequenceDragPayload: Equatable, Sendable {
    let itemID: String
    let dataID: String
}

@MainActor @Observable final class SequenceDragSession {
    struct Destination {
        let folderID: String?
        let frame: CGRect
    }

    var payload: SequenceDragPayload?
    var name = ""
    var symbol = "rectangle.stack"
    var location = CGPoint.zero
    var targetID: UUID?
    @ObservationIgnored var destinations: [UUID: Destination] = [:]

    func update(_ payload: SequenceDragPayload, item: SequenceHierarchy.Item,
                location: CGPoint, model: LibraryModel) {
        self.payload = payload
        name = item.name
        symbol = if case .folder = item { "folder" } else { "rectangle.stack" }
        self.location = location
        targetID = destinations.filter { _, destination in
            destination.frame.contains(location) && model.itemForSequenceDrop(payload, to: destination.folderID) != nil
        }.min { left, right in
            left.value.frame.width * left.value.frame.height < right.value.frame.width * right.value.frame.height
        }?.key
    }

    func finish(model: LibraryModel) {
        let payload = payload
        let destination = targetID.flatMap { destinations[$0] }
        cancel()
        if let payload, let destination { _ = model.acceptSequenceDrop([payload], to: destination.folderID) }
    }

    func cancel() { payload = nil; targetID = nil }
}

extension LibraryModel {
    func itemForSequenceDrop(_ payload: SequenceDragPayload, to folderID: String?) -> SequenceHierarchy.Item? {
        guard workspaceWritable, !isSaving, service.identity?.dataId == payload.dataID else { return nil }
        let item: SequenceHierarchy.Item?
        if payload.itemID.hasPrefix("folder:") {
            item = sequenceHierarchy.folders[String(payload.itemID.dropFirst(7))].map(SequenceHierarchy.Item.folder)
        } else if payload.itemID.hasPrefix("sequence:") {
            item = sequences.first { $0.id == String(payload.itemID.dropFirst(9)) }.map(SequenceHierarchy.Item.sequence)
        } else { item = nil }
        guard let item, item.parentID != folderID, sequenceHierarchy.canMove(item, to: folderID) else { return nil }
        return item
    }

    func acceptSequenceDrop(_ payloads: [SequenceDragPayload], to folderID: String?) -> Bool {
        guard payloads.count == 1, let payload = payloads.first,
              let item = itemForSequenceDrop(payload, to: folderID) else { return false }
        Task { await move(item, to: folderID, dataID: payload.dataID) }
        return true
    }
}

struct SequenceDragSource: ViewModifier {
    @Bindable var model: LibraryModel
    let item: SequenceHierarchy.Item
    @GestureState private var dragging = false
    @State private var payload: SequenceDragPayload?
    @State private var frame = CGRect.zero

    func body(content: Content) -> some View {
        content
            .onGeometryChange(for: CGRect.self) { $0.frame(in: .named("sequence-organization")) } action: { frame = $0 }
            // List rows have their own hosting coordinate space. Translate the local
            // gesture through the row frame so both sides of the split view agree.
            .highPriorityGesture(DragGesture(minimumDistance: 6, coordinateSpace: .local)
                .updating($dragging) { _, state, _ in state = true }
                .onChanged { value in
                    if payload == nil, model.workspaceWritable, !model.isSaving,
                       let dataID = model.service.identity?.dataId {
                        let started = SequenceDragPayload(itemID: item.id, dataID: dataID)
                        payload = started
                        model.sequenceDrag.update(started, item: item, location: location(value), model: model)
                    }
                    if let payload, model.sequenceDrag.payload == payload {
                        model.sequenceDrag.update(payload, item: item, location: location(value), model: model)
                    }
                }
                .onEnded { value in
                    if let payload, model.sequenceDrag.payload == payload {
                        model.sequenceDrag.update(payload, item: item, location: location(value), model: model)
                        model.sequenceDrag.finish(model: model)
                    }
                    payload = nil
                })
            .onChange(of: dragging) { _, active in
                if !active {
                    Task { @MainActor in
                        await Task.yield()
                        if !dragging, payload != nil { payload = nil; model.sequenceDrag.cancel() }
                    }
                }
            }
            .onDisappear {
                if payload != nil { model.sequenceDrag.cancel() }
                payload = nil
            }
    }

    private func location(_ value: DragGesture.Value) -> CGPoint {
        CGPoint(x: frame.minX + value.location.x, y: frame.minY + value.location.y)
    }
}

struct SequenceDropDestination: ViewModifier {
    @Bindable var model: LibraryModel
    let folderID: String?
    @State private var id = UUID()
    private var targeted: Bool { model.sequenceDrag.targetID == id }

    func body(content: Content) -> some View {
        content
            .background(targeted ? Color.accentColor.opacity(0.13) : .clear, in: RoundedRectangle(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(targeted ? Color.accentColor : .clear, lineWidth: 2)
                .allowsHitTesting(false))
            .onGeometryChange(for: CGRect.self) { $0.frame(in: .named("sequence-organization")) } action: { frame in
                model.sequenceDrag.destinations[id] = .init(folderID: folderID, frame: frame)
            }
            .onDisappear { model.sequenceDrag.destinations.removeValue(forKey: id) }
    }
}

struct SequenceFolderDropDestination: ViewModifier {
    @Bindable var model: LibraryModel
    let item: SequenceHierarchy.Item

    @ViewBuilder func body(content: Content) -> some View {
        if case .folder(let folder) = item {
            content.modifier(SequenceDropDestination(model: model, folderID: folder.id))
        } else { content }
    }
}

struct SequenceDragPreview: View {
    @Bindable var session: SequenceDragSession
    var body: some View {
        if session.payload != nil {
            Label(session.name, systemImage: session.symbol)
                .font(.callout.weight(.medium)).lineLimit(1)
                .padding(.horizontal, 12).padding(.vertical, 8)
                .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8))
                .shadow(color: .black.opacity(0.12), radius: 8, y: 3)
                .fixedSize().position(x: session.location.x + 35, y: session.location.y + 24)
                .allowsHitTesting(false).accessibilityHidden(true)
        }
    }
}
