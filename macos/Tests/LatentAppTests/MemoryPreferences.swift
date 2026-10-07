import Foundation

/// UserDefaults that keeps every value in this process, so tests never ask cfprefsd to write a plist.
/// Foundation sends typed accessors such as bool(forKey:) and stringArray(forKey:) through these three overrides.
final class MemoryPreferences: UserDefaults, @unchecked Sendable {
    private let lock = NSLock()
    private var values: [String: Any] = [:]

    init() {
        // Only UserDefaults methods that are not overridden below can reach this suite; LibraryModel uses none.
        super.init(suiteName: "LatentTests.MemoryPreferences")!
    }

    override func object(forKey defaultName: String) -> Any? {
        lock.withLock { values[defaultName] }
    }

    override func set(_ value: Any?, forKey defaultName: String) {
        guard let value else { return removeObject(forKey: defaultName) }
        // Real UserDefaults rejects values that are not property lists.
        precondition(PropertyListSerialization.propertyList(value, isValidFor: .binary),
                     "\(defaultName) is not a property list value")
        lock.withLock { values[defaultName] = value }
    }

    override func removeObject(forKey defaultName: String) {
        lock.withLock { values[defaultName] = nil }
    }
}
