import AppKit
import ImageIO
import LatentCore
import Network
import Synchronization
import Testing
@testable import LatentApp

/// Test-only ownership transfer for exercising PhotoItem's isolated deinit.
/// The item is never accessed off actor; the only operation drops its reference.
/// The test clears its local reference before starting detached work, and the
/// enclosing Mutex serializes mutation of this release-only value.
private struct OffActorPhotoItemRelease: @unchecked Sendable {
    private var item: PhotoItem?

    @MainActor init(_ item: PhotoItem?) { self.item = item }

    mutating func release() { item = nil }
}

/// An owned loopback listener lets the real shared pipeline pause at either await.
/// It accepts only synthetic image requests and never opens a window.
@MainActor private final class HeldGalleryImages {
    let listener: NWListener
    private let queue = DispatchQueue(label: "LatentGalleryOwnershipTests")
    private var connections: [NWConnection] = []
    private var pending: [(String, NWConnection)] = []
    private(set) var requests: [String: Int] = [:]
    private(set) var ready = false
    private(set) var stopped = false
    private(set) var failure: NWError?
    private var stopping = false

    init() throws {
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: .any)
        listener = try NWListener(using: parameters)
        listener.stateUpdateHandler = { [weak self] state in
            Task { @MainActor in
                if case .ready = state { self?.ready = true }
                if case .cancelled = state { self?.stopped = true }
                if case .failed(let error) = state { self?.failure = error }
            }
        }
        listener.newConnectionHandler = { [weak self] connection in
            Task { @MainActor in
                guard let self else { connection.cancel(); return }
                self.accept(connection)
            }
        }
        listener.start(queue: queue)
    }

    private func accept(_ connection: NWConnection) {
        guard !stopping else { connection.cancel(); return }
        connections.append(connection)
        connection.start(queue: queue)
        receive(connection, header: Data())
    }

    private func receive(_ connection: NWConnection, header: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 4096) { [weak self] data, _, complete, error in
            Task { @MainActor in
                guard let self, !self.stopping else { connection.cancel(); return }
                let accumulated = header + (data ?? Data())
                if let text = String(data: accumulated, encoding: .utf8), text.contains("\r\n\r\n") {
                    let path = text.split(separator: " ").dropFirst().first.map(String.init) ?? ""
                    self.requests[path, default: 0] += 1
                    self.pending.append((path, connection))
                } else if !complete && error == nil && accumulated.count < 8192 {
                    self.receive(connection, header: accumulated)
                } else {
                    connection.cancel()
                }
            }
        }
    }

    func respond(to path: String, with png: Data) {
        guard !stopping else { return }
        let matches = pending.filter { $0.0 == path }
        pending.removeAll { $0.0 == path }
        for (_, connection) in matches {
            let headers = "HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: \(png.count)\r\nConnection: close\r\n\r\n"
            connection.send(content: Data(headers.utf8) + png, completion: .contentProcessed { _ in connection.cancel() })
        }
    }

    func stop() {
        stopping = true
        listener.cancel()
        connections.forEach { $0.cancel() }
        pending.removeAll()
    }
}

@MainActor @Suite(.serialized)
struct GalleryOwnershipTests {
    private func waitUntil(_ description: String, _ condition: () -> Bool) async throws {
        for _ in 0..<300 {
            if condition() { return }
            try await Task.sleep(for: .milliseconds(10))
        }
        try #require(condition(), Comment(rawValue: description))
    }

    private func photo(_ id: Int, contact: String, preview: String) throws -> Photo {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(Photo.self, from: Data("""
        {"id":\(id),"name":"SYNTHETIC_\(id).ARW","remote_path":"/synthetic/\(id).ARW",
         "provider":"synthetic","fingerprint":"fixture-\(id)","size_bytes":1024,
         "capture_at":"2026:08:29 12:00:00","preview_width":320,"preview_height":480,
         "contact_url":"\(contact)","preview_url":"\(preview)","preview_available":true,
         "rating":5,"caption":""}
        """.utf8))
    }

    private func png() throws -> Data {
        let context = try #require(CGContext(data: nil, width: 64, height: 32, bitsPerComponent: 8,
            bytesPerRow: 64 * 4, space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        let image = try #require(context.makeImage())
        let data = NSMutableData()
        let destination = try #require(CGImageDestinationCreateWithData(data, "public.png" as CFString, 1, nil))
        CGImageDestinationAddImage(destination, image, nil)
        try #require(CGImageDestinationFinalize(destination))
        return data as Data
    }

    @Test(arguments: [false, true])
    func releasingAnImageConsumerKeepsTheOtherCellLoading(holdLargePreview: Bool) async throws {
        let server = try HeldGalleryImages()
        defer { server.stop() }
        try await waitUntil("loopback listener ready or failed") { server.ready || server.failure != nil }
        try #require(server.ready, "Loopback listener failure: \(String(describing: server.failure))")
        let port = try #require(server.listener.port)
        let client = try LibraryClient(baseURL: URL(string: "http://127.0.0.1:\(port.rawValue)")!)
        let prefix = "/media/\(UUID().uuidString)"
        let contact = prefix + "/contact.png", preview = prefix + "/preview.png"
        let retainedContact = holdLargePreview ? contact : prefix + "/retained-contact.png"
        let image = try png()
        let collection = PhotoCollectionView()
        let retained = PhotoItem()
        defer { retained.cancelLoading() }
        retained.configure(photo: try photo(4, contact: retainedContact, preview: preview), client: client) {}
        retained.isSelected = true
        collection.addSubview(retained.view)
        let retainedPicture = try #require(retained.view.subviews.compactMap { $0 as? NSImageView }.first)
        var released: PhotoItem? = PhotoItem()
        weak let owner = released
        released?.configure(photo: try photo(3, contact: contact, preview: preview), client: client) {}
        released?.isSelected = true
        let orphan = try #require(released?.view)
        let orphanPicture = try #require(orphan.subviews.compactMap { $0 as? NSImageView }.first)
        collection.addSubview(orphan)
        // Separate held contacts prove both requests started. In the shared-preview
        // case, both displayed contacts below prove both consumers reached that await.
        try await waitUntil("held contact requests") {
            server.requests[contact] == 1 && server.requests[retainedContact] == 1
        }
        if holdLargePreview {
            server.respond(to: contact, with: image)
            try await waitUntil("both contacts displayed and preview held") {
                server.requests[preview] == 1 && orphanPicture.image != nil && retainedPicture.image != nil
            }
        }

        released = nil
        #expect(owner == nil)
        #expect(orphan.superview == nil && orphan.isHidden)
        #expect(orphanPicture.image == nil && orphanPicture.layer?.borderWidth == 0)
        #expect(retained.view.superview === collection && retained.isSelected)
        if !holdLargePreview {
            server.respond(to: retainedContact, with: image)
            try await waitUntil("surviving cell starts preview") { server.requests[preview] == 1 }
        }
        let contactImage = retainedPicture.image
        server.respond(to: preview, with: image)
        try await waitUntil("surviving cell displays the large preview") {
            retainedPicture.image != nil && retainedPicture.image !== contactImage
        }
        var expectedRequests = [contact: 1, preview: 1]
        expectedRequests[retainedContact] = 1
        #expect(server.requests == expectedRequests)
        #expect(retainedPicture.layer?.borderWidth == 2 && !retained.view.isHidden)
        #expect(retained.view.accessibilityIdentifier() == "photo-4")
        #expect(orphan.superview == nil && orphanPicture.image == nil)
        server.stop()
        try await waitUntil("owned listener stops") { server.stopped }
    }

    @Test func anOffActorReleaseRetiresTheViewOnTheMainActor() async throws {
        let client = try LibraryClient(baseURL: URL(string: "http://127.0.0.1:1")!)
        let collection = PhotoCollectionView()
        var item: PhotoItem? = PhotoItem()
        item?.configure(photo: try photo(3, contact: "https://invalid.example/unrequested", preview: ""), client: client) {}
        let view = try #require(item?.view)
        collection.addSubview(view)
        let holder = Mutex(OffActorPhotoItemRelease(item))
        item = nil
        await Task.detached { holder.withLock { $0.release() } }.value
        try await waitUntil("off-actor release detaches view") { view.superview == nil }
        #expect(view.isHidden && !view.accessibilityPerformPress())
    }
}
