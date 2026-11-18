"""Manual file queue transport for air-gapped or offline external agents."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from openagent_bridge.queue import TaskQueue


class ManualFileTransport:
    """Processes task JSON files dropped into an inbox folder."""

    def __init__(self, data_dir: Path | str, queue: TaskQueue) -> None:
        self.data_dir = Path(data_dir)
        self.inbox = self.data_dir / "transport" / "inbox"
        self.outbox = self.data_dir / "transport" / "outbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.queue = queue

    def poll_inbox(self) -> list[str]:
        """Scan inbox directory and enqueue all found task spec JSON files."""
        enqueued_ids = []
        for file in self.inbox.glob("*.json"):
            try:
                raw = json.loads(file.read_text(encoding="utf-8"))
                task_id = self.queue.enqueue(raw)
                enqueued_ids.append(task_id)

                # Move to outbox as processed receipt
                receipt = {
                    "task_id": task_id,
                    "status": "QUEUED",
                    "original_file": file.name,
                }
                out_path = self.outbox / f"{file.stem}.receipt.json"
                out_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
                file.unlink()
            except Exception as exc:
                err_path = self.outbox / f"{file.stem}.error.txt"
                err_path.write_text(str(exc), encoding="utf-8")
        return enqueued_ids
