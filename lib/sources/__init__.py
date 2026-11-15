"""Source backend registry."""
from .base import Source
from .gibmacos import GibMacOSSource
from .local import LocalSource
from .url import UrlSource

REGISTRY = {s.id: s for s in (GibMacOSSource, LocalSource, UrlSource)}


def make_source(args):
    """Build a Source from parsed CLI args."""
    if getattr(args, "local", None):
        return LocalSource(args.local)
    if getattr(args, "url", None):
        return UrlSource(args.url)
    return GibMacOSSource()
