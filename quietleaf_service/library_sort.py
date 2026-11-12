"""Shared ordering for stored books. Public timestamps use Unix milliseconds."""
from typing import Literal

from .adapters import SourceError

BookSort = Literal["last_read_desc", "last_read_asc", "added_desc", "added_asc", "title"]


def book_order(sort: BookSort, tie: str = "source,id") -> str:
    orders = {
        "last_read_desc": "(last_read_at=0),last_read_at DESC,created_at DESC",
        "last_read_asc": "(last_read_at=0),last_read_at ASC,created_at DESC",
        "added_desc": "created_at DESC",
        "added_asc": "created_at ASC",
        "title": "title COLLATE NOCASE ASC",
    }
    if sort not in orders:
        raise SourceError("Unknown saved-library sort order")
    return orders[sort] + "," + tie


def book_sort_key(book: dict, sort: BookSort):
    added, read = book.get("addedAt", 0), book.get("lastReadAt", 0)
    tie = (book["source"], book["id"])
    if sort.startswith("last_read"):
        return (not read, -read if sort.endswith("desc") else read, -added, *tie)
    if sort.startswith("added"):
        return (-added if sort.endswith("desc") else added, *tie)
    # Match SQLite's NOCASE collation when merging independently paged stores.
    return (book["title"].translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")), *tie)
