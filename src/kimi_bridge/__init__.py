"""kimi-bridge: connect kimi-code to IM platforms via the local kimi server."""

from importlib.metadata import PackageNotFoundError, version


try:
    __version__ = version("kimi-bridge")
except PackageNotFoundError:
    __version__ = "unknown"
