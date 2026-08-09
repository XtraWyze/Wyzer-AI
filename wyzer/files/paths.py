"""Grounded path clustering for file-search results."""

from __future__ import annotations

import math
import ntpath
import re


def dominant_location(paths: list[str], query: str | None = None) -> str:
    """Return the deepest directory containing a clear majority of matched files."""
    if not paths:
        raise ValueError("paths cannot be empty")
    wanted = "".join(character for character in (query or "").casefold() if character.isalnum())
    if wanted:
        exact: dict[str, tuple[int, str]] = {}
        for path in paths:
            current = ntpath.dirname(path)
            while current and current != ntpath.dirname(current):
                name = "".join(
                    character
                    for character in ntpath.basename(current).casefold()
                    if character.isalnum()
                )
                if name == wanted:
                    key = current.casefold()
                    count, _ = exact.get(key, (0, current))
                    exact[key] = (count + 1, current)
                current = ntpath.dirname(current)
        if exact:
            return max(exact.values(), key=lambda value: value[0])[1]
        generic = {"a", "file", "files", "folder", "my", "project", "repo", "repository", "the"}
        terms = [
            term
            for term in re.findall(r"[a-z0-9]+", (query or "").casefold())
            if term not in generic
        ]
        named: dict[str, tuple[tuple[int, int, int, int], str]] = {}
        for path in paths:
            current = ntpath.dirname(path)
            while current and current != ntpath.dirname(current):
                normalized = "".join(
                    character
                    for character in ntpath.basename(current).casefold()
                    if character.isalnum()
                )
                matched = sum(term in normalized for term in terms)
                if matched:
                    key = current.casefold()
                    previous = named.get(key)
                    count = (previous[0][0] if previous else 0) + 1
                    components = {part.casefold() for part in current.split(ntpath.sep)}
                    documents = int("documents" in components)
                    score = (count, matched, documents, -len(normalized))
                    named[key] = (score, current)
                current = ntpath.dirname(current)
        if named:
            return max(named.values(), key=lambda value: value[0])[1]
    if len(paths) == 1:
        return ntpath.dirname(paths[0]) or paths[0]
    counts: dict[str, tuple[int, str]] = {}
    for path in paths:
        current = ntpath.dirname(path)
        observed: set[str] = set()
        while current and current != ntpath.dirname(current):
            key = current.casefold()
            if key not in observed:
                count, _ = counts.get(key, (0, current))
                counts[key] = (count + 1, current)
                observed.add(key)
            current = ntpath.dirname(current)
    required = math.ceil(len(paths) * 0.6)
    candidates = [value for value in counts.values() if value[0] >= required]
    if not candidates:
        return ntpath.commonpath(paths)
    return max(candidates, key=lambda value: len(ntpath.normpath(value[1]).split(ntpath.sep)))[1]
