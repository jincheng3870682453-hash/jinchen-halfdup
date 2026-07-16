"""
auto_tune.py
=============
jinchen 全双工 Demo 的自动化调参工具 v2

改进点（vs v1）：
  - 更丰富的测试用例（长句 / 短句 / 边界句）
  - 参数搜索空间更宽，让差异真正显现
  - 评分函数区分度更强（引入"抢答时机"指标）
  - 每次评估用不同随机种子，避免巧合

纯标准库，无依赖，直接跑：python auto_tune.py
"""

import sys
import json
import random
import itertools

sys.path.insert(0, ".")
from jinchen_fd_demo import Archive, Dispatcher, asr_stream


# ============================================================
# 1. 测试用例
# ============================================================
class TestCase:
    def __init__(self, text, should_guess, topic, expect_correct, category=""):
        self.text = text
        self.should_guess = should_guess
        self.topic = topic
        self.expect_correct = expect_correct
        self.category = category  # 用于分析


# 格式：(用户说的话, 该不该抢, 话题, 期望猜对否, 类别)
TEST_CASES = [
    # ---- 该抢 + 能猜对（正例）----
    TestCase("上次路口那家奶茶店芋泥又卖光了气死我", True, "奶茶", True, "正例-长"),
    TestCase("我想养的那只橘猫名字想叫小芋", True, "猫", True, "正例-中"),
    TestCase("今天 index.js 那个分号又忘加了调了一下午", True, "代码", True, "正例-长"),
    TestCase("这周杭州一直下雨烦死了都不想出门", True, "下雨", True, "正例-长"),

    # ---- 该抢但会猜错（话题冲突）----
    TestCase("上次那家奶茶店居然进了芒果味的新品", True, "奶茶", False, "冲突-长"),
    TestCase("今天写了个 python 脚本缩进错了半天", True, "代码", False, "冲突-中"),

    # ---- 边界：短句（5-8字），测 MIN_LEN 敏感度 ----
    TestCase("奶茶又没了", True, "奶茶", True, "正例-短"),
    TestCase("猫吐了", True, "猫", True, "正例-短"),

    # ---- 不该抢（无关话题）----
    TestCase("晚上想去吃火锅涮羊肉", False, "", True, "负例-长"),
    TestCase("今天天气真好适合跑步", False, "", True, "负例-中"),
    TestCase("刚看完一部电影特别好看推荐给你", False, "", True, "负例-长"),
    TestCase("周末想去爬山锻炼身体", False, "", True, "负例-中"),

    # ---- 边界：很短的无关句 ----
    TestCase("吃面", False, "", True, "负例-短"),
    TestCase("好困", False, "", True, "负例-短"),
]


# ============================================================
# 2. 参数搜索空间（更宽）
# ============================================================
PARAM_GRID = {
    "PARTIAL_LEN": [2, 3, 4, 5, 6],
    "MIN_LEN":     [2, 3, 4, 5, 6],
    "FILLER_PROB": [0.0, 0.3, 0.6, 0.9],
    "COOLDOWN":    [0, 1, 2, 3],
}


# ============================================================
# 3. 单次运行一个用例，记录更多信息
# ============================================================
def run_one(tc, dm):
    guessed = False
    guess_text = ""
    guess_at_chunk = -1  # 第几个 chunk 抢的（越早越好）
    chunk_count = 0
    reply_text = ""

    for chunk in asr_stream(tc.text, chunk_size=dm.PARTIAL_LEN):
        chunk_count += 1
        action = dm.on_partial(chunk)
        if action and action["type"] == "guess":
            if not guessed:
                guess_at_chunk = chunk_count
            guessed = True
            guess_text = action["text"]

    result = dm.on_final(tc.text)
    reply_text = result["text"]

    # 判对错
    if tc.expect_correct:
        correct = bool(tc.topic) and (tc.topic in guess_text or tc.topic in reply_text)
    else:
        correct = any(w in reply_text for w in ["不对", "听岔", "搞错", "慌", "抱歉"])

    # 重置
    dm.last_guess = None
    dm.last_topic = None
    dm.last_guess_text = None
    dm.cooldown = 0

    return {
        "text": tc.text[:18],
        "category": tc.category,
        "should_guess": tc.should_guess,
        "did_guess": guessed,
        "correct": correct,
        "reply_len": len(reply_text),
        "guess_at_chunk": guess_at_chunk,
        "total_chunks": chunk_count,
    }


# ============================================================
# 4. 评分（区分度拉满）
# ============================================================
def score_results(results):
    """
    设计原则：让不同参数组合分数明显不同。

    对"该抢"的用例：
      +4  抢了 + 猜对 + 抢得早（第1-2个chunk）+ 回复短
      +3  抢了 + 猜对 + 回复短
      +2  抢了 + 猜对 + 回复长
      +1.5 抢了 + 猜错（容错有人味）
      +0.5 抢了 + 猜错 + 回复短（省 token）
      -1  该抢但没抢（漏答）
      -2  该抢没抢且回复长（哑巴又啰嗦）

    对"不该抢"的用例：
      +3  没抢 + 回复短（安静又省 token，最理想）
      +2  没抢 + 回复长（安静但啰嗦）
      -3  不该抢却抢了（乱抢，重罚）
    """
    s = 0.0
    for r in results:
        if r["should_guess"]:
            if r["did_guess"]:
                if r["correct"]:
                    # 抢得越早分越高
                    early_bonus = 0
                    if r["guess_at_chunk"] <= 2:
                        early_bonus = 1.0
                    elif r["guess_at_chunk"] <= 3:
                        early_bonus = 0.5
                    length_bonus = 0.5 if r["reply_len"] < 35 else 0
                    s += 3.0 + early_bonus + length_bonus
                else:
                    s += 1.5 if r["reply_len"] < 40 else 1.0
            else:
                s -= 1.0 if r["reply_len"] < 30 else 2.0
        else:
            if not r["did_guess"]:
                s += 1.5 if r["reply_len"] < 25 else 1.0
            else:
                s -= 3.0  # 乱抢重罚
    return round(s, 2)


# ============================================================
# 5. 评估一组参数（跑 N 次取平均，消随机性）
# ============================================================
def evaluate(params, n_runs=5):
    all_results = []
    for seed in range(n_runs):
        random.seed(seed * 7 + 42)
        archive = Archive()
        dm = Dispatcher(archive)
        for k, v in params.items():
            setattr(dm, k, v)

        results = []
        for tc in TEST_CASES:
            results.append(run_one(tc, dm))
        all_results.append(results)

    # 多次运行取平均分数
    avg_score = sum(score_results(r) for r in all_results) / n_runs
    return round(avg_score, 2), all_results[0]  # 返回第一次的明细用于展示


# ============================================================
# 6. 主流程
# ============================================================
def main():
    print("🔧 jinchen 自动调参工具 v2")
    print("=" * 66)
    print(f"📋 测试用例数 : {len(TEST_CASES)}")
    total = 1
    for v in PARAM_GRID.values():
        total *= len(v)
    print(f"📐 参数组合数 : {total}")
    print(f"🔁 每组跑 5 次取平均（消随机性）")
    print("=" * 66)

    keys = list(PARAM_GRID.keys())
    all_rows = []
    for values in itertools.product(*[PARAM_GRID[k] for k in keys]):
        params = dict(zip(keys, values))
        sc, _ = evaluate(params)
        all_rows.append({"params": params, "score": sc})

    all_rows.sort(key=lambda x: x["score"], reverse=True)

    # ---- 分数分布 ----
    scores = [r["score"] for r in all_rows]
    print(f"\n📈 分数分布: 最高 {max(scores):.2f} | 最低 {min(scores):.2f} | 平均 {sum(scores)/len(scores):.2f}")
    unique_scores = sorted(set(scores), reverse=True)
    print(f"📊 不同分数档位: {len(unique_scores)} 档")
    for s in unique_scores[:10]:
        count = scores.count(s)
        print(f"   {s:>7.2f} 分  →  {count:>3} 组")

    # ---- Top 8 ----
    print(f"\n🏆 Top 8 最优参数组合：\n")
    hdr = f"{'#':<4} {'得分':<8}"
    for k in keys:
        hdr += f" {k:<12}"
    print(hdr)
    print("-" * 68)
    seen = set()
    shown = 0
    for row in all_rows:
        p = row["params"]
        sig = tuple(p[k] for k in keys)
        if sig in seen:
            continue
        seen.add(sig)
        line = f"{shown+1:<4} {row['score']:<8}"
        for k in keys:
            line += f" {p[k]:<12}"
        print(line)
        shown += 1
        if shown >= 8:
            break

    # ---- 最优参数明细 ----
    best = all_rows[0]
    print(f"\n{'='*66}")
    print(f"🥇 最优参数（得分 {best['score']}）：")
    for k, v in best["params"].items():
        print(f"   {k} = {v}")

    _, details = evaluate(best["params"], n_runs=1)
    print(f"\n📊 该参数下各用例表现：")
    print(f"{'用例':<20} {'类别':<10} {'该抢':<5} {'抢了':<5} {'对':<4} {'chunk':<7} {'长度':<6}")
    print("-" * 62)
    for r in details:
        a = "✅" if r["should_guess"] else "❌"
        b = "✅" if r["did_guess"] else "❌"
        c = "✅" if r["correct"] else "❌"
        ga = str(r["guess_at_chunk"]) if r["guess_at_chunk"] > 0 else "-"
        print(f"{r['text']:<20} {r['category']:<10} {a:<5} {b:<5} {c:<4} {ga:<7} {r['reply_len']:<6}")

    # ---- 保存 ----
    out = "auto_tune_result.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, ensure_ascii=False, indent=2)
    print(f"\n💾 全部 {total} 组结果已保存 → {out}")


if __name__ == "__main__":
    random.seed(None)  # 让 evaluate 内部自己管种子
    main()
