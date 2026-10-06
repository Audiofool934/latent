import Foundation
import LatentCore
import Testing

@Test(arguments: [(0.002, "1/500 s"), (0.5, "1/2 s"), (0.8, "0.8 s"), (2.5, "2.5 s")])
func shootingSettingsKeepTheRecordedExposure(_ seconds: Double, _ displayed: String) throws {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    let exif = try decoder.decode(PhotoEXIF.self, from: Data("""
        {"exposure_time":\(seconds),"f_number":6.3,"iso":250,"focal_length":400}
        """.utf8))
    #expect(exif.shutter == displayed)
    #expect(exif.aperture == "ƒ/6.3")
    #expect(exif.sensitivity == "ISO 250")
    #expect(exif.dimensions == nil)
}
