class ExporterError(Exception):
    """Base error that is safe to present to the user."""


class DiscoveryError(ExporterError):
    """Weixin installation or account data could not be discovered."""


class UnsupportedVersionError(ExporterError):
    """The installed Weixin version has no matching adapter."""


class KeyRecoveryError(ExporterError):
    """The local database key could not be recovered."""


class DatabaseError(ExporterError):
    """An encrypted database could not be opened or inspected."""


class SchemaError(ExporterError):
    """The database schema does not match the supported adapter."""
