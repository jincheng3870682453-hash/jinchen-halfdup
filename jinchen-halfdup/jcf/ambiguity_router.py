"""
JCF Ambiguity Router - 模糊度分流器
在 Rule Gate 之后、AI Sentinel 之前
用确定性规则判断输入模糊度，三路分流
"""

import re
from enum import Enum
from dataclasses import dataclass
from typing import Optional


class AmbiguityLevel(Enum):
    GIBBERISH = "gibberish"   # 纯乱码/噪声 → 模板吐槽
    HIGH      = "high"         # 高模糊·像人话但不完整 → 轻量追问
    LOW       = "low"          # 低模糊·语义清晰 → AI Sentinel → LLM


@dataclass
class RouteResult:
    level: AmbiguityLevel
    reason: str
    confidence: float  # 0-1, 判断置信度
    suggested_action: str  # 给人看的说明


class AmbiguityRouter:
    """
    模糊度分流器

    三路：
    - GIBBERISH → 零成本模板吐槽
    - HIGH       → 小模型轻量追问（"你是说...吗？"）
    - LOW        → 正常流程（AI Sentinel → Weight Router → LLM）
    """

    # 常见英文实词词典（用于区分"有意义的英文"和"随机字母串"）
    _COMMON_EN_WORDS = frozenset([
        "the", "and", "you", "for", "are", "but", "not", "can", "all",
        "any", "how", "man", "new", "now", "our", "out", "say", "she",
        "too", "use", "will", "with", "have", "this", "that", "they",
        "what", "when", "where", "which", "who", "why", "would", "could",
        "should", "about", "after", "again", "below", "could", "every",
        "from", "great", "where", "write", "because", "through", "during",
        "before", "always", "around", "between", "people", "please",
        "weather", "tomorrow", "today", "yesterday", "hangzhou", "beijing",
        "shanghai", "shenzhen", "guangzhou", "chengdu", "hello", "world",
        "code", "function", "class", "import", "return", "print", "input",
        "query", "search", "check", "find", "look", "tell", "show", "give",
        "make", "take", "help", "need", "want", "think", "know", "feel",
        "speak", "talk", "listen", "hear", "see", "watch", "read", "write",
        "good", "bad", "nice", "great", "terrible", "awesome", "amazing",
        "project", "computer", "internet", "website", "application", "system",
        "data", "information", "result", "answer", "question", "problem",
        "solution", "method", "approach", "strategy", "plan", "design",
    ])

    def __init__(self):
        # 中文实词（2字滑动窗口 + 3字组合）
        self._cjk_word_re = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]{2,}")
        # 英文 token（2字以上字母串）
        self._en_token_re = re.compile(r"[a-zA-Z]{2,}")
        # 犹豫/填充词
        self._hesitation_re = re.compile(
            r"(嗯+|呃+|啊+|哦+|那个|就是|就是说|怎么说呢|"
            r"算了吧|其实吧|怎么说|那个啥|你懂的|对了|哦对|嗯嗯|啊啊)"
        )
        # 标点符号（非字母数字非汉字）
        self._punct_re = re.compile(r"[^\w\u4e00-\u9fff\u3400-\u4dbf]")
        # 随机字母串检测（辅音簇：3个以上连续辅音）
        self._consonant_cluster = re.compile(r"[bcdfghjklmnpqrstvwxyz]{4,}", re.I)

    def _is_meaningful_en(self, token: str) -> bool:
        """判断英文 token 是否像有意义的词（而非随机字母）"""
        token_lower = token.lower()

        # 太短（2字母）→ 不算实词
        if len(token_lower) < 3:
            return False

        # 在常见词表中
        if token_lower in self._COMMON_EN_WORDS:
            return True

        # 必须同时包含元音和辅音才算像英文词
        has_vowel = bool(re.search(r"[aeiou]", token_lower))
        has_consonant = bool(re.search(r"[bcdfghjklmnpqrstvwxyz]", token_lower))

        if not has_vowel or not has_consonant:
            return False

        # 元音占比应在 20%-60% 之间（真实英文单词的特征）
        vowel_count = len(re.findall(r"[aeiou]", token_lower))
        vowel_ratio = vowel_count / len(token_lower)

        # 真实英文词通常有合理的元音比例
        if 0.2 <= vowel_ratio <= 0.6:
            return True

        # 长度 <= 4 的短词，有元音就接受
        if len(token_lower) <= 4:
            return True

        return False

    def route(self, text: str) -> RouteResult:
        if not text or not text.strip():
            return RouteResult(
                level=AmbiguityLevel.GIBBERISH,
                reason="empty_input",
                confidence=1.0,
                suggested_action="wait_for_more_input",
            )

        text = text.strip()
        char_count = len(text)

        # ===== 特征提取 =====

        # 中文实词（用2字滑动窗口提取，排除犹豫词区域）
        # 先标记犹豫词在文本中的位置
        hesitation_spans = []
        for m in self._hesitation_re.finditer(text):
            hesitation_spans.append((m.start(), m.end()))

        def _in_hesitation(pos: int) -> bool:
            for s, e in hesitation_spans:
                if s <= pos < e:
                    return True
            return False

        cjk_chars = list(re.finditer(r"[\u4e00-\u9fff\u3400-\u4dbf]", text))
        # 只取不在犹豫词区域的汉字
        real_cjk = [m.group() for m in cjk_chars if not _in_hesitation(m.start())]

        cjk_bigrams = []
        for i in range(len(real_cjk) - 1):
            cjk_bigrams.append(real_cjk[i] + real_cjk[i+1])
        cjk_trigrams = []
        for i in range(len(real_cjk) - 2):
            cjk_trigrams.append(real_cjk[i] + real_cjk[i+1] + real_cjk[i+2])
        cjk_all = set(cjk_bigrams) | set(cjk_trigrams)
        cjk_word_count = len(cjk_all)

        # 英文 token → 过滤掉随机字母串
        en_all = self._en_token_re.findall(text)
        en_meaningful = [t for t in en_all if self._is_meaningful_en(t)]
        en_gibberish = [t for t in en_all if not self._is_meaningful_en(t)]

        # 实词总数（中文词 + 有意义的英文词）
        real_word_count = cjk_word_count + len(en_meaningful)

        # 犹豫词
        hesitation_matches = self._hesitation_re.findall(text)
        hesitation_count = len(hesitation_matches)

        # 纯乱码英文 token 占比
        gibberish_en_ratio = len(en_gibberish) / max(len(en_all), 1)

        # 噪声字符（标点+空格+特殊符号）
        noise_chars = len(self._punct_re.findall(text)) + text.count(" ")
        noise_ratio = noise_chars / max(char_count, 1)

        # ===== 分流决策 =====

        # ★ 规则 1：无任何实词 → GIBBERISH
        if real_word_count == 0:
            return RouteResult(
                level=AmbiguityLevel.GIBBERISH,
                reason="no_real_words",
                confidence=0.95,
                suggested_action="template_roast_or_wait",
            )

        # ★ 规则 2：英文乱码占比高 → GIBBERISH
        if gibberish_en_ratio > 0.5 and len(en_all) >= 2:
            return RouteResult(
                level=AmbiguityLevel.GIBBERISH,
                reason="gibberish_english",
                confidence=0.9,
                suggested_action="template_roast",
            )

        # ★ 规则 3：噪声占比极高 + 实词极少 → GIBBERISH
        if noise_ratio > 0.6 and real_word_count <= 1:
            return RouteResult(
                level=AmbiguityLevel.GIBBERISH,
                reason="high_noise_ratio",
                confidence=0.85,
                suggested_action="template_roast",
            )

        # ★ 规则 4：实词极少(≤1)且有犹豫词 → HIGH
        if real_word_count <= 1 and hesitation_count >= 1:
            return RouteResult(
                level=AmbiguityLevel.HIGH,
                reason="hesitation_with_few_words",
                confidence=0.85,
                suggested_action="light_model_clarify",
            )

        # ★ 规则 5：实词只有 1 个 → HIGH（信息不足）
        if real_word_count == 1:
            return RouteResult(
                level=AmbiguityLevel.HIGH,
                reason="single_word_input",
                confidence=0.8,
                suggested_action="light_model_ask_more",
            )

        # ★ 规则 6：犹豫词多 + 实词不多 → HIGH
        if hesitation_count >= 2 and real_word_count < 5:
            return RouteResult(
                level=AmbiguityLevel.HIGH,
                reason="many_hesitations",
                confidence=0.75,
                suggested_action="light_model_clarify",
            )

        # ★ 规则 7：实词 >= 3 且犹豫词 <= 1 → LOW（语义清晰）
        if real_word_count >= 3 and hesitation_count <= 1:
            return RouteResult(
                level=AmbiguityLevel.LOW,
                reason="clear_semantics",
                confidence=0.9,
                suggested_action="proceed_to_sentinel",
            )

        # ★ 规则 8：实词 >= 5 → LOW（信息充足）
        if real_word_count >= 5:
            return RouteResult(
                level=AmbiguityLevel.LOW,
                reason="rich_content",
                confidence=0.85,
                suggested_action="proceed_to_sentinel",
            )

        # ★ 默认：犹豫词多但实词也够 → HIGH
        if hesitation_count >= 2:
            return RouteResult(
                level=AmbiguityLevel.HIGH,
                reason="hesitant_but_substantive",
                confidence=0.7,
                suggested_action="light_model_confirm",
            )

        # 兜底 → LOW
        return RouteResult(
            level=AmbiguityLevel.LOW,
            reason="default_low",
            confidence=0.6,
            suggested_action="proceed_to_sentinel",
        )

    # ---------- 辅助：生成追问话术 ----------

    def generate_clarify_prompt(self, text: str, result: RouteResult) -> str:
        """为高模糊输入生成追问提示"""
        templates = {
            "hesitation_with_few_words": "你是想说…？我猜一下，你说的是「{snippet}」吗？",
            "single_word_input": "嗯…「{snippet}」是啥？再说具体点？",
            "many_hesitations": "慢慢来，你想问的是关于「{snippet}」的事吗？",
            "hesitant_but_substantive": "我大概懂了，你是说「{snippet}」对吧？",
            "insufficient_content": "嗯…再多说一点？我没太听清。",
        }
        tpl = templates.get(result.reason, "嗯…你说的是？")
        # 取第一个实词做 snippet
        cjk = self._cjk_word_re.findall(text)
        snippet = cjk[0] if cjk else text[:6]
        return tpl.format(snippet=snippet)


# ---------- 快速测试 ----------
if __name__ == "__main__":
    router = AmbiguityRouter()
    tests = [
        "asdfghjkl @@@",
        "嗯…那个…就是…",
        "我想问一下昨天那个项目",
        "呃…山河吃饭…哦对，那个AI项目",
        "@@@@@!!!",
        "今天杭州天气怎么样",
        "嗯…那个…怎么说呢…就是上次…你懂的",
        "",
        "啊啊啊啊",
        "帮我查一下明天北京的天气预报，要带伞吗",
        "asdf fghjkl qwerty",
        "hello world",
    ]
    for t in tests:
        r = router.route(t)
        print(f"  [{r.level.value:>10}] {t!r:45s} → {r.reason} (conf={r.confidence})")
