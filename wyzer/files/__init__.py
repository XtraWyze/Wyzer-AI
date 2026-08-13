"""Local whole-PC file catalog."""

from wyzer.files.catalog import FileCatalog, FileMatch, IndexStats
from wyzer.runtime_paths import file_index_path


def run_startup_quick_scan() -> IndexStats:
    """Update high-value file metadata without delaying application startup."""

    return FileCatalog(file_index_path()).quick_refresh()


__all__ = ["FileCatalog", "FileMatch", "IndexStats", "run_startup_quick_scan"]
