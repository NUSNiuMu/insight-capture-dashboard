"""Offline-first operator voice control."""

__all__ = ["VoiceService"]


def __getattr__(name: str):
    if name == "VoiceService":
        from .service import VoiceService

        return VoiceService
    raise AttributeError(name)
