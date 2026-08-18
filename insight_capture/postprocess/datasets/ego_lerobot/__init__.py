"""Cached Ego hand-pose to LeRobot delivery pipeline."""

__all__ = ["ExportOptions", "export_dataset"]


def __getattr__(name: str):
    """Load export dependencies only when the pipeline is actually requested."""
    if name in __all__:
        from .pipeline import ExportOptions, export_dataset

        return {"ExportOptions": ExportOptions, "export_dataset": export_dataset}[name]
    raise AttributeError(name)
