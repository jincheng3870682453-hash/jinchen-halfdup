"""
JCF Trust Tier - 用户信用体系 v2.1
绑定用户ID，用历史行为计算信用分

v2.1 修复：
- 新增时间衰减：长期不活跃的用户信用缓慢回归均值
- 新增会话级临时信用：每次会话开始，临时信用 = 基础信用 - 5
- 会话内连续3次垃圾 → 立即降权（不等55次）
- 基础信用只在大时间尺度上缓慢变化，临时信用处理实时风险
"""

import time
import json
import threading
from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path


class TrustTier(Enum):
    NEWCOMER = "newcomer"   # 信用 < 30  → 严格模式
    NORMAL   = "normal"     # 信用 30-70 → 标准模式
    TRUSTED  = "trusted"    # 信用 70-90 → 宽松模式
    VIP      = "vip"        # 信用 > 90  → 白名单


@dataclass
class UserCredit:
    user_id: str
    credit_score: int = 30           # 基础信用分（长期累积）
    total_sessions: int = 0
    valid_sessions: int = 0
    fused_sessions: int = 0
    consecutive_good: int = 0
    consecutive_bad: int = 0
    total_garbage_inputs: int = 0
    last_seen: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    # 手动白名单（运营可设置）
    manual_vip: bool = False
    # v2.1: 会话内连续垃圾计数
    session_garbage_streak: int = 0
    # v2.1: 上次会话结束时间（用于时间衰减计算）
    last_session_end: float = field(default_factory=time.time)


@dataclass
class SessionCredit:
    """
    v2.1 新增：会话级临时信用

    每次会话开始，临时信用 = 基础信用 - penalty
    会话内的所有判断基于临时信用（更敏感）
    会话结束后，临时信用的变化按比例回写到基础信用
    """
    base_score: int          # 会话开始时的基础信用
    penalty: int = 5         # 会话开始时的临时惩罚
    temp_score: int = 0      # 当前临时信用
    garbage_streak: int = 0   # 会话内连续垃圾计数
    good_streak: int = 0      # 会话内连续正常计数
    clarifications: int = 0    # 会话内 HIGH 追问轮次

    def __post_init__(self):
        self.temp_score = self.base_score - self.penalty


class CreditStore:
    """信用存储（文件持久化，可替换为 Redis/DB）"""

    def __init__(self, store_path: str = "data/credit_store.json"):
        self.store_path = Path(store_path)
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, UserCredit] = {}
        self._lock = threading.RLock()
        self._load()

    def _load(self):
        if not self.store_path.exists():
            return
        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for uid, d in data.items():
                self._cache[uid] = UserCredit(**d)
        except (json.JSONDecodeError, TypeError) as e:
            print(f"[CreditStore] WARN: failed to load: {e}")

    def _save(self):
        with self._lock:
            data = {uid: asdict(c) for uid, c in self._cache.items()}
            tmp = self.store_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp.replace(self.store_path)

    def get(self, user_id: str) -> Optional[UserCredit]:
        return self._cache.get(user_id)

    def get_or_create(self, user_id: str) -> UserCredit:
        with self._lock:
            if user_id not in self._cache:
                self._cache[user_id] = UserCredit(user_id=user_id)
                self._save()
            return self._cache[user_id]

    def save(self, credit: UserCredit):
        with self._lock:
            self._cache[credit.user_id] = credit
            self._save()

    def set_manual_vip(self, user_id: str, vip: bool = True):
        """运营手动设置白名单"""
        c = self.get_or_create(user_id)
        c.manual_vip = vip
        if vip:
            c.credit_score = max(c.credit_score, 95)
        self.save(c)

    def get_all_stats(self) -> dict:
        with self._lock:
            tiers = {t.value: 0 for t in TrustTier}
            for c in self._cache.values():
                t = TrustEngine._score_to_tier_static(c.credit_score, c.manual_vip)
                tiers[t.value] += 1
            return {
                "total_users": len(self._cache),
                "by_tier": tiers,
            }


class TrustEngine:
    """
    信用引擎 v2.1

    核心逻辑：
    - 基础信用（长期）：缓慢累积，时间衰减回归均值
    - 临时信用（会话内）：敏感响应，连续垃圾立即降权
    - 好人越来越舒服，坏人会话内就被制裁
    """

    # ========== 时间衰减参数 ==========
    # 超过此天数不活跃 → 开始衰减
    INACTIVITY_THRESHOLD_DAYS = 30
    # 每天衰减量（最多衰减到均值 50）
    DECAY_PER_DAY = 0.5
    # 信用均值（回归目标）
    MEAN_SCORE = 50
    # 最大衰减总量（不会无限衰减）
    MAX_DECAY = 20

    # ========== 会话内参数 ==========
    # 会话开始时的临时惩罚
    SESSION_PENALTY = 5
    # 连续垃圾多少次 → 立即降权
    GARBAGE_STREAK_LIMIT = 3
    # 连续垃圾时的临时信用扣除
    STREAK_PENALTY = 15
    # 单次会话内最大临时扣分
    MAX_TEMP_DEDUCTION = 30

    def __init__(self, store: CreditStore):
        self.store = store
        # 会话级临时信用缓存（user_id → SessionCredit）
        self._sessions: dict[str, SessionCredit] = {}
        self._sess_lock = threading.RLock()

    # ========== 查询接口 ==========

    def get_tier(self, user_id: str) -> TrustTier:
        credit = self.store.get(user_id)
        if credit is None:
            return TrustTier.NEWCOMER
        # 应用时间衰减后判断
        effective_score = self._apply_time_decay(credit)
        return self._score_to_tier(effective_score, credit.manual_vip)

    def get_credit_score(self, user_id: str) -> int:
        credit = self.store.get(user_id)
        if credit is None:
            return 30
        return int(self._apply_time_decay(credit))

    @staticmethod
    def _score_to_tier_static(score: int, manual_vip: bool = False) -> TrustTier:
        if manual_vip or score >= 90:
            return TrustTier.VIP
        if score >= 70:
            return TrustTier.TRUSTED
        if score >= 30:
            return TrustTier.NORMAL
        return TrustTier.NEWCOMER

    def _score_to_tier(self, score: int, manual_vip: bool = False) -> TrustTier:
        return self._score_to_tier_static(score, manual_vip)

    # ========== 时间衰减 ==========

    def _apply_time_decay(self, credit: UserCredit) -> float:
        """
        长期不活跃的用户，信用缓慢回归均值

        超过 30 天没会话 → 每天衰减 0.5 分
        最多衰减 20 分，不会无限衰减
        新用户（< 30天）不衰减
        """
        now = time.time()
        days_since_last = (now - credit.last_session_end) / 86400
        days_since_create = (now - credit.created_at) / 86400

        # 新账号（30天内）不衰减
        if days_since_create < self.INACTIVITY_THRESHOLD_DAYS:
            return float(credit.credit_score)

        # 超过阈值才衰减
        if days_since_last < self.INACTIVITY_THRESHOLD_DAYS:
            return float(credit.credit_score)

        excess_days = days_since_last - self.INACTIVITY_THRESHOLD_DAYS
        decay = min(self.MAX_DECAY, excess_days * self.DECAY_PER_DAY)

        # 向均值回归
        if credit.credit_score > self.MEAN_SCORE:
            return max(self.MEAN_SCORE, credit.credit_score - decay)
        elif credit.credit_score < self.MEAN_SCORE:
            return min(self.MEAN_SCORE, credit.credit_score + decay)
        return float(credit.credit_score)

    # ========== 会话级临时信用 ==========

    def begin_session(self, user_id: str) -> SessionCredit:
        """
        会话开始：创建临时信用

        临时信用 = 基础信用 - 5（给容错空间但不无限纵容）
        后续所有会话内的判断都基于临时信用
        """
        credit = self.store.get_or_create(user_id)
        # 先应用时间衰减
        effective = self._apply_time_decay(credit)

        with self._sess_lock:
            sess_credit = SessionCredit(
                base_score=int(effective),
                penalty=self.SESSION_PENALTY,
            )
            self._sessions[user_id] = sess_credit
            return sess_credit

    def get_session_credit(self, user_id: str) -> SessionCredit:
        """获取当前会话的临时信用（不存在则创建）"""
        with self._sess_lock:
            if user_id not in self._sessions:
                return self.begin_session(user_id)
            return self._sessions[user_id]

    def end_session(self, user_id: str, session_was_clean: bool):
        """
        会话结束：将临时信用的变化回写到基础信用

        回写策略：
        - 会话干净 → 基础信用 +2（最多到100）
        - 会话有垃圾 → 基础信用 -5
        - 临时信用的最终值也影响回写幅度
        """
        credit = self.store.get_or_create(user_id)

        with self._sess_lock:
            sess = self._sessions.pop(user_id, None)

        if sess is None:
            # 没有会话记录，走旧逻辑
            if session_was_clean:
                credit.credit_score = min(100, credit.credit_score + 2)
                credit.consecutive_good += 1
                credit.consecutive_bad = 0
                credit.valid_sessions += 1
            else:
                credit.credit_score = max(0, credit.credit_score - 5)
                credit.consecutive_good = 0
                credit.consecutive_bad += 1
                credit.fused_sessions += 1
        else:
            # 新逻辑：基于临时信用变化回写
            temp_change = sess.temp_score - (sess.base_score - sess.penalty)

            if session_was_clean:
                # 会话干净 → 基础 +2，但临时信用跌太多也要扣
                base_delta = 2
                if temp_change < -10:
                    base_delta = -2  # 虽然最终干净，但过程中跌太多
            else:
                # 会话有问题 → 基础 -5，临时跌更多就多扣
                base_delta = -5
                if temp_change < -20:
                    base_delta = -8  # 临时信用崩了，多扣点

            credit.credit_score = max(0, min(100, credit.credit_score + base_delta))

            if session_was_clean:
                credit.consecutive_good += 1
                credit.consecutive_bad = 0
                credit.valid_sessions += 1
            else:
                credit.consecutive_good = 0
                credit.consecutive_bad += 1
                credit.fused_sessions += 1

        credit.total_sessions += 1
        credit.last_seen = time.time()
        credit.last_session_end = time.time()
        self.store.save(credit)

    # ========== 会话内事件处理 ==========

    def on_garbage_input(self, user_id: str) -> dict:
        """
        会话内垃圾输入处理 v2.1

        返回 dict 包含：
        - temp_score: 当前临时信用
        - immediate_downgrade: 是否触发立即降权
        - streak: 当前连续垃圾计数

        逻辑：
        - 每次垃圾：临时信用 -1（基础信用不动）
        - 连续3次垃圾：额外 -15（立即降权）
        - 连续5次：标记建议熔断
        """
        sess = self.get_session_credit(user_id)
        sess.garbage_streak += 1
        sess.good_streak = 0

        result = {
            "temp_score": sess.temp_score,
            "immediate_downgrade": False,
            "streak": sess.garbage_streak,
            "suggest_fuse": False,
        }

        # 每次垃圾 -1
        sess.temp_score = max(0, sess.temp_score - 1)

        # 连续3次 → 立即降权
        if sess.garbage_streak >= self.GARBAGE_STREAK_LIMIT:
            sess.temp_score = max(0, sess.temp_score - self.STREAK_PENALTY)
            result["immediate_downgrade"] = True

        # 连续5次 → 建议熔断
        if sess.garbage_streak >= 5:
            result["suggest_fuse"] = True

        result["temp_score"] = sess.temp_score
        return result

    def on_valid_input(self, user_id: str):
        """
        会话内正常输入：临时信用微升，连续计数+1
        基础信用只加 0.5（缓慢建立信任）
        """
        sess = self.get_session_credit(user_id)
        sess.good_streak += 1
        sess.garbage_streak = 0  # 重置连续垃圾

        # 临时信用恢复（但不会超过基础分）
        cap = sess.base_score + 5  # 最多比基础分高5
        sess.temp_score = min(cap, sess.temp_score + 1)

        # 基础信用微升
        credit = self.store.get_or_create(user_id)
        credit.credit_score = min(100, credit.credit_score + 0.5)
        self.store.save(credit)

    def on_clarification(self, user_id: str):
        """HIGH 路径追问计数"""
        sess = self.get_session_credit(user_id)
        sess.clarifications += 1
        return sess.clarifications

    # ========== 熔断阈值（基于临时信用） ==========

    def get_fuse_threshold(self, user_id: str) -> int:
        """
        v2.1：基于临时信用判断阈值

        临时信用低 → 阈值低（更容易熔断）
        临时信用高 → 阈值高（更宽容）
        这解决了"85分要扣55次才掉出Trusted"的问题
        """
        sess = self.get_session_credit(user_id)
        temp = sess.temp_score

        if temp >= 85:
            return 8    # 高信用：容忍8次
        elif temp >= 70:
            return 5    # 中高信用：容忍5次
        elif temp >= 50:
            return 3    # 中等信用：容忍3次
        elif temp >= 30:
            return 2    # 低信用：容忍2次
        else:
            return 1    # 极低信用：1次就熔断

    # ========== 耐心恢复速率 ==========

    def get_decay_rate(self, user_id: str) -> int:
        sess = self.get_session_credit(user_id)
        temp = sess.temp_score
        if temp >= 70:
            return 20
        elif temp >= 50:
            return 16
        elif temp >= 30:
            return 10
        else:
            return 5

    # ========== 怒气增量 ==========

    def get_anger_increment(self, user_id: str) -> int:
        sess = self.get_session_credit(user_id)
        temp = sess.temp_score
        if temp >= 70:
            return 10
        elif temp >= 50:
            return 20
        elif temp >= 30:
            return 30
        else:
            return 40

    # ========== 熔断提示 ==========

    def get_fuse_message(self, user_id: str) -> str:
        sess = self.get_session_credit(user_id)
        temp = sess.temp_score

        if temp >= 70:
            messages = [
                "稍等，我这边重新连接一下…好了，继续聊。",
            ]
        elif temp >= 50:
            messages = [
                "刚才网络有点问题？恢复了咱们继续。",
                "嗯…好了，刚才断了一下，接着说。",
            ]
        elif temp >= 30:
            messages = [
                "咱俩先歇一下，好了再聊。",
                "检测到异常输入较多，请检查麦克风后重试。",
            ]
        else:
            messages = [
                "检测到异常输入较多，请检查麦克风或网络后重新发起会话。",
                "输入内容无法识别，请确认设备正常后再次尝试。",
            ]

        import random
        return random.choice(messages)


# ---------- 演示 ----------
if __name__ == "__main__":
    import tempfile, os
    tmpdir = tempfile.mkdtemp()
    store_path = os.path.join(tmpdir, "credit.json")
    store = CreditStore(store_path)
    engine = TrustEngine(store)

    print("\n  ┌─────────────────────────────────────────────┐")
    print("  │  Trust Tier v2.1 Demo                      │")
    print("  └─────────────────────────────────────────────┘")

    # 模拟：Trusted 用户（基础85分）设备坏了，连续输入乱码
    uid = "trusted_user_device_broken"
    store.get_or_create(uid).credit_score = 85

    print(f"\n  📌 场景：Trusted用户(85分)设备坏了，连续乱码")
    print(f"  {'─'*50}")

    # 会话开始
    sess = engine.begin_session(uid)
    print(f"  会话开始: 基础={sess.base_score} 临时={sess.temp_score} (扣了{sess.penalty}分惩罚)")

    for i in range(6):
        result = engine.on_garbage_input(uid)
        marker = " ⚠️ 立即降权!" if result["immediate_downgrade"] else ""
        marker += " 🔴 建议熔断!" if result["suggest_fuse"] else ""
        print(f"  垃圾#{i+1}: 临时信用={result['temp_score']:3d} "
              f"连续={result['streak']}{marker}")

    # 查看当前阈值
    threshold = engine.get_fuse_threshold(uid)
    print(f"\n  当前熔断阈值: {threshold}（临时信用低→阈值低）")

    # 模拟：正常用户长期使用
    print(f"\n  📌 场景：时间衰减测试")
    bob = store.get_or_create("bob_longterm")
    bob.credit_score = 85
    bob.last_session_end = time.time() - (60 * 86400)  # 60天前
    score = engine.get_credit_score("bob_longterm")
    print(f"  Bob 60天没来: 85 → {score:.0f}（衰减回归均值50）")

    bob2 = store.get_or_create("bob_recent")
    bob2.credit_score = 85
    bob2.last_session_end = time.time() - (5 * 86400)  # 5天前
    score2 = engine.get_credit_score("bob_recent")
    print(f"  Bob 5天没来: 85 → {score2:.0f}（未超30天阈值，不衰减）")

    # 清理
    os.remove(store_path)
    os.rmdir(tmpdir)
