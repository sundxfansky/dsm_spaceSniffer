"""Task-local, in-memory file metadata. Never opens a NAS path or writes a database file."""
import heapq
import itertools
import posixpath
import sqlite3
import threading


class FileIndex:
    def __init__(self):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        self._db.execute("PRAGMA temp_store=MEMORY")
        self._db.execute("CREATE TABLE files (seq INTEGER PRIMARY KEY, parent TEXT, path TEXT, size INTEGER, links INTEGER)")
        self._db.execute("CREATE INDEX by_parent_size ON files(parent, size DESC, path)")
        self._db.create_function("matches", 2, lambda path, q: q.casefold() in path.casefold(), deterministic=True)

    def add(self, row):
        with self._lock:
            self._db.execute("INSERT INTO files(parent,path,size,links) VALUES(?,?,?,?)",
                             (posixpath.dirname(row["path"]), row["path"], row["size_bytes"], row["link_count"]))

    def browse(self, result, path, offset=0, limit=30, query=""):
        directories = result.get("tree_directories", [])
        current = next((row for row in directories if row["path"] == path), None)
        if current is None:
            raise ValueError("该目录尚未扫描或不在任务范围内")
        children = [{"path": row["path"], "size_bytes": row["size_bytes"], "file_count": row["file_count"], "type": "folder"}
                    for row in directories if posixpath.dirname(row["path"]) == path]
        key = lambda row: (-row["size_bytes"], row["path"])
        children.sort(key=key)
        # Restrict reads to the published snapshot, even if the scanner is already ahead.
        sequence = result.get("scanned_files", 0)
        with self._lock:
            def page(q, start, count):
                dirs = [row for row in children if q.casefold() in row["path"].casefold()]
                where = "parent=? AND seq<=?" + (" AND matches(path, ?)" if q else "")
                params = (path, sequence, q) if q else (path, sequence)
                total = self._db.execute(f"SELECT count(*) FROM files WHERE {where}", params).fetchone()[0] + len(dirs)
                start = min(start, max(0, (total - 1) // count * count))
                rows = self._db.execute(f"SELECT path,size,links FROM files WHERE {where} ORDER BY size DESC,path LIMIT ?",
                                        (*params, start + count))
                files = ({"path": p, "size_bytes": size, "link_count": links, "file_count": 1, "type": "file"} for p, size, links in rows)
                merged = heapq.merge(dirs, files, key=key)
                return list(itertools.islice(merged, start, start + count)), total, start
            items, total, actual_offset = page(query, offset, limit)
            map_items, map_total, _ = page("", 0, 80)
        return {"path": path, "items": items, "total": total, "offset": actual_offset, "limit": limit,
                "map_items": map_items, "map_total": map_total, "size_bytes": current["size_bytes"],
                "file_count": current["file_count"], "scanned_files": sequence}
