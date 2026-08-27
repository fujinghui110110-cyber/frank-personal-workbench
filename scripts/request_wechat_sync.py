from __future__ import annotations

import os
import time

from app.client import WorkbenchClient


def main() -> None:
    client = WorkbenchClient(
        os.getenv("WORKBENCH_BASE_URL", "http://127.0.0.1:8000"),
        os.getenv("WORKBENCH_WORKER_TOKEN", "local-worker-token"),
    )
    last_error: Exception | None = None
    for _ in range(12):
        try:
            client.request_json("POST", "/api/wechat/sync/run")
            return
        except Exception as error:
            last_error = error
            time.sleep(5)
    raise RuntimeError("登录后未能通知工作台检查微信") from last_error


if __name__ == "__main__":
    main()
