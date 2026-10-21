"""Small Transmission RPC client used by the analysis and cleanup flows."""
from __future__ import annotations

import base64
import json
import posixpath
import socket
import urllib.error
import urllib.request
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from urllib.parse import urlparse

from .models import TransmissionServer


class TransmissionError(RuntimeError):
    pass


def _rpc_url(server: TransmissionServer) -> str:
    raw = server.address.strip()
    parsed = urlparse(raw if "://" in raw else f"http://{raw}:{server.port}")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Transmission 地址无效: {server.address}")
    if parsed.port is None:
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = f"{host}:{server.port}"
    else:
        netloc = parsed.netloc
    base = f"{parsed.scheme}://{netloc}"
    path = parsed.path.rstrip("/") or server.rpc_path.rstrip("/")
    # An address with an explicit path wins; otherwise use the configured RPC path.
    if not parsed.path or parsed.path == "/":
        path = server.rpc_path.rstrip("/") or "/transmission/rpc"
    return base + (path if path.startswith("/") else "/" + path)


def _timestamp(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


class TransmissionClient:
    def __init__(self, server: TransmissionServer, timeout: int = 20) -> None:
        self.server = server
        self.url = _rpc_url(server)
        self.timeout = timeout
        self.session_id = ""

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.server.username:
            token = base64.b64encode(f"{self.server.username}:{self.server.password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        if self.session_id:
            headers["X-Transmission-Session-Id"] = self.session_id
        return headers

    def call(self, method: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = json.dumps({"method": method, "arguments": arguments or {}, "tag": "dsm-inspector"}).encode()
        for attempt in range(2):
            request = urllib.request.Request(self.url, payload, headers=self._headers(), method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read(16 * 1024 * 1024)
            except urllib.error.HTTPError as exc:
                if exc.code == 409 and attempt == 0:
                    self.session_id = exc.headers.get("X-Transmission-Session-Id", "")
                    if self.session_id:
                        continue
                detail = exc.read(512).decode("utf-8", errors="replace")
                raise TransmissionError(f"Transmission RPC HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
                raise TransmissionError(f"无法连接 Transmission: {exc}") from exc
            try:
                result = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TransmissionError("Transmission 返回了无效 JSON") from exc
            if result.get("result") != "success":
                raise TransmissionError(f"Transmission RPC 失败: {result.get('result', 'unknown')}")
            return result.get("arguments", {})
        raise TransmissionError("Transmission RPC 会话握手失败")

    def test(self) -> dict[str, Any]:
        arguments = self.call("session-get")
        torrents = self.call("torrent-get", {"fields": ["id"]}).get("torrents", [])
        return {
            "name": self.server.name,
            "address": self.server.address,
            "connected": True,
            "version": arguments.get("version"),
            "torrent_count": len(torrents),
            "rpc_url": self.url,
        }

    def torrents(self) -> list[dict[str, Any]]:
        fields = ["id", "name", "hashString", "downloadDir", "files", "activityDate", "dateAdded", "doneDate", "status", "percentDone", "totalSize"]
        return self.call("torrent-get", {"fields": fields}).get("torrents", [])

    def remove_torrent(self, torrent_id: int, delete_local_data: bool = False) -> None:
        self.call("torrent-remove", {"ids": [torrent_id], "delete-local-data": delete_local_data})


# Only tasks below this Transmission directory are matched. Each first-level
# entry (/download/<name>) is located on DSM independently, because the
# entries may be mounted from different shared folders.
DOWNLOAD_ROOT = "/download"
MAX_MATCHES_PER_NAME = 64


def _download_files(torrent: dict[str, Any]) -> list[tuple[str, str, int]]:
    """Return (first-level entry, path inside that entry, size) for files under /download.

    Files placed directly in /download use "" as their entry.
    """
    root = posixpath.normpath(str(torrent.get("downloadDir") or "/"))
    rows = []
    for file_info in torrent.get("files") or []:
        name = str(file_info.get("name") or "")
        if not name:
            continue
        full = posixpath.normpath(posixpath.join(root, name))
        if not full.startswith(DOWNLOAD_ROOT + "/"):
            continue
        relative = full[len(DOWNLOAD_ROOT) + 1:]
        entry, _, inner = relative.partition("/")
        if not inner:
            entry, inner = "", relative
        try:
            size = int(file_info.get("length") or 0)
        except (TypeError, ValueError):
            continue
        rows.append((entry, inner, size))
    return rows


class PathLocator:
    """Finds where Transmission's /download entries live on DSM.

    The file scan feeds every DSM file through add(); only files whose trailing
    path and size match a Transmission file are kept, so memory stays bounded
    by the task list rather than by the volume size.
    """

    def __init__(self, torrent_lists: list[list[dict[str, Any]] | None]) -> None:
        self._wanted: dict[str, set[int]] = {}
        for torrents in torrent_lists:
            for torrent in torrents or []:
                for _, inner, size in _download_files(torrent):
                    self._wanted.setdefault(inner, set()).add(size)
        self._found: dict[str, list[tuple[str, int]]] = {}

    def add(self, path: str, size: int) -> None:
        if not self._wanted:
            return
        index = path.find("/", 1)
        while index != -1:
            inner = path[index + 1:]
            sizes = self._wanted.get(inner)
            if sizes and size in sizes:
                rows = self._found.setdefault(inner, [])
                if len(rows) < MAX_MATCHES_PER_NAME:
                    rows.append((path, size))
            index = path.find("/", index + 1)

    def locate(self, torrents: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Return entry -> {"dsm_path", "matched_files"} decided by file votes."""
        votes: dict[str, dict[str, int]] = {}
        for torrent in torrents:
            for entry, inner, size in _download_files(torrent):
                for path, found_size in self._found.get(inner, []):
                    if found_size == size:
                        directory = path[: -(len(inner) + 1)]
                        if directory:
                            counts = votes.setdefault(entry, {})
                            counts[directory] = counts.get(directory, 0) + 1
        located = {}
        for entry, counts in votes.items():
            # Most matching files wins; ties prefer a DSM folder with the same
            # name, then one outside the hardlinked “电视剧” library, then the
            # shallower path, so the choice is deterministic.
            directory = max(counts, key=lambda item: (counts[item], bool(entry) and posixpath.basename(item) == entry,
                                                      "电视剧" not in item, -item.count("/"), item))
            located[entry] = {"dsm_path": directory, "matched_files": counts[directory]}
        return located


def load_torrents(servers: list[TransmissionServer]) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]] | None], list[str]]:
    """Return connection summaries, task lists (None when unavailable) and warnings."""
    connections: list[dict[str, Any]] = []
    torrent_lists: list[list[dict[str, Any]] | None] = []
    warnings: list[str] = []
    for server in servers:
        if not server.enabled:
            connections.append({"name": server.name, "address": server.address, "connected": False, "disabled": True, "torrent_count": 0})
            torrent_lists.append(None)
            continue
        try:
            client = TransmissionClient(server)
            torrents = client.torrents()
        except (TransmissionError, ValueError) as exc:
            warnings.append(f"Transmission「{server.name}」：{exc}")
            connections.append({"name": server.name, "address": server.address, "connected": False, "torrent_count": 0, "error": str(exc)})
            torrent_lists.append(None)
            continue
        connections.append({"name": server.name, "address": server.address, "connected": True, "torrent_count": len(torrents), "rpc_url": client.url})
        torrent_lists.append(torrents)
    return connections, torrent_lists, warnings


def _age_timestamp(torrent: dict[str, Any]) -> int | None:
    # doneDate is the closest representation of “downloaded”; old clients may
    # omit it, so dateAdded/activityDate are conservative fallbacks.
    return _timestamp(torrent.get("doneDate")) or _timestamp(torrent.get("dateAdded")) or _timestamp(torrent.get("activityDate"))


def find_candidates(
    servers: list[TransmissionServer],
    torrent_lists: list[list[dict[str, Any]] | None],
    locator: PathLocator,
    hardlinks: list[dict[str, Any]],
    max_file_mb: int = 1024,
    age_days: int = 30,
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]], list[str]]:
    """Return discovered path mappings per server, cleanup candidates, warnings."""
    cutoff = int(datetime.now(UTC).timestamp()) - age_days * 86400
    max_bytes = max_file_mb * 1024 * 1024
    by_path: dict[str, list[dict[str, Any]]] = {}
    for group in hardlinks:
        for path in group.get("paths", []):
            by_path.setdefault(path, []).append(group)

    all_mappings: list[list[dict[str, Any]]] = []
    candidates: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, server in enumerate(servers):
        torrents = torrent_lists[index] if index < len(torrent_lists) else None
        if torrents is None:
            all_mappings.append([])
            continue
        located = locator.locate(torrents)
        entries = sorted({entry for torrent in torrents for entry, _, _ in _download_files(torrent)})
        mappings = []
        for entry in entries:
            tr_path = posixpath.join(DOWNLOAD_ROOT, entry) if entry else DOWNLOAD_ROOT
            if entry in located:
                mappings.append({"tr_path": tr_path, **located[entry]})
            else:
                mappings.append({"tr_path": tr_path, "dsm_path": None, "matched_files": 0})
        missing = [row["tr_path"] for row in mappings if not row["dsm_path"]]
        if missing:
            preview = "、".join(missing[:5]) + (f" 等 {len(missing)} 个" if len(missing) > 5 else "")
            warnings.append(f"Transmission「{server.name}」：在所选存储空间中未找到 {preview} 对应的 DSM 路径，这些任务不参与匹配")
        all_mappings.append(mappings)
        for torrent in torrents:
            timestamp = _age_timestamp(torrent)
            files = torrent.get("files") or []
            percent_done = torrent.get("percentDone")
            if timestamp is None or timestamp > cutoff or len(files) != 1:
                continue
            if percent_done is not None:
                try:
                    if float(percent_done) < 0.999:
                        continue
                except (TypeError, ValueError):
                    continue
            file_info = files[0]
            located_files = _download_files(torrent)
            if len(located_files) != 1 or torrent.get("id") is None:
                continue
            entry, inner, size = located_files[0]
            if size > max_bytes or entry not in located:
                continue
            mapped = posixpath.join(located[entry]["dsm_path"], inner)
            for group in by_path.get(mapped, []):
                television_paths = [path for path in group.get("paths", []) if "电视剧" in posixpath.dirname(path)]
                if not television_paths:
                    continue
                raw_id = f"{index}:{torrent.get('id')}:{mapped}:{group.get('inode')}"
                candidate_id = sha256(raw_id.encode()).hexdigest()[:24]
                candidates.append({
                    "id": candidate_id,
                    "server_index": index,
                    "server_name": server.name,
                    "task_id": torrent.get("id"),
                    "task_name": torrent.get("name", ""),
                    "hash": torrent.get("hashString", ""),
                    "download_dir": torrent.get("downloadDir", ""),
                    "file_name": file_info.get("name", ""),
                    "size_bytes": size,
                    "completed_at": datetime.fromtimestamp(timestamp, UTC).isoformat(),
                    "age_days": max(0, int((datetime.now(UTC).timestamp() - timestamp) // 86400)),
                    "dsm_path": mapped,
                    "hardlink_paths": sorted(set(group.get("paths", []))),
                    "television_paths": television_paths,
                    "link_count": group.get("link_count", len(group.get("paths", []))),
                })
    # Multiple Transmission servers can report the same mounted task. Keep all
    # task actions but do not duplicate the exact candidate row within one task.
    unique: dict[str, dict[str, Any]] = {row["id"]: row for row in candidates}
    return all_mappings, list(unique.values()), warnings
