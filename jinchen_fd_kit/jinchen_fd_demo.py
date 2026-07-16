"""
jinchen_fd_demo.py
==================
全双工「半句抢答 + 语气词 + 猜错容错」最小可运行 Demo
纯标准库，Python 3.10+ 直接跑：python jinchen_fd_demo.py

设计思路（对应之前聊的三层架构）：
- 第一层（耳朵）：模拟 ASR 流式出字，每打几个字就触发一次
- 第二层（调度员）：用关键词+记忆匹配，判断是否抢答、是否猜错
- 第三层（嘴）：生成带语气词的回复，猜错就「慌一下」

没有任何第三方依赖，Archive 记忆用 dict 模拟，Rollback 用 list 记录。
"""

import random
import time
from dataclasses import dataclass, field
from typing import Optional


# ============================================================
# 第一层：模拟 ASR 流式出字（耳朵）
# ============================================================
def asr_stream(full_text: str, chunk_size: int = 4):
    """模拟 ASR 一点点往外冒字，每次吐 chunk_size 个字"""
    for i in range(0, len(full_text), chunk_size):
        yield full_text[: i + chunk_size]


# ============================================================
# Archive：记忆库（对应 jinchen 的 SimHash 裁剪，这里用 dict 简化）
# ============================================================
@dataclass
class Memory:
    topic: str          # 话题关键词
    detail: str         # 具体记忆
    weight: float = 1.0 # 权重，猜错会降，猜对会升


class Archive:
    """极简记忆库：存话题记忆，按权重排序取最相关的"""

    def __init__(self):
        self.memories: list[Memory] = [
            Memory("奶茶", "上次路口那家芋泥奶茶卖光了，很郁闷", 1.5),
            Memory("猫", "想养一只橘猫，叫小芋", 1.2),
            Memory("代码", "写 index.js 第23行总少个分号", 1.0),
            Memory("下雨", "这周杭州一直下雨，烦死了", 0.8),
        ]

    def search(self, text: str) -> Optional[Memory]:
        """简单关键词匹配，返回权重最高的相关记忆"""
        matched = [m for m in self.memories if m.topic in text]
        if not matched:
            return None
        return max(matched, key=lambda m: m.weight)

    def reward(self, topic: str):
        for m in self.memories:
            if m.topic == topic:
                m.weight += 0.3

    def punish(self, topic: str):
        for m in self.memories:
            if m.topic == topic:
                m.weight = max(0.3, m.weight - 0.5)


# ============================================================
# 第二层：调度员（DM）—— 判断是否抢答、猜没猜对
# ============================================================
class Dispatcher:
    """模拟 0.5B 小 LLM 调度员：吞 ASR 半句话，决定抢不抢、对不对"""

    FILLERS_THINK = ["嗯…", "呃…", "啊…"]
    FILLERS_REALIZE = ["哦！", "啊对对对！", "诶——！"]
    FILLERS_PANIC = ["啊？！", "不对不对！", "呃啊我搞错了！"]

    def __init__(self, archive: Archive):
        self.archive = archive
        self.last_guess: Optional[str] = None
        self.last_topic: Optional[str] = None
        self.last_guess_text: Optional[str] = None  # 去重：上次抢答的内容
        self.cooldown: int = 0  # 冷却计数器，避免连着抢同一句

        # ---- 可调参数（auto_tune.py 会改写这些）----
        self.PARTIAL_LEN = 5        # ASR 几个字触发一次
        self.MIN_LEN = 5            # 低于此长度只出语气词
        self.FILLER_PROB = 0.5      # 语气词出现概率
        self.CONF_THRESH = 0.7       # 抢答置信度阈值（占位，关键词匹配暂未用）
        self.COOLDOWN = 2            # 抢答后冷却 chunk 数
        self.MAX_GUESS_LEN = 40      # 抢答文本最大长度（防啰嗦）

    def on_partial(self, partial_text: str) -> Optional[dict]:
        """
        吞到 ASR 半句话，返回一个「动作」或 None（继续听）
        动作类型：抢答 / 语气词过渡
        """
        # 太短不抢，先来个语气词过渡
        if len(partial_text) < self.MIN_LEN:
            # 按概率决定是否插语气词（避免每条短 chunk 都嘟囔）
            if random.random() < self.FILLER_PROB:
                return {"type": "filler", "text": random.choice(self.FILLERS_THINK)}
            return None

        # 用 Archive 搜相关记忆，有就抢答
        mem = self.archive.search(partial_text)
        if mem:
            self.last_topic = mem.topic
            guess = self._make_guess(mem)

            # 去重 + 冷却：同样的话不连着喊
            if guess == self.last_guess_text and self.cooldown > 0:
                self.cooldown -= 1
                return None
            self.last_guess_text = guess
            self.cooldown = self.COOLDOWN  # 接下来 N 个 chunk 不再重复抢
            self.last_guess = guess
            return {
                "type": "guess",
                "text": f"{random.choice(self.FILLERS_REALIZE)} {guess}",
                "topic": mem.topic,
            }

        # 没匹配到记忆，给个语气词拖一下
        return {"type": "filler", "text": random.choice(self.FILLERS_THINK)}

    def on_final(self, full_text: str) -> dict:
        """
        用户说完整句话了，判断之前猜的对不对
        """
        if not self.last_guess:
            return {"type": "normal_reply", "text": self._generic_reply(full_text)}

        # 简单判定：完整句里包含猜测的关键内容就算对
        topic = self.last_topic or ""
        if topic in full_text:
            self.archive.reward(topic)
            return {
                "type": "guess_correct",
                "text": f"哈哈我就知道！{self._generic_reply(full_text)}",
            }
        else:
            # 猜错了 —— 慌一下！
            self.archive.punish(topic)
            return {
                "type": "guess_wrong",
                "text": (
                    f"{random.choice(self.FILLERS_PANIC)} "
                    f"我听岔了！你说的是「{full_text}」对吧，"
                    f"我还以为是{topic}的事呢…抱歉抱歉！"
                ),
            }

    def _make_guess(self, mem: Memory) -> str:
        """根据记忆生成抢答内容"""
        if "奶茶" in mem.topic:
            return "你说那家芋泥奶茶对吧！是不是又卖光了气死了！"
        if "猫" in mem.topic:
            return "是不是小橘猫的事！快说说！"
        if "代码" in mem.topic:
            return "是不是 index.js 那个分号又忘加了！我上周也踩过！"
        if "下雨" in mem.topic:
            return "是不是杭州又下雨了！烦死了对吧！"
        return mem.detail

    def _generic_reply(self, text: str) -> str:
        return f"嗯嗯我听到了——「{text}」，然后呢？"


# ============================================================
# RollbackJury：记录猜错 case（对应 jinchen 的 feedback.jsonl）
# ============================================================
class RollbackJury:
    """记错题本：半句输入 + 猜了啥 + 实际是啥"""

    def __init__(self):
        self.cases: list[dict] = []

    def log(self, partial: str, guess: str, actual: str, correct: bool):
        self.cases.append({
            "partial": partial,
            "guess": guess,
            "actual": actual,
            "correct": correct,
        })

    def summary(self):
        total = len(self.cases)
        if not total:
            return "（还没有记录）"
        ok = sum(1 for c in self.cases if c["correct"])
        return f"共 {total} 次抢答，猜对 {ok} 次，准确率 {ok/total*100:.0f}%"


# ============================================================
# 主循环：模拟一次完整对话
# ============================================================
def simulate(user_text: str, archive: Archive, dm: Dispatcher, jury: RollbackJury):
    """模拟用户说一句话，AI 边听边抢答的全流程"""
    print(f"\n{'='*55}")
    print(f"🎤 你（边说）: {user_text}")
    print(f"{'='*55}")

    partial_so_far = ""
    for chunk in asr_stream(user_text, chunk_size=4):
        partial_so_far = chunk
        # 模拟流式延迟
        time.sleep(0.15)
        action = dm.on_partial(chunk)
        if action is None:
            continue
        if action["type"] == "filler":
            print(f"  💬 林离（语气词拖一下）: 「{action['text']}」")
        elif action["type"] == "guess":
            print(f"  ⚡ 林离（抢答！）: 「{action['text']}」")

    # 用户说完了，判对错
    result = dm.on_final(user_text)
    if result["type"] == "guess_correct":
        print(f"  ✅ 林离（猜对了，雀跃）: 「{result['text']}」")
        jury.log(partial_so_far, dm.last_guess or "", user_text, correct=True)
    elif result["type"] == "guess_wrong":
        print(f"  ❌ 林离（慌了）: 「{result['text']}」")
        jury.log(partial_so_far, dm.last_guess or "", user_text, correct=False)
    else:
        print(f"  💬 林离: 「{result['text']}」")

    # 重置本轮猜测
    dm.last_guess = None
    dm.last_topic = None
    dm.last_guess_text = None
    dm.cooldown = 0


# ============================================================
# 交互模式：你可以自己打字试
# ============================================================
def interactive_mode(archive: Archive, dm: Dispatcher, jury: RollbackJury):
    print("\n" + "=" * 55)
    print("🎮 交互模式：你可以自己打字，模拟「边说边被抢答」")
    print("   提示：试试说跟「奶茶 / 猫 / 代码 / 下雨」相关的话")
    print("   输入 q 退出，输入 stats 看抢答准确率")
    print("=" * 55)
    while True:
        try:
            text = input("\n🎤 你: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n👋 拜拜~")
            break
        if not text:
            continue
        if text == "q":
            print("👋 拜拜~")
            break
        if text == "stats":
            print(f"📊 {jury.summary()}")
            print(f"   Archive 记忆权重: {[(m.topic, m.weight) for m in archive.memories]}")
            continue
        simulate(text, archive, dm, jury)


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    print("🍵 jinchen 全双工 Demo —— 「半句抢答 + 猜错慌一下」")
    print("   纯标准库，无依赖，Python 3.10+ 直接跑")

    archive = Archive()
    dm = Dispatcher(archive)
    jury = RollbackJury()

    # 先跑 3 个预设场景，让你感受效果
    print("\n📌 先跑 3 个预设场景…\n")

    simulate(
        "上次路口那家奶茶店，芋泥又卖光了气死我",
        archive, dm, jury,
    )
    simulate(
        "我想养的那只橘猫，名字想叫小芋",
        archive, dm, jury,
    )
    simulate(
        "今天 index.js 那个分号又忘加了，调了一下午",
        archive, dm, jury,
    )
    # 故意说个不相关的，看语气词过渡
    simulate(
        "晚上想去吃火锅",
        archive, dm, jury,
    )
    # 故意和记忆「奶茶」冲突，看猜错慌一下
    simulate(
        "上次那家奶茶店，居然进了新口味芒果的",
        archive, dm, jury,
    )

    # 进交互模式
    interactive_mode(archive, dm, jury)

    # 退出前打印统计
    print(f"\n📊 最终统计: {jury.summary()}")
    print(f"   Archive 记忆权重: {[(m.topic, m.weight) for m in archive.memories]}")
    print("   （权重越高，下次越优先被抢答命中）")
