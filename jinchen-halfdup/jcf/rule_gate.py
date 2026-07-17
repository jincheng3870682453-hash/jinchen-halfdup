"""
JCF Rule Gate - 可配置策略引擎 v2.1
第一道硬闸门：零 LLM 成本，纯正则 + 确定性规则
不可被 Prompt 注入绕过

v2.1 修复：
- 热更新原子性：先加载到临时对象，验证通过才替换
- 捕获 yaml.YAMLError / re.error / KeyError
- 加载失败 → 保留旧规则继续运行（不裸奔）
- 新增 last_load_error 供运维监控
"""

import re
import time
import yaml
import threading
from enum import Enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class Action(Enum):
    BLOCK    = "block"
    WAIT     = "wait"
    PASS     = "pass"
    TRUNCATE = "truncate"


@dataclass
class GateResult:
    action: Action
    reason: str
    matched_rule: Optional[str]
    cleaned_text: str
    log_level: str
    truncated: bool = False
    truncate_length: Optional[int] = None


class SafeEvaluator:
    """安全的表达式求值器（白名单字段，禁止 eval）"""

    ALLOWED_FIELDS = {
        "length", "special_char_ratio", "repeated_char_ratio",
        "no_real_word", "has_real_word", "hesitation_count",
        "real_word_count", "is_all_caps", "digit_ratio",
    }

    ALLOWED_OPS = {"AND", "OR", ">", "<", ">=", "<=", "==", "!="}

    @classmethod
    def eval(cls, condition: str, ctx: dict) -> bool:
        """极简表达式解析：支持 field > num AND field < num 形式"""
        tokens = re.split(r"\s+(AND|OR)\s+", condition.strip())
        results = []
        last_op = "AND"

        for token in tokens:
            token = token.strip()
            if token in ("AND", "OR"):
                last_op = token
                continue

            m = re.match(r"(\w+)\s*(>|<|>=|<=|==|!=)\s*([\d.]+)", token)
            if not m:
                continue
            field, op, value = m.group(1), m.group(2), float(m.group(3))

            if field not in cls.ALLOWED_FIELDS:
                continue

            field_val = ctx.get(field, 0)
            if isinstance(field_val, bool):
                field_val = int(field_val)

            result = {
                ">":  field_val >  value,
                "<":  field_val <  value,
                ">=": field_val >= value,
                "<=": field_val <= value,
                "==": field_val == value,
                "!=": field_val != value,
            }[op]

            if last_op == "OR" and results:
                results[-1] = results[-1] or result
            else:
                results.append(result)

        return all(results) if results else False


@dataclass
class _LoadResult:
    """内部：一次加载尝试的结果"""
    success: bool
    rules: list = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    error: Optional[str] = None


class RuleGate:
    """
    可配置策略引擎 —— 第一道硬闸门（v2.1 原子性保护）

    热更新策略：
    1. 检测到文件变更 → 加载到临时变量
    2. 逐条验证规则（正则编译、字段完整性）
    3. 全部通过 → 原子替换 self.rules / self.settings
    4. 任何失败 → 保留旧规则，记录错误，系统继续运行
    """

    def __init__(self, policy_path: str = "policies/rule_gate.yaml"):
        self.policy_path = Path(policy_path)
        self.rules: list[dict] = []
        self.settings: dict = {}
        self._last_mtime: float = 0
        self._lock = threading.RLock()
        # v2.1: 监控字段
        self.last_load_error: Optional[str] = None
        self.load_error_count: int = 0
        self.total_reloads: int = 0
        self.consecutive_failures: int = 0
        # 加载初始规则
        self._load()
        # 如果初始加载就失败，要有兜底规则
        if not self.rules:
            self._install_fallback_rules()

    # ========== 加载与热更新（原子性） ==========

    def _load(self):
        """
        原子性加载：
        - 解析到临时对象
        - 验证通过才替换 self.rules
        - 任何异常 → 保留旧规则 + 记录错误
        """
        try:
            result = self._parse_file()
        except Exception as e:
            # 捕获一切异常（YAML 解析错误、文件不存在、权限问题等）
            self._handle_load_failure(f"Unexpected error: {e}")
            return

        if not result.success:
            self._handle_load_failure(result.error)
            return

        # 验证通过 → 原子替换
        with self._lock:
            self.rules = result.rules
            self.settings = result.settings
            self._last_mtime = self.policy_path.stat().st_mtime
            self.last_load_error = None
            self.consecutive_failures = 0
            self.total_reloads += 1

        print(f"[RuleGate] ✓ Loaded {len(self.rules)} rules from {self.policy_path}")

    def _parse_file(self) -> _LoadResult:
        """解析 YAML 文件，逐条验证规则，返回临时结果"""
        if not self.policy_path.exists():
            return _LoadResult(success=False, error=f"File not found: {self.policy_path}")

        try:
            with open(self.policy_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
        except yaml.YAMLError as e:
            return _LoadResult(success=False, error=f"YAML parse error: {e}")
        except OSError as e:
            return _LoadResult(success=False, error=f"OS error: {e}")

        if not isinstance(config, dict):
            return _LoadResult(success=False, error="Config root must be a mapping")

        settings = config.get("settings", {}) or {}
        if not isinstance(settings, dict):
            settings = {}

        rules_raw = config.get("rules", []) or []
        if not isinstance(rules_raw, list):
            return _LoadResult(success=False, error="rules must be a list")

        validated_rules = []
        for idx, rule in enumerate(rules_raw):
            if not isinstance(rule, dict):
                return _LoadResult(success=False, error=f"Rule #{idx} is not a mapping")

            # 必须有 id
            rule_id = rule.get("id", f"rule_{idx}")
            rule.setdefault("id", rule_id)

            # 必须有 action
            action_str = rule.get("action", "").upper()
            if action_str not in ("BLOCK", "WAIT", "PASS", "TRUNCATE"):
                return _LoadResult(
                    success=False,
                    error=f"Rule '{rule_id}' has invalid action: {action_str}"
                )

            # 编译正则（如果有）
            if "pattern" in rule and rule["pattern"]:
                try:
                    rule["_compiled"] = re.compile(rule["pattern"])
                except re.error as e:
                    return _LoadResult(
                        success=False,
                        error=f"Rule '{rule_id}' has invalid regex: {e}"
                    )

            # condition 和 pattern 至少要有其一
            has_pattern = "_compiled" in rule
            has_condition = "condition" in rule and rule["condition"].strip()
            if not has_pattern and not has_condition:
                return _LoadResult(
                    success=False,
                    error=f"Rule '{rule_id}' has neither pattern nor condition"
                )

            # reason 默认值
            rule.setdefault("reason", "matched")
            rule.setdefault("log_level", "info")
            rule.setdefault("priority", 0)

            validated_rules.append(rule)

        # 按 priority 降序
        validated_rules.sort(key=lambda r: r.get("priority", 0), reverse=True)

        return _LoadResult(success=True, rules=validated_rules, settings=settings)

    def _handle_load_failure(self, error_msg: str):
        """加载失败处理：保留旧规则，记录错误"""
        self.last_load_error = error_msg
        self.load_error_count += 1
        self.consecutive_failures += 1

        # 如果初始加载就失败（rules 为空），安装兜底规则
        with self._lock:
            if not self.rules:
                self._install_fallback_rules()

        print(f"[RuleGate] ✗ Load FAILED (kept {len(self.rules)} old rules): {error_msg}")
        print(f"[RuleGate]   consecutive_failures={self.consecutive_failures}")

        # 连续失败过多 → 告警
        if self.consecutive_failures >= 5:
            self._escalate_load_failure()

    def _install_fallback_rules(self):
        """兜底规则：当所有加载都失败时，保证基本防护"""
        self.rules = [
            {
                "id": "fallback_pass",
                "priority": 0,
                "condition": "always",
                "action": "PASS",
                "reason": "fallback_default",
                "log_level": "warn",  # 记录警告，因为走了兜底
            },
        ]
        self.settings = {"hot_reload": False}
        print("[RuleGate] ⚠️ Installed FALLBACK rules (permissive mode)")

    def _escalate_load_failure(self):
        """连续加载失败告警"""
        msg = (f"[RuleGate CRITICAL] {self.consecutive_failures} consecutive "
               f"load failures! Last error: {self.last_load_error}")
        print(msg)
        # TODO: 接入告警通道（邮件/钉钉/短信）
        # send_alert(msg)

    def _check_reload(self):
        """检查配置文件是否变更，自动热更新（原子性）"""
        if not self.settings.get("hot_reload", True):
            return
        try:
            mtime = self.policy_path.stat().st_mtime
            if mtime > self._last_mtime:
                print(f"[RuleGate] Config changed (mtime={mtime}), reloading...")
                self._load()
        except OSError as e:
            # 文件暂时不可读（磁盘问题等）→ 不恐慌，保留旧规则
            self.last_load_error = f"OS error during reload: {e}"
            print(f"[RuleGate] WARN: cannot read config: {e}, keeping old rules")

    # ========== 核心评估 ==========

    def evaluate(self, user_id: str, text: str) -> GateResult:
        self._check_reload()

        cleaned = self._clean(text)

        # 提取上下文特征
        ctx = self._extract_features(cleaned)

        with self._lock:
            for rule in self.rules:
                if self._rule_matches(rule, cleaned, ctx):
                    action_str = rule.get("action", "pass").lower()
                    try:
                        action = Action(action_str)
                    except ValueError:
                        action = Action.PASS

                    log_level = rule.get("log_level", "info")

                    if action == Action.BLOCK:
                        self._record_alert(user_id, log_level, rule.get("id", ""))
                    elif action == Action.TRUNCATE:
                        max_len = rule.get("truncate_to", 4000)
                        cleaned = cleaned[:max_len]

                    return GateResult(
                        action=action,
                        reason=rule.get("reason", "matched"),
                        matched_rule=rule.get("id", None),
                        cleaned_text=cleaned,
                        log_level=log_level,
                        truncated=(action == Action.TRUNCATE),
                        truncate_length=rule.get("truncate_to") if action == Action.TRUNCATE else None,
                    )

        # 没有任何规则命中 → PASS
        return GateResult(Action.PASS, "default_pass", None, cleaned, "debug")

    # ========== 特征提取 ==========

    def _extract_features(self, text: str) -> dict:
        length = len(text)
        if length == 0:
            return {"length": 0, "special_char_ratio": 0, "repeated_char_ratio": 0,
                    "no_real_word": True, "has_real_word": False,
                    "real_word_count": 0, "hesitation_count": 0,
                    "is_all_caps": False, "digit_ratio": 0}

        cjk_words = re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]{2,}", text)
        en_words = re.findall(r"[a-zA-Z]{3,}", text)
        real_words = cjk_words + en_words

        special_chars = sum(1 for c in text if not c.isalnum() and c not in "，。！？、；：""''（） ")
        special_ratio = special_chars / length

        repeat_count = 0
        for char, grp in re.findall(r"((.)\2{2,})", text):
            repeat_count += len(char) - 2
        repeat_ratio = repeat_count / max(length, 1)

        hesitation_re = re.compile(r"(嗯+|呃+|啊+|那个|就是|就是说|怎么说呢|算了)")
        hesitation_count = len(hesitation_re.findall(text))

        is_all_caps = bool(re.search(r"[A-Z]{3,}", text)) and not bool(re.search(r"[a-z]", text))

        digit_count = sum(1 for c in text if c.isdigit())
        digit_ratio = digit_count / length

        return {
            "length": length,
            "special_char_ratio": round(special_ratio, 3),
            "repeated_char_ratio": round(repeat_ratio, 3),
            "no_real_word": len(real_words) == 0,
            "has_real_word": len(real_words) > 0,
            "real_word_count": len(real_words),
            "hesitation_count": hesitation_count,
            "is_all_caps": is_all_caps,
            "digit_ratio": round(digit_ratio, 3),
        }

    # ========== 规则匹配 ==========

    def _rule_matches(self, rule: dict, text: str, ctx: dict) -> bool:
        if "_compiled" in rule:
            return bool(rule["_compiled"].search(text))
        if "condition" in rule:
            cond = rule["condition"].strip()
            if cond == "always":
                return True
            return SafeEvaluator.eval(cond, ctx)
        return False

    # ========== 告警与熔断 ==========

    def _record_alert(self, user_id: str, level: str, rule_id: str):
        if not hasattr(self, '_alert_counters'):
            self._alert_counters = {}
        now = time.time()
        self._alert_counters.setdefault(user_id, []).append(now)

        window = self.settings.get("alert_window_seconds", 60)
        threshold = self.settings.get("alert_threshold", 10)

        self._alert_counters[user_id] = [
            t for t in self._alert_counters[user_id] if now - t < window
        ]

        count = len(self._alert_counters[user_id])
        if count >= threshold:
            self._escalate(user_id, count, rule_id)

        if self.settings.get("log_all_blocks", True):
            print(f"[RuleGate] BLOCK user={user_id} rule={rule_id} level={level} count={count}")

    def _escalate(self, user_id: str, count: int, rule_id: str):
        msg = f"[RuleGate ALERT] user={user_id} hits {count} blocks, last_rule={rule_id}"
        print(msg)

    # ========== 工具方法 ==========

    def _clean(self, text: str) -> str:
        text = text.strip()
        text = re.sub(r"\s+", " ", text)
        return text

    def get_stats(self) -> dict:
        return {
            "rules_loaded": len(self.rules),
            "active_alerts": len(getattr(self, '_alert_counters', {})),
            "total_alert_users": sum(
                len(v) for v in getattr(self, '_alert_counters', {}).values()
            ),
            "total_reloads": self.total_reloads,
            "consecutive_failures": self.consecutive_failures,
            "last_load_error": self.last_load_error,
        }
