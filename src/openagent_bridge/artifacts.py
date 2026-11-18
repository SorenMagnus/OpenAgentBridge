"""Register immutable delivery copies and serve task-authorized binary artifacts."""

import hashlib
import json
import mimetypes
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

CHUNK = 64 * 1024


def delivery_root(config):
    return Path(config.data_dir).resolve() / "delivery"


def _task_storage(task_id):
    return hashlib.sha256(task_id.encode("utf-8")).hexdigest()


def _file_identity(info):
    identity = {
        "device": info.st_dev,
        "inode": info.st_ino,
        "mtime_ns": info.st_mtime_ns,
    }
    # Windows reports different creation/change times for path and descriptor stat.
    if os.name != "nt":
        identity["ctime_ns"] = info.st_ctime_ns
    return identity


def _contained_path(root, path):
    """Require every component below the trusted root to resolve without redirection."""
    try:
        return path.is_relative_to(root) and not root.is_symlink() and path.resolve() == path
    except (OSError, RuntimeError):
        return False


def register_artifact(db, config, task_id, source):
    """Publish a snapshot of a regular file inside the task worktree."""
    task = db.get_task(task_id)
    if task is None:
        raise ValueError("Unknown task")
    if not task.get("worktree_path"):
        raise ValueError("Task has no worktree")
    worktree = Path(task["worktree_path"]).absolute()
    source = Path(os.path.abspath(source))
    if not _contained_path(worktree, source):
        raise ValueError("Artifact source must be a regular file inside the task worktree")
    initial_info = source.stat()
    if not stat.S_ISREG(initial_info.st_mode):
        raise ValueError("Artifact source must be a regular file inside the task worktree")
    limit = config.channels.http.max_artifact_bytes
    if initial_info.st_size > limit:
        raise ValueError("Artifact exceeds configured size limit")
    root = delivery_root(config)
    directory = root / _task_storage(task_id)
    if not _contained_path(root, directory):
        raise ValueError("Invalid artifact storage directory")
    directory.mkdir(parents=True, exist_ok=True)
    if not _contained_path(root, directory):
        raise ValueError("Invalid artifact storage directory")
    artifact_id = uuid.uuid4().hex
    destination = directory / artifact_id
    digest = hashlib.sha256()
    size = 0
    created = False
    try:
        with source.open("rb") as reader:
            if (_file_identity(os.fstat(reader.fileno())) != _file_identity(initial_info)
                    or not _contained_path(worktree, source)):
                raise ValueError("Artifact source changed while opening")
            with destination.open("xb") as writer:
                created = True
                while chunk := reader.read(CHUNK):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("Artifact exceeds configured size limit")
                    digest.update(chunk)
                    writer.write(chunk)
            if _file_identity(os.fstat(reader.fileno())) != _file_identity(initial_info):
                raise ValueError("Artifact source changed while copying")
        metadata = {
            "filename": source.name, "size": size, "sha256": digest.hexdigest(),
            "content_type": mimetypes.guess_type(source.name)[0] or "application/octet-stream",
            "identity": _file_identity(destination.stat()),
        }
        with db.connection:
            db.connection.execute(
                "INSERT INTO artifacts(id, task_id, kind, path, created_at, metadata) "
                "VALUES (?, ?, 'delivery', ?, ?, ?)",
                (artifact_id, task_id, destination.relative_to(root).as_posix(), datetime.now(timezone.utc).isoformat(),
                 json.dumps(metadata)),
            )
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    return artifact_id


def byte_range(header, size):
    if header is None:
        return 0, size
    match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", header)
    if not match or not size:
        raise ValueError
    first, last = match.groups()
    if not first:
        if not last or not int(last):
            raise ValueError
        return max(0, size - int(last)), size
    start = int(first)
    stop = min(size, int(last) + 1) if last else size
    if start >= stop:
        raise ValueError
    return start, stop


def serve_artifact(handler, core, task, parts):
    """Called only after task authorization. Storage names are opaque generated IDs."""
    db = core.db.connection
    where = "FROM artifacts WHERE task_id = ? AND kind = 'delivery'"
    if len(parts) == 5:
        rows = db.execute("SELECT id, metadata " + where + " ORDER BY created_at, id", (task["id"],))
        items = []
        for row in rows:
            metadata = json.loads(row["metadata"])
            item = {key: metadata[key] for key in ("filename", "size", "sha256", "content_type")}
            items.append(dict(item, id=row["id"]))
        handler._send_json(200, {"items": items})
        return
    if len(parts) != 6 or not re.fullmatch("[a-f0-9]{32}", parts[5]):
        handler._send_json(404, {"error": "Artifact not found"})
        return
    artifact_id = parts[5]
    row = db.execute("SELECT metadata, path " + where + " AND id = ?", (task["id"], artifact_id)).fetchone()
    root = delivery_root(core.config)
    if row is None or row["path"] not in (artifact_id, f"{_task_storage(task['id'])}/{artifact_id}"):
        handler._send_json(404, {"error": "Artifact not found"})
        return
    path = root / row["path"]
    metadata = json.loads(row["metadata"])
    try:
        if not _contained_path(root, path):
            raise FileNotFoundError
        expected = path.stat()
        if not stat.S_ISREG(expected.st_mode):
            raise FileNotFoundError
        stream = path.open("rb")
    except (OSError, RuntimeError):
        handler._send_json(404, {"error": "Artifact unavailable"})
        return
    with stream:
        info = os.fstat(stream.fileno())
        size = info.st_size
        if (_file_identity(info) != _file_identity(expected)
                or not _contained_path(root, path) or size != metadata["size"]):
            handler._send_json(409, {"error": "Artifact changed"})
            return
        if size > core.config.channels.http.max_artifact_bytes:
            handler._send_json(413, {"error": "Artifact exceeds download limit"})
            return
        identity = metadata.get("identity")
        if identity is None:
            # Legacy snapshots predate stored file identity.
            digest = hashlib.sha256()
            remaining = size
            while remaining and (chunk := stream.read(min(remaining, CHUNK))):
                digest.update(chunk)
                remaining -= len(chunk)
            unchanged = remaining == 0 and digest.hexdigest() == metadata["sha256"]
        else:
            unchanged = identity == _file_identity(info)
        if not unchanged:
            handler._send_json(409, {"error": "Artifact changed"})
            return
        ranges = handler.headers.get_all("Range", [])
        requested = ",".join(ranges) if ranges else None
        try:
            start, stop = byte_range(requested, size)
        except ValueError:
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{size}")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        handler.send_response(206 if requested else 200)
        handler.send_header("Content-Type", metadata["content_type"])
        filename = quote(metadata["filename"], safe="", errors="replace")
        handler.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + filename)
        handler.send_header("Cache-Control", "private, no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("ETag", '"' + metadata["sha256"] + '"')
        handler.send_header("Content-Length", str(stop - start))
        if requested:
            handler.send_header("Content-Range", f"bytes {start}-{stop - 1}/{size}")
        handler.end_headers()
        if handler.command == "HEAD":
            return
        stream.seek(start)
        remaining = stop - start
        while remaining:
            chunk = stream.read(min(remaining, CHUNK))
            if not chunk:
                break
            handler.wfile.write(chunk)
            remaining -= len(chunk)

