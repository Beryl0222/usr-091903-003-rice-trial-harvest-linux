"""追加型事件存储（JSONL）。

每类记录一个只追加文件，服务重启后整体重放；写操作串行化并 fsync，
满足“绝不覆盖、离线补传不丢”的基本要求。生产环境可替换为等价接口的
事件流/Kafka 实现，domain 层只依赖 all()/append()/next_seq() 三个方法。
"""

from __future__ import annotations

import json
import os
import threading


class AppendStore:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._seq = 0
        # 记录每类文件当前句柄，追加复用；崩溃恢复靠启动时重读
        self._handles = {}

    def _path(self, kind):
        if not kind.replace("_", "").isalnum():
            raise ValueError(f"非法记录类型 {kind!r}")
        return os.path.join(self.data_dir, f"{kind}.jsonl")

    def append(self, kind, record):
        """把一条 dict 记录追加到 kind 流，返回全局单调序号。"""
        if not isinstance(record, dict) or "id" not in record:
            raise ValueError("追加记录必须是带 id 的对象")
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._seq += 1
            record = dict(record)
            record.setdefault("_seq", self._seq)
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            with open(self._path(kind), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            return self._seq

    def all(self, kind):
        """按追加顺序返回一类记录。"""
        path = self._path(kind)
        if not os.path.exists(path):
            return []
        out = []
        with self._lock:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        out.append(json.loads(line))
            if out:
                self._seq = max(self._seq, max(r.get("_seq", 0) for r in out))
        return out

    def next_seq(self):
        with self._lock:
            return self._seq + 1
