from __future__ import annotations

import hashlib
import heapq
import posixpath
import re
import shlex
import socket
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

import paramiko

from .models import AnalysisRequest, Credentials, VolumeInfo
from .transmission import PathLocator, TransmissionClient, TransmissionError, find_candidates, load_torrents


VOLUME_PATTERN = re.compile(r"^/volume[0-9]+$")


class SynologyError(RuntimeError):
    pass


class ScanCancelled(SynologyError):
    pass


def check_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ScanCancelled("分析已停止")


def parse_host(address: str) -> str:
    raw = address.strip()
    parsed = urlparse(raw if "://" in raw else f"ssh://{raw}")
    if parsed.scheme not in {"ssh", "http", "https"} or not parsed.hostname:
        raise ValueError("请输入有效的群晖地址或主机名")
    return parsed.hostname


def parse_df(output: str) -> list[VolumeInfo]:
    volumes: dict[str, VolumeInfo] = {}
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        filesystem, total, used, available, percent = parts[0:5]
        mount = parts[-1]
        if not VOLUME_PATTERN.fullmatch(mount):
            continue
        try:
            volumes[mount] = VolumeInfo(
                path=mount,
                filesystem=filesystem,
                total_bytes=int(total) * 1024,
                used_bytes=int(used) * 1024,
                available_bytes=int(available) * 1024,
                usage_percent=int(percent.rstrip("%")),
            )
        except ValueError:
            continue
    return sorted(volumes.values(), key=lambda item: int(item.path.removeprefix("/volume")))


def parse_du(output: str, root: str) -> list[dict]:
    rows: dict[str, dict] = {}
    for line in output.splitlines():
        size, separator, path = line.partition("\t")
        if not separator or path == root:
            continue
        try:
            rows[path] = {"path": path, "size_bytes": int(size) * 1024}
        except ValueError:
            continue
    return sorted(rows.values(), key=lambda item: item["size_bytes"], reverse=True)


def parse_large_files(output: str) -> list[dict]:
    files: list[dict] = []
    records = output.split("\0") if "\0" in output else output.splitlines()
    for line in records:
        if not line:
            continue
        parts = line.split("\t", 5)
        if len(parts) != 6:
            raise SynologyError("文件元数据格式不兼容，无法保证扫描完整性")
        try:
            files.append(
                {
                    "device": parts[0],
                    "inode": parts[1],
                    "link_count": int(parts[2]),
                    "size_bytes": int(parts[3]),
                    "ctime": parts[4],
                    "path": parts[5],
                }
            )
        except ValueError:
            raise SynologyError("文件元数据包含无效数值，无法保证扫描完整性")
    return files


def group_hardlinks(output: str | list[dict], volume: str) -> list[dict]:
    grouped: dict[str, dict] = defaultdict(lambda: {"size_bytes": 0, "link_count": 0, "paths": []})
    for row in parse_large_files(output) if isinstance(output, str) else output:
        if row["link_count"] < 2:
            continue
        identity = f'{row["device"]}:{row["inode"]}:{row["ctime"]}:{row["size_bytes"]}:{row["link_count"]}'
        entry = grouped[identity]
        entry["device"] = row["device"]
        entry["inode"] = row["inode"]
        entry["size_bytes"] = row["size_bytes"]
        entry["link_count"] = row["link_count"]
        entry["paths"].append(row["path"])

    result = []
    for entry in grouped.values():
        result.append(
            {
                "volume": volume,
                "device": entry["device"],
                "inode": entry["inode"],
                "size_bytes": entry["size_bytes"],
                "link_count": entry["link_count"],
                "paths": sorted(entry["paths"]),
                "complete": len(entry["paths"]) == entry["link_count"],
                "shared_bytes": entry["size_bytes"] * (len(entry["paths"]) - 1),
            }
        )
    return sorted(result, key=lambda item: item["shared_bytes"], reverse=True)


@dataclass
class SSHSession:
    client: paramiko.SSHClient
    host: str
    fingerprint: str
    identity: dict | None = None
    cancel_event: threading.Event | None = None

    def close(self) -> None:
        self.client.close()

    def run(self, command: str, timeout: int = 3600) -> str:
        output, error, exit_code = self.capture(command, timeout)
        if exit_code != 0:
            raise SynologyError(f"远端只读命令失败: {error[-600:] or f'退出码 {exit_code}'}")
        return output

    def capture(self, command: str, timeout: int = 3600, on_chunk: Callable[[bytes], None] | None = None) -> tuple[str, str, int]:
        channel = None
        try:
            check_cancelled(self.cancel_event)
            _, stdout, stderr = self.client.exec_command(command, timeout=min(timeout, 15))
            channel = stdout.channel
            output = bytearray()
            errors = bytearray()
            deadline = time.monotonic() + timeout
            received = 0
            while True:
                check_cancelled(self.cancel_event)
                if time.monotonic() >= deadline:
                    raise SynologyError("扫描超过时限，未返回完整结果")
                if channel.recv_ready():
                    chunk = channel.recv(65536)
                    received += len(chunk)
                    if received > 256 * 1024 * 1024:
                        raise SynologyError("文件清单超过单次扫描内存限额，请减少所选存储空间后重试")
                    if on_chunk is None:
                        output.extend(chunk)
                    else:
                        on_chunk(chunk)
                if channel.recv_stderr_ready():
                    errors.extend(channel.recv_stderr(65536))
                    if len(errors) > 65536:
                        del errors[:-65536]
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if not channel.recv_ready() and not channel.recv_stderr_ready():
                    time.sleep(0.01)
            exit_code = channel.recv_exit_status()
        except (socket.timeout, paramiko.SSHException, OSError) as exc:
            raise SynologyError(f"远端命令执行失败: {exc}") from exc

        finally:
            if channel is not None:
                channel.close()
        return output.decode("utf-8", errors="replace"), errors.decode("utf-8", errors="replace").strip(), exit_code


def file_scan_command(session: SSHSession, root: str) -> str:
    # Probe GNU find's NUL-delimited metadata output before relying on it.
    fmt = shlex.quote("%D\t%i\t%n\t%s\t%C@\t%p\\0")
    # stat uses integer ctime; keep both implementations on the same format.
    script = 'for p do m=$(stat -c \'%d\t%i\t%h\t%s\t%Z\' "$p") || exit 1; printf \'%s\\t%s\\0\' "$m" "$p"; done'
    probe, _, code = session.capture(f"find /dev/null -maxdepth 0 -printf {fmt}")
    if code == 0 and "\0" in probe:
        return f"LC_ALL=C find {root} -type f -printf {fmt}"
    probe, _, code = session.capture(f"find /dev/null -exec sh -c {shlex.quote(script)} sh {{}} +")
    if code == 0 and parse_large_files(probe):
        ending = "+"
    else:
        ending = "\\;"
        parse_large_files(session.run(f"find /dev/null -exec sh -c {shlex.quote(script)} sh {{}} {ending}"))
    return f"LC_ALL=C find {root} -type f -exec sh -c {shlex.quote(script)} sh {{}} {ending}"


def connect(credentials: Credentials, cancel_event: threading.Event | None = None) -> SSHSession:
    check_cancelled(cancel_event)
    host = parse_host(credentials.address)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=credentials.ssh_port,
            username=credentials.username,
            password=credentials.password,
            timeout=12,
            banner_timeout=12,
            auth_timeout=12,
            look_for_keys=False,
            allow_agent=False,
        )
        transport = client.get_transport()
        if transport is None or transport.get_remote_server_key() is None:
            raise SynologyError("无法读取 SSH 服务器密钥")
        digest = hashlib.sha256(transport.get_remote_server_key().asbytes()).digest()
        fingerprint = "SHA256:" + __import__("base64").b64encode(digest).decode().rstrip("=")
        check_cancelled(cancel_event)
        return SSHSession(client=client, host=host, fingerprint=fingerprint, cancel_event=cancel_event)
    except ScanCancelled:
        client.close()
        raise
    except paramiko.AuthenticationException as exc:
        client.close()
        raise SynologyError("SSH 登录失败，请检查账户、密码和账户权限") from exc
    except (paramiko.SSHException, socket.timeout, OSError) as exc:
        client.close()
        raise SynologyError(f"无法连接群晖 SSH: {exc}") from exc


def discover(credentials: Credentials, cancel_event: threading.Event | None = None) -> tuple[SSHSession, list[VolumeInfo]]:
    session = connect(credentials, cancel_event) if cancel_event is not None else connect(credentials)
    try:
        session.identity = remote_identity(session)
        volumes = parse_df(session.run("LC_ALL=C df -Pk"))
        if not volumes:
            raise SynologyError("未发现 /volumeN 存储空间，请检查账户权限")
        return session, volumes
    except Exception:
        session.close()
        raise


def remote_identity(session: SSHSession) -> dict:
    output = session.run("id -u; id -un", timeout=15).splitlines()
    if len(output) < 2 or not output[0].strip().isdigit():
        raise SynologyError("无法确认远端 SSH 会话身份")
    return {"uid": int(output[0]), "username": output[1].strip()}


def scan_issue(label: str, error: str, code: int, identity: dict | None) -> str:
    lower = error.lower()
    if "permission denied" in lower or "operation not permitted" in lower:
        if identity and identity.get("uid") == 0:
            reason = "当前远端 UID=0，但目标仍拒绝访问；需检查对应挂载、NFS root_squash 或服务端访问限制"
        else:
            reason = "当前 SSH 会话未确认为 root；DSM 管理员、容器 root 或其他终端的 sudo 不代表本次会话已提权"
    elif "no such file or directory" in lower:
        reason = "文件或目录在扫描期间不存在（可能已移动、删除或挂载失效）"
    elif "invalid option" in lower or "unrecognized" in lower or "not found" in lower:
        reason = "远端命令或选项不兼容"
    else:
        reason = "远端命令报告异常，不能直接判定为权限不足"
    return f"{label}未完整读取：{reason}\n{error[-1500:] or f'退出码 {code}'}"


def analyze(
    request: AnalysisRequest,
    progress: Callable[[int, str], None],
    publish: Callable[[dict], None] | None = None,
    cancel_event: threading.Event | None = None,
    on_file: Callable[[dict], None] | None = None,
) -> dict:
    check_cancelled(cancel_event)
    session, discovered = discover(request, cancel_event) if cancel_event is not None else discover(request)
    try:
        discovered_by_path = {item.path: item for item in discovered}
        missing = [path for path in request.volumes if path not in discovered_by_path]
        if missing:
            raise SynologyError(f"存储空间不存在或不可访问: {', '.join(missing)}")

        directories: list[dict] = []
        large_files: list[dict] = []
        linked_files: dict[str, list[dict]] = {}
        folder_totals: dict[tuple[str, str], dict] = {}
        # Bounded previews; full metadata lives in the task's on-demand index.
        previews: dict[str, list[tuple[int, str, int]]] = {}
        warnings: list[str] = []
        transmission_connections: list[dict] = []
        transmission_candidates: list[dict] = []
        transmission_warnings: list[str] = []
        transmission_mappings: list[list[dict]] = []
        torrent_lists: list[list[dict] | None] = []
        locator: PathLocator | None = None
        scanned_files = 0
        total_steps = len(request.volumes) * 2
        completed_steps = 0
        threshold = request.large_file_mb * 1024 * 1024
        last_publish = 0.0

        def snapshot(finished: bool = False) -> dict:
            hardlinks = [group for volume, rows in linked_files.items() for group in group_hardlinks(rows, volume)]
            folders = [dict(row) for row in folder_totals.values() if row["path"] != row["volume"] and row["size_bytes"] >= threshold]
            return {
                "host": session.host,
                "identity": getattr(session, "identity", None),
                "server_fingerprint": session.fingerprint,
                "volumes": [discovered_by_path[path].model_dump() for path in request.volumes],
                "directories": sorted(directories, key=lambda row: row["size_bytes"], reverse=True),
                "large_directories": sorted(folders, key=lambda row: row["size_bytes"], reverse=True),
                "large_directories_total": len(folders),
                "tree_directories": [dict(row) for row in folder_totals.values()],
                "tree_file_index": on_file is not None,
                "tree_file_previews": [{"size_bytes": size, "path": path, "link_count": links}
                                       for rows in previews.values() for size, path, links in rows],
                "large_files": sorted(large_files, key=lambda row: row["size_bytes"], reverse=True),
                "large_files_total": len(large_files),
                "hardlinks": sorted(hardlinks, key=lambda row: row["shared_bytes"], reverse=True),
                "hardlink_groups_total": len(hardlinks),
                # path_mappings appear only once the scan has located them.
                "transmission_connections": [
                    {**row, "path_mappings": transmission_mappings[index]} if index < len(transmission_mappings) else dict(row)
                    for index, row in enumerate(transmission_connections)
                ],
                "transmission_candidates": list(transmission_candidates),
                "transmission_candidates_total": len(transmission_candidates),
                "transmission_warnings": list(transmission_warnings),
                "scanned_files": scanned_files,
                "warnings": list(warnings),
                "complete": finished and not warnings,
                "scan_finished": finished,
                "scope": "大文件夹按所有层级的普通文件逻辑大小递归累加（含子目录，每条硬链接路径分别计数），父子目录不可相加；不是实际磁盘占用。一级目录占用由 du 单独统计。不跟随符号链接。",
                "settings": {"large_file_mb": request.large_file_mb, "result_limit": None,
                             "transmission_age_days": request.transmission_age_days,
                             "transmission_max_file_mb": request.transmission_max_file_mb},
            }

        def emit(force: bool = False) -> None:
            nonlocal last_publish
            now = time.monotonic()
            if publish and (force or now - last_publish >= 2):
                publish(snapshot())
                last_publish = now

        if request.transmission_servers:
            # Task lists are read first so the file scan can locate each
            # /download entry on DSM without the user configuring a mapping.
            progress(3, "读取 Transmission 任务列表")
            transmission_connections, torrent_lists, transmission_warnings = load_torrents(request.transmission_servers)
            warnings.extend(transmission_warnings)
            locator = PathLocator(torrent_lists)
        emit(True)

        def read_scan(command: str, label: str) -> str:
            output, error, code = session.capture(command)
            issue = scan_issue(label, error, code, getattr(session, "identity", None))
            if code != 0 and not output:
                raise SynologyError(issue)
            if code != 0 or error:
                warnings.append(issue)
            return output

        for volume in request.volumes:
            check_cancelled(cancel_event)
            root = shlex.quote(volume)
            linked_files[volume] = []
            progress(5 + int(completed_steps / total_steps * 90), f"扫描 {volume}，实时汇总文件和文件夹")
            pending = bytearray()
            volume_count = 0

            def consume(chunk: bytes) -> None:
                nonlocal scanned_files, volume_count, pending
                pending.extend(chunk)
                # Split bytes before decoding: UTF-8 and records can cross SSH packets.
                records = pending.split(b"\0")
                pending = bytearray(records.pop())
                for raw in records:
                    check_cancelled(cancel_event)
                    rows = parse_large_files(raw.decode("utf-8", errors="replace") + "\0")
                    for row in rows:
                        if not row["path"].startswith(volume + "/"):
                            raise SynologyError("远端返回了扫描范围外的路径")
                        row["volume"] = volume
                        if on_file is not None:
                            on_file(row)
                        if locator is not None:
                            locator.add(row["path"], row["size_bytes"])
                        scanned_files += 1
                        volume_count += 1
                        if row["size_bytes"] >= threshold:
                            large_files.append(row)
                        if row["link_count"] > 1:
                            linked_files[volume].append(row)
                        parent = posixpath.dirname(row["path"])
                        if parent in previews or len(previews) < 1000:
                            preview = previews.setdefault(parent, [])
                            item = (row["size_bytes"], row["path"], row["link_count"])
                            if len(preview) < 8:
                                heapq.heappush(preview, item)
                            elif item > preview[0]:
                                heapq.heapreplace(preview, item)
                        direct_parent = parent
                        while True:
                            key = (volume, parent)
                            entry = folder_totals.setdefault(key, {"volume": volume, "path": parent, "size_bytes": 0, "file_count": 0, "direct_size_bytes": 0, "direct_file_count": 0})
                            entry["size_bytes"] += row["size_bytes"]
                            entry["file_count"] += 1
                            if parent == direct_parent:
                                entry["direct_size_bytes"] += row["size_bytes"]
                                entry["direct_file_count"] += 1
                            if parent == volume:
                                break
                            parent = posixpath.dirname(parent)
                progress(5 + int(completed_steps / total_steps * 90), f"扫描 {volume}：已读取 {scanned_files} 个文件（阶段进度）")
                emit(force=scanned_files == 1)

            command = file_scan_command(session, root)
            try:
                _, error, code = session.capture(command, on_chunk=consume)
                if pending:
                    raise SynologyError("远端文件清单被截断，已读取结果已保留")
                if code != 0 or error:
                    issue = scan_issue(f"{volume} 文件扫描", error, code, getattr(session, "identity", None))
                    if code != 0 and volume_count == 0:
                        raise SynologyError(issue)
                    warnings.append(issue)
            except Exception:
                emit(True)
                raise
            completed_steps += 1
            emit(True)

            progress(5 + int(completed_steps / total_steps * 90), f"统计 {volume} 目录占用")
            du_command = (
                "if du --help 2>&1 | grep -q -- '--max-depth'; then "
                f"LC_ALL=C du -k --max-depth=1 {root}; else "
                f"LC_ALL=C du -k -d 1 {root}; fi"
            )
            for row in parse_du(read_scan(du_command, f"{volume} 目录统计"), volume):
                row["volume"] = volume
                directories.append(row)
            completed_steps += 1
            emit(True)

        progress(97, "整理分析结果")
        check_cancelled(cancel_event)
        if locator is not None:
            progress(98, "定位 Transmission 下载目录并匹配硬链接")
            hardlinks = [group for volume, rows in linked_files.items() for group in group_hardlinks(rows, volume)]
            transmission_mappings, transmission_candidates, match_warnings = find_candidates(
                request.transmission_servers,
                torrent_lists,
                locator,
                hardlinks,
                request.transmission_max_file_mb,
                request.transmission_age_days,
            )
            transmission_warnings.extend(match_warnings)
            warnings.extend(match_warnings)
        return snapshot(finished=True)
    finally:
        session.close()


def cleanup_candidates(
    request: AnalysisRequest,
    result: dict,
    candidate_ids: list[str],
    delete_files: bool = True,
    delete_transmission_tasks: bool = True,
    create_rmed: bool = True,
) -> dict:
    """Delete only candidates published by this job and return per-row outcomes."""
    available = {row.get("id"): row for row in result.get("transmission_candidates", [])}
    selected = [available[item] for item in dict.fromkeys(candidate_ids) if item in available]
    if len(selected) != len(set(candidate_ids)):
        raise ValueError("存在不属于此任务或已失效的候选项")
    if not selected:
        raise ValueError("没有可清理的候选项")

    allowed_volumes = tuple(request.volumes)
    all_paths = sorted({path for row in selected for path in [row.get("dsm_path"), *row.get("hardlink_paths", [])] if path})
    unsafe = [path for path in all_paths if not any(path == root or path.startswith(root + "/") for root in allowed_volumes)]
    if unsafe:
        raise ValueError("候选文件不在本次扫描的存储空间内")

    warnings: list[str] = []
    deleted_paths: list[str] = []
    ssh_session = None
    try:
        if delete_files:
            ssh_session = connect(request)
            for path in all_paths:
                marker = path + ".rmed"
                try:
                    command = f"rm -f -- {shlex.quote(path)}"
                    if create_rmed:
                        command += f" && : > {shlex.quote(marker)}"
                    ssh_session.run(command, timeout=60)
                    deleted_paths.append(path)
                except SynologyError as exc:
                    warnings.append(f"删除文件失败 {path}: {exc}")

        task_status: dict[tuple[int, int], str] = {}
        if delete_transmission_tasks:
            for row in selected:
                task_id = row.get("task_id")
                server_index = int(row.get("server_index", -1))
                key = (server_index, int(task_id)) if task_id is not None else (server_index, -1)
                if key in task_status:
                    continue
                if server_index < 0 or server_index >= len(request.transmission_servers):
                    task_status[key] = "服务器配置不存在"
                    continue
                try:
                    TransmissionClient(request.transmission_servers[server_index]).remove_torrent(int(task_id), delete_local_data=False)
                    task_status[key] = "已删除任务"
                except (TransmissionError, ValueError) as exc:
                    task_status[key] = str(exc)
                    warnings.append(f"删除 Transmission 任务失败 {row.get('task_name', task_id)}: {exc}")
        outcomes = []
        for row in selected:
            key = (int(row.get("server_index", -1)), int(row.get("task_id", -1)))
            file_ok = not delete_files or all(path in deleted_paths for path in row.get("hardlink_paths", []))
            task_ok = not delete_transmission_tasks or task_status.get(key) == "已删除任务"
            outcomes.append({
                "id": row["id"],
                "dsm_path": row.get("dsm_path"),
                "deleted_paths": [path for path in row.get("hardlink_paths", []) if path in deleted_paths],
                "task": task_status.get(key, "未执行"),
                "ok": file_ok and task_ok,
            })
        return {"outcomes": outcomes, "deleted_paths": deleted_paths, "warnings": warnings}
    finally:
        if ssh_session is not None:
            ssh_session.close()
