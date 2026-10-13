"""Locate referenced API documents; business queries are supplied by the model."""
from pathlib import PurePosixPath
import re


def reference_paths(documents, knowledge, point):
    """Recover auxiliary file locations for abbreviated follow-up questions."""
    if not documents:
        return []
    entry = documents[0]
    text = entry.get('output', '')
    source = entry.get('resolved_path', '')
    if not source or not re.search(r'API[^\n]*(?:相同|同一)', text, re.I):
        return []
    root = PurePosixPath(source).parent
    paths = []
    for record in knowledge:
        path = record.get('resolved_path', '')
        if (record.get('point') == point and record.get('kind') == 'read'
                and path and PurePosixPath(path).parent == root
                and re.search(r'https?://', record.get('output', ''))
                and path != source and path not in paths):
            paths.append(path)
    return paths[-2:]
