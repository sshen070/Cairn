"""Cairn -- find the documents you forgot you had, and keep the ones that matter."""

__version__ = "0.1.0"

__all__ = ["Cairn", "__version__"]


def __getattr__(name: str) -> object:
    # Imported lazily so `python -m cairn --version` does not pay for sqlite
    # setup, and so a broken optional import cannot break `import cairn`.
    if name == "Cairn":
        from .api import Cairn

        return Cairn
    raise AttributeError(name)
