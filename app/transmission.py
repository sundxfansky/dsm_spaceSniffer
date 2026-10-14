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


def _mapped_path(server: TransmissionServer, torrent: dict[str, Any], filename: str) -> str | None:
    remote_root = posixpath.normpath(str(torrent.get("downloadDir") or "/"))
    remote = posixpath.normpath(posixpath.join(remote_root, filename))
    mount = posixpath.normpath(server.mount_path)
    if remote != mount and not remote.startswith(mount + "/"):
        return None
    suffix = remote[len(mount):].lstrip("/")
    return posixpath.normpath(posixpath.join(server.local_path, suffix)) if suffix else server.local_path


def _age_timestamp(torrent: dict[str, Any]) -> int | None:
    # doneDate is the closest representation of “downloaded”; old clients may
    # omit it, so dateAdded/activityDate are conservative fallbacks.
    return _timestamp(torrent.get("doneDate")) or _timestamp(torrent.get("dateAdded")) or _timestamp(torrent.get("activityDate"))


def find_candidates(
    servers: list[TransmissionServer],
    hardlinks: list[dict[str, Any]],
    max_file_mb: int = 1024,
    age_days: int = 30,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Return connection summaries, safe-to-display cleanup candidates, warnings."""
    cutoff = int(datetime.now(UTC).timestamp()) - age_days * 86400
    max_bytes = max_file_mb * 1024 * 1024
    by_path: dict[str, list[dict[str, Any]]] = {}
    for group in hardlinks:
        for path in group.get("paths", []):
            by_path.setdefault(path, []).append(group)

    connections: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, server in enumerate(servers):
        if not server.enabled:
            connections.append({"name": server.name, "address": server.address, "connected": False, "disabled": True, "torrent_count": 0})
            continue
        try:
            client = TransmissionClient(server)
            torrents = client.torrents()
            connections.append({"name": server.name, "address": server.address, "connected": True, "torrent_count": len(torrents), "rpc_url": client.url})
        except (TransmissionError, ValueError) as exc:
            warnings.append(f"Transmission「{server.name}」：{exc}")
            connections.append({"name": server.name, "address": server.address, "connected": False, "torrent_count": 0, "error": str(exc)})
            continue
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
            try:
                size = int(file_info.get("length") or 0)
            except (TypeError, ValueError):
                continue
            if size > max_bytes:
                continue
            if torrent.get("id") is None:
                continue
            mapped = _mapped_path(server, torrent, str(file_info.get("name") or ""))
            if not mapped:
                continue
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
    return connections, list(unique.values()), warnings
