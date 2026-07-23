"""Composition root for online alignment services."""

from .config import AlignmentConfigurator
from .consensus import AlignmentConsensus
from .detector import AlignmentDetector
from .diagnostics import AlignmentDiagnostics
from .lifecycle import AlignmentLifecycle
from .persistence import AlignmentPersistence
from .transforms import AlignmentTransforms


class AlignmentController:
    def __init__(self, owner) -> None:
        self.config = AlignmentConfigurator(owner)
        self.transforms = AlignmentTransforms(owner)
        self.detector = AlignmentDetector(owner)
        self.lifecycle = AlignmentLifecycle(owner)
        self.consensus = AlignmentConsensus(owner)
        self.diagnostics = AlignmentDiagnostics(owner)
        self.persistence = AlignmentPersistence(owner)
