"""调用审计：每次探测落一行 JSONL（spec：可查到调用者、时间、目标与结果类别）。

纪律沿用仓里 `fetch_log` 记录器的做法——**审计写失败不能破坏探测**：写盘异常
降级为一条 WARNING，不抛到调用方。审计是证据的一部分，但它不是探测的前置条件。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def record(path: str, event: dict[str, Any]) -> None:
    if not path:
        return
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"ts": datetime.now(timezone.utc).isoformat(), **event},
            ensure_ascii=False,
            default=str,
        )
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001 - 审计失败不得中断探测
        logger.warning("probe audit write failed (%s): %s", path, exc)
