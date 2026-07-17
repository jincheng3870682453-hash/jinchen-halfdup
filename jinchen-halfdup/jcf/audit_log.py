"""
JCF Audit Logger - 不可篡改审计日志
用于合规举证：谁在什么时候生成了什么内容
"""

import json
import hashlib
import time
import threading
from pathlib import Path
from typing import Optional


class AuditLogger:
    """
    链式哈希审计日志（WORM: Write Once Read Many）

    每条记录包含：
    - timestamp: 时间戳
    - entry_hash: 本条内容的 SHA-256
    - prev_hash:  上一条的哈希（形成链）
    - 业务字段（user_id, category, content_hash 等）

    验证时从头遍历，任何篡改都会导致哈希链断裂。
    """

    def __init__(self, storage_path: str = "data/audit_log.jsonl"):
        self.storage_path = Path(storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._chain_hash = self._load_last_hash()

    # ---------- 写入 ----------

    def log(self, entry: dict) -> str:
        """写入一条审计记录，返回本条哈希"""
        with self._lock:
            entry = dict(entry)  # 不修改原 dict
            entry["timestamp"] = time.time()
            entry["prev_hash"] = self._chain_hash

            # 计算哈希
            entry_str = json.dumps(entry, sort_keys=True, ensure_ascii=False)
            entry_hash = hashlib.sha256(entry_str.encode("utf-8")).hexdigest()
            entry["hash"] = entry_hash
            self._chain_hash = entry_hash

            # 追加写入
            with open(self.storage_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

            return entry_hash

    # ---------- 验证 ----------

    def verify_chain(self) -> dict:
        """
        验证整条链完整性
        返回: {"valid": bool, "total": int, "first_error_at": int|None}
        """
        if not self.storage_path.exists():
            return {"valid": True, "total": 0, "first_error_at": None}

        prev_hash = "0" * 64  # 创世哈希
        count = 0
        first_error = None

        with open(self.storage_path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    if first_error is None:
                        first_error = line_num
                    continue

                # 检查 prev_hash 链接
                if entry.get("prev_hash") != prev_hash:
                    if first_error is None:
                        first_error = line_num
                    # 继续检查后续（可能只是中间断了）
                
                # 重新计算哈希验证完整性
                hash_fields = {k: v for k, v in entry.items() if k != "hash"}
                expected = hashlib.sha256(
                    json.dumps(hash_fields, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                if expected != entry.get("hash"):
                    if first_error is None:
                        first_error = line_num

                prev_hash = entry.get("hash", "")
                count += 1

        return {
            "valid": first_error is None,
            "total": count,
            "first_error_at": first_error,
        }

    # ---------- 查询 ----------

    def query(self, user_id: str, limit: int = 50) -> list[dict]:
        """查询某用户的审计记录"""
        if not self.storage_path.exists():
            return []
        results = []
        with open(self.storage_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("user_id") == user_id:
                    results.append(entry)
        return results[-limit:]

    # ---------- 内部方法 ----------

    def _load_last_hash(self) -> str:
        """加载最后一条记录的哈希作为链头"""
        if not self.storage_path.exists():
            return "0" * 64  # 创世哈希
        last_line = ""
        with open(self.storage_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last_line = line
        if not last_line:
            return "0" * 64
        try:
            entry = json.loads(last_line)
            return entry.get("hash", "0" * 64)
        except json.JSONDecodeError:
            return "0" * 64


# ---------- 合规辅助函数 ----------

def log_ai_output(
    logger: AuditLogger,
    user_id: str,
    category: str,
    content_preview: str,
    content_hash: str,
    requires_review: bool = False,
    review_status: str = "pending",
) -> str:
    """记录一次 AI 输出（合规举证用）"""
    return logger.log({
        "event_type": "ai_output",
        "user_id": user_id,
        "category": category,           # "代码生成" / "医疗建议" / etc.
        "content_preview": content_preview[:200],
        "content_hash": content_hash,
        "requires_review": requires_review,
        "review_status": review_status,
    })


def log_review_signoff(
    logger: AuditLogger,
    user_id: str,
    reviewer: str,
    reviewer_title: str,           # "资深工程师" / "执业律师" / etc.
    original_hash: str,            # 被复核的 AI 输出哈希
) -> str:
    """记录一次人工复核签字"""
    return logger.log({
        "event_type": "review_signoff",
        "user_id": user_id,
        "reviewer": reviewer,
        "reviewer_title": reviewer_title,
        "original_content_hash": original_hash,
        "signoff_time": time.time(),
    })


# ---------- 演示 ----------
if __name__ == "__main__":
    logger = AuditLogger("data/demo_audit.jsonl")

    # 模拟 AI 生成代码
    h1 = log_ai_output(
        logger,
        user_id="alice",
        category="代码生成",
        content_preview="def hello(): print('world')",
        content_hash=hashlib.sha256(b"def hello()").hexdigest(),
        requires_review=True,
        review_status="pending",
    )
    print(f"  AI output logged: {h1[:16]}...")

    # 模拟资深工程师复核签字
    h2 = log_review_signoff(
        logger,
        user_id="alice",
        reviewer="张三",
        reviewer_title="资深工程师",
        original_hash=hashlib.sha256(b"def hello()").hexdigest(),
    )
    print(f"  Review signoff: {h2[:16]}...")

    # 验证链完整性
    result = logger.verify_chain()
    print(f"  Chain valid: {result['valid']}, total entries: {result['total']}")
