"""Domain errors with user-safe messages."""


class LatentError(Exception):
    """Base class for expected Latent failures."""


class ConfigurationError(LatentError):
    """Raised when a required local configuration value is unavailable."""


class ArchiveSafetyError(LatentError):
    """Raised when a provider response could trigger an unsafe full download."""


class PreviewMetadataNotFound(LatentError):
    """Raised when the fetched RAW prefix does not expose a usable JPEG preview."""


class PreviewDecodeError(LatentError):
    """Raised when an embedded preview cannot be decoded as an image."""
