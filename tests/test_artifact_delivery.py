import hashlib
import json
import os
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from openagent_bridge.artifacts import delivery_root, register_artifact
from openagent_bridge.channels.http_channel import HTTPChannelAdapter
from openagent_bridge.cli import main
from openagent_bridge.config import AuthConfig, BridgeConfig
from openagent_bridge.core import BridgeCore
from openagent_bridge.models import CommandSpec
from openagent_bridge.worker import Worker


_OWNER = {"Authorization": "Bearer owner-token"}
_OTHER = {"Authorization": "Bearer other-token"}


def fetch(url, *, headers=None, method="GET"):
    return urlopen(Request(url, headers={**_OWNER, **(headers or {})}, method=method), timeout=5)


def link_directory(link, target):
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.fixture
def delivery(tmp_path):
    config = BridgeConfig(
        data_dir=tmp_path / "data",
        auth=AuthConfig(
            enabled=True,
            api_tokens=["owner-token", "other-token"],
            users=[
                {"id": "http:owner-token", "role": "developer"},
                {"id": "http:other-token", "role": "developer"},
            ],
        ),
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    core = BridgeCore(config)
    core.db.register_project("fixture", str(tmp_path))
    task = core.db.insert_task({
        "project_id": "fixture", "goal": "publish", "worktree_path": str(worktree),
        "user_id": "http:owner-token",
    })
    other = core.db.insert_task({
        "project_id": "fixture", "goal": "other", "user_id": "http:owner-token",
    })
    adapter = HTTPChannelAdapter(port=0)
    core.register_channel_adapter(adapter)
    adapter.start()
    base = f"http://127.0.0.1:{adapter.server.server_port}/api/v1/tasks"
    try:
        yield core, task, other, base
    finally:
        adapter.stop()
        core.db.close()


@pytest.mark.parametrize("autonomy", [False, True])
def test_worker_publishes_required_files_for_review(delivery, tmp_path, monkeypatch, autonomy):
    core, task_id, _, base = delivery
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Tester", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "initial"],
        cwd=worktree, check=True, capture_output=True,
    )
    payload = bytes(range(256)) * 300
    (worktree / "result.blend").write_bytes(payload)
    (worktree / "oversize.bin").write_bytes(payload + b"!")
    core.config.autonomy.enabled = autonomy
    core.config.channels.http.max_artifact_bytes = len(payload)
    core.db.connection.execute(
        "UPDATE tasks SET acceptance = ? WHERE id = ?",
        (json.dumps({"required_files": ["oversize.bin", "result.blend"]}), task_id),
    )
    core.db.connection.commit()
    core.db.transition_task(task_id, "QUEUED")
    monkeypatch.setattr("openagent_bridge.worker.CodexAdapter.detect", lambda self: True)
    monkeypatch.setattr(
        "openagent_bridge.worker.CodexAdapter.build_command",
        lambda self, prompt, worktree, sandbox_mode: CommandSpec(
            [sys.executable, "-c", "pass"], cwd=worktree, timeout_seconds=10,
        ),
    )

    worker = Worker(core.config, db=core.db)
    assert worker.run_once()["status"] == "REVIEW_REQUIRED"

    collection = f"{base}/{task_id}/artifacts"
    with fetch(collection) as response:
        artifact, = json.load(response)["items"]
    assert artifact["filename"] == "result.blend"
    with fetch(f"{collection}/{artifact['id']}") as response:
        assert response.read() == payload
    events = core.db.get_events(task_id)
    assert [event["type"] for event in events if event["type"].startswith("artifact.")] == [
        "artifact.publish_failed", "artifact.published",
    ]
    assert core.db.get_task(task_id)["status"] == "REVIEW_REQUIRED"


def test_worker_backfills_review_artifacts_once_per_snapshot(delivery, tmp_path):
    core, task_id, _, base = delivery
    output = tmp_path / "worktree" / "result.bin"
    output.write_bytes(b"first version")
    with core.db.connection:
        core.db.connection.execute(
            "UPDATE tasks SET status = 'REVIEW_REQUIRED', acceptance = ? WHERE id = ?",
            (json.dumps({"required_files": ["result.bin", "result.bin", "missing.bin"]}), task_id),
        )

    collection = f"{base}/{task_id}/artifacts"
    with fetch(collection) as response:
        assert json.load(response)["items"] == []
    assert Worker(core.config, db=core.db).run_once() is None
    assert Worker(core.config, db=core.db).run_once() is None
    with fetch(collection) as response:
        artifact, = json.load(response)["items"]
    assert artifact["sha256"] == hashlib.sha256(b"first version").hexdigest()
    assert len(list(delivery_root(core.config).glob("*/*"))) == 1
    events = core.db.get_events(task_id)
    assert sum(event["type"] == "artifact.published" for event in events) == 1
    assert any(event["type"] == "artifact.publish_failed" for event in events)
    assert core.db.get_task(task_id)["status"] == "REVIEW_REQUIRED"


@pytest.mark.parametrize("extension", ["png", "glb", "mp4", "zip"])
def test_delivery_round_trip(delivery, tmp_path, extension):
    core, task, other, base = delivery
    file = tmp_path / "worktree" / f"result.{extension}"
    payload = bytes(range(256)) * 1200
    file.write_bytes(payload)
    key = register_artifact(core.db, core.config, task, file)
    file.write_bytes(b"replaced")
    collection = f"{base}/{task}/artifacts"
    url = f"{collection}/{key}"
    with fetch(collection) as response:
        item, = json.load(response)["items"]
        assert item["sha256"] == hashlib.sha256(payload).hexdigest()
        assert "path" not in item
        assert "identity" not in item
    with fetch(url) as response:
        assert response.read() == payload
    with fetch(url, method="HEAD") as response:
        assert int(response.headers["Content-Length"]) == len(payload)
        assert response.read() == b""
    with fetch(url, headers={"Range": "bytes=-17"}) as response:
        assert response.status == 206
        assert response.read() == payload[-17:]
    with fetch(url, headers={"Range": "bytes=10-31"}, method="HEAD") as response:
        assert response.status == 206
        assert response.headers["Content-Range"] == f"bytes 10-31/{len(payload)}"
        assert response.headers["Content-Length"] == "22"
        assert response.read() == b""
    for value in ("bytes=999999-", "bytes=0-2,5-9", "bytes=7-5", "bytes=-", ""):
        with pytest.raises(HTTPError) as invalid:
            fetch(url, headers={"Range": value})
        assert invalid.value.code == 416
    with pytest.raises(HTTPError) as mismatch:
        fetch(f"{base}/{other}/artifacts/{key}")
    assert mismatch.value.code == 404


def test_delivery_policy(delivery, tmp_path):
    core, task, _, base = delivery
    path = tmp_path / "worktree" / "report.bin"
    path.write_bytes(b"123")
    key = register_artifact(core.db, core.config, task, path)
    core.config.channels.http.max_artifact_bytes = 2
    with pytest.raises(ValueError):
        register_artifact(core.db, core.config, task, path)
    with pytest.raises(HTTPError) as oversized:
        fetch(f"{base}/{task}/artifacts/{key}")
    assert oversized.value.code == 413
    for name in ("%2e%2e", "%2fetc%2fpasswd", "missing"):
        with pytest.raises(HTTPError) as missing:
            fetch(f"{base}/{task}/artifacts/{name}")
        assert missing.value.code == 404


def test_publish_command_and_access_control(delivery, tmp_path, monkeypatch, capsys):
    core, task, _, base = delivery
    file = tmp_path / "worktree" / "result.zip"
    file.write_bytes(b"output")
    monkeypatch.setattr("openagent_bridge.config.load_config", lambda _: core.config)
    assert main(["task", "publish-artifact", task, str(file)]) == 0
    key = capsys.readouterr().out.strip()
    collection = f"{base}/{task}/artifacts"
    for url in (collection, f"{collection}/{key}"):
        with pytest.raises(HTTPError) as anonymous:
            urlopen(url, timeout=5)
        assert anonymous.value.code == 401
        for method in ("GET", "HEAD"):
            with pytest.raises(HTTPError) as other_user:
                fetch(url, headers=_OTHER, method=method)
            assert other_user.value.code == 404
    with fetch(f"{collection}/{key}") as response:
        assert response.read() == b"output"


def test_reject_source_escape(delivery, tmp_path):
    core, task, _, _ = delivery
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    outside = outside_directory / "secrets.zip"
    outside.write_bytes(b"outside")
    with pytest.raises(ValueError, match="inside the task worktree"):
        register_artifact(core.db, core.config, task, outside)
    linked_directory = tmp_path / "worktree" / "redirect"
    link_directory(linked_directory, outside_directory)
    with pytest.raises(ValueError, match="inside the task worktree"):
        register_artifact(core.db, core.config, task, linked_directory / outside.name)


def test_reject_delivery_directory_escape(delivery, tmp_path):
    core, task, _, base = delivery
    file = tmp_path / "worktree" / "report.zip"
    file.write_bytes(b"expected")
    key = register_artifact(core.db, core.config, task, file)
    row = core.db.connection.execute("SELECT path FROM artifacts WHERE id = ?", (key,)).fetchone()
    directory = (delivery_root(core.config) / row["path"]).parent
    outside = tmp_path / "outside"
    directory.rename(outside)
    link_directory(directory, outside)
    with pytest.raises(HTTPError) as redirected:
        fetch(f"{base}/{task}/artifacts/{key}")
    assert redirected.value.code == 404
    with pytest.raises(ValueError, match="storage directory"):
        register_artifact(core.db, core.config, task, file)


@pytest.mark.parametrize("legacy", [True, False])
def test_reject_changed_snapshot(delivery, tmp_path, legacy):
    core, task, _, base = delivery
    file = tmp_path / "worktree" / "report.zip"
    file.write_bytes(b"expected")
    key = register_artifact(core.db, core.config, task, file)
    root = delivery_root(core.config)
    row = core.db.connection.execute("SELECT path, metadata FROM artifacts WHERE id = ?", (key,)).fetchone()
    snapshot = root / row["path"]
    if legacy:
        snapshot.rename(root / key)
        snapshot = root / key
        metadata = json.loads(row["metadata"])
        del metadata["identity"]
        with core.db.connection:
            core.db.connection.execute(
                "UPDATE artifacts SET path = ?, metadata = ? WHERE id = ?", (key, json.dumps(metadata), key)
            )
        with fetch(f"{base}/{task}/artifacts/{key}") as response:
            assert response.read() == b"expected"
    snapshot.write_bytes(b"tampered")
    with pytest.raises(HTTPError) as changed:
        fetch(f"{base}/{task}/artifacts/{key}")
    assert changed.value.code == 409
