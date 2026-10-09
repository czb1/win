"""Compatibility imports for the standalone extractor's shared log reader."""
if __package__:
    from .extract_logs import (PREFIX, TASK_CATEGORIES, ReadReport, events, is_task,
                              payload_key, scope)
else:
    from extract_logs import (PREFIX, TASK_CATEGORIES, ReadReport, events, is_task,
                              payload_key, scope)

__all__ = ["PREFIX", "TASK_CATEGORIES", "ReadReport", "events", "is_task", "payload_key", "scope"]
