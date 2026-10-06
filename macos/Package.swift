// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "Latent",
    platforms: [.macOS(.v26)],
    products: [
        .executable(name: "Latent", targets: ["LatentApp"]),
        .executable(name: "LatentBenchmark", targets: ["LatentBenchmark"]),
    ],
    targets: [
        .target(name: "LatentCore"),
        .executableTarget(name: "LatentApp", dependencies: ["LatentCore"]),
        .executableTarget(name: "LatentBenchmark", dependencies: ["LatentCore"], path: "Tools/LatentBenchmark"),
        .testTarget(name: "LatentCoreTests", dependencies: ["LatentCore"]),
        .testTarget(name: "LatentAppTests", dependencies: ["LatentApp", "LatentCore"]),
    ]
)
