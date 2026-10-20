from .models import BookSearchResult, compute_priority_score
from .providers import (
    search_annas_archive,
    search_books,
    search_gutendex,
    search_libgen,
    search_openlibrary,
)
from .downloader import (
    auto_search_and_download,
    download_book,
    resolve_direct_download_url,
)

__all__ = [
    "BookSearchResult",
    "compute_priority_score",
    "search_books",
    "search_gutendex",
    "search_libgen",
    "search_annas_archive",
    "search_openlibrary",
    "resolve_direct_download_url",
    "download_book",
    "auto_search_and_download",
]
