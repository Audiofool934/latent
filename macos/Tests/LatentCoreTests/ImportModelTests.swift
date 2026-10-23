import Foundation
import Testing
@testable import LatentCore

// Captured from the Python service's ImportManager while its folder scan listed the second folder.
private let scanningJSON = """
{"batches":[{"id":"447de554-3885-4839-9fcb-e24cb34f37d2","created_at":"2026-10-23T12:21:05.775416+00:00",
 "revision":"fa535723-f0f8-48cd-8dc8-d876dc217a29","status":"scanning","phase":"preview","error":null,
 "current_path":"later","ignored":0,"scanned_files":2,"scanned_photos":1,"sources":[
 {"id":"a22c2e27-173a-40a9-898d-00a37f466ec8","name":"Camera",
  "provider":"filesystem:a22c2e27-173a-40a9-898d-00a37f466ec8","logical_root":"/local-a22c2e27-173a-40a9-898d-00a37f466ec8",
  "folder":{"path":"/Volumes/Card/Camera","device":16777232,"inode":4614949},"legacy":false,"label":"Camera"},
 {"id":"75bd8082-9adf-434c-90c0-5d1566aed1f5","name":"later",
  "provider":"filesystem:75bd8082-9adf-434c-90c0-5d1566aed1f5","logical_root":"/local-75bd8082-9adf-434c-90c0-5d1566aed1f5",
  "folder":{"path":"/Volumes/Card/later","device":16777232,"inode":4614952},"legacy":false,"label":"later"}],
 "destination":null,"file_count":0,"photo_count":0,"sidecar_count":0,"other_count":0,"bytes_total":0,
 "preview_ready":0,"preview_failed":0,"archived":0,"skipped":0,"copied":0,"archived_bytes":0,"undated":0,
 "failures":[],"extensions":{}}],"data_id":"library-1"}
"""

private func decoder() -> JSONDecoder {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    return decoder
}

private func batch(_ json: String) throws -> PhotoImport {
    try #require(try decoder().decode(ImportList.self, from: Data(json.utf8)).batches.first)
}

@Test func aScanningBatchReportsItsProgressAndHoldsOtherImports() throws {
    let scanning = try batch(scanningJSON)
    #expect(scanning.scanning && scanning.active && !scanning.scanFailed)
    #expect(scanning.label == "Scanning folders")
    #expect(scanning.scannedFiles == 2 && scanning.scannedPhotos == 1)
    #expect(scanning.currentPath == "later" && scanning.title == "Camera + later")
}

@Test func cancelingAndFailedScansHaveTheirOwnStates() throws {
    let canceling = try batch(scanningJSON.replacingOccurrences(of: "\"scanning\"", with: "\"canceling\""))
    #expect(canceling.scanning && canceling.active && canceling.label == "Canceling scan")
    let failed = try batch(scanningJSON.replacingOccurrences(of: "\"scanning\"", with: "\"scan_failed\""))
    #expect(failed.scanFailed && !failed.scanning && !failed.active && failed.label == "Scan failed")
}

@Test func batchesFromOlderServicesDecodeWithoutScanProgress() throws {
    let older = try batch(scanningJSON
        .replacingOccurrences(of: "\"scanned_files\":2,\"scanned_photos\":1,", with: "")
        .replacingOccurrences(of: "\"scanning\"", with: "\"prepared\""))
    #expect(older.scannedFiles == nil && older.scannedPhotos == nil)
    #expect(older.pairedCount == nil && older.derivativeCount == nil)
    #expect(!older.scanning && !older.active && older.label == "Ready to import")
}

@Test func cameraJPEGsPairedWithARAWAreCountedApartFromPhotos() throws {
    let paired = try batch(scanningJSON.replacingOccurrences(
        of: "\"sidecar_count\":0,", with: "\"sidecar_count\":0,\"paired_count\":3,\"derivative_count\":2,"))
    #expect(paired.pairedCount == 3 && paired.derivativeCount == 2 && paired.photoCount == 0)
}
