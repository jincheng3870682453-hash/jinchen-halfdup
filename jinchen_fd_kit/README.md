# jinchen 全双工 + 自动调参 工具包

## 文件说明

| 文件 | 作用 |
|---|---|
| `jinchen_fd_demo.py` | 全双工原型：半句抢答 + 语气词过渡 + 猜错慌一下 |
| `auto_tune.py` | 自动调参：扫 400 组参数组合，打分排名，告诉你哪组最优 |
| `auto_tune_result.json` | 调参结果缓存（400 组全部打分明细） |

## 快速开始

```bash
# 1. 跑原型 demo（含 5 个预设场景 + 交互模式）
python jinchen_fd_demo.py

# 2. 跑自动调参（约 30 秒，扫 400 组参数）
python auto_tune.py
```

## 调参跑出来的结论

最优参数（得分 25.5 / 满分区间 11.5~25.5）：
```
PARTIAL_LEN = 4    # ASR 每出 4 个字触发一次判断
MIN_LEN     = 2    # 输入 ≥2 字就允许抢答（越短越敏感）
FILLER_PROB = 0.0  # 不插语气词（要"干净抢答"风格）
COOLDOWN    = 0    # 不冷却，每个 chunk 都可抢
```

含义：要让 AI 像真人一样"抢话"，核心是**反应快（MIN_LEN 低）+ 抢得早（PARTIAL_LEN 适中）**，反而语气词和冷却是减分项——因为测试用例里"干净利落的抢答"比"嗯…呃…哦！"得分高。

## 怎么接你自己的场景

1. **换测试用例**：编辑 `auto_tune.py` 里的 `TEST_CASES`，换成你真实用户的对话日志
2. **加新参数**：在 `PARAM_GRID` 里加键值对，并在 `Dispatcher` 里加对应属性
3. **改评分标准**：编辑 `score_results()`，按你的业务目标调权重
   - 想省 token → 加大 `reply_len` 的惩罚
   - 想像真人 → 给"猜错但有人味"加更多分
4. **自动化**：把这脚本丢 crontab 每天跑一次，用最新 feedback.jsonl 当测试用例

## 下一步

- 把 `Dispatcher` 接到真 ASR（whisper.cpp / FunASR）就是真·全双工
- 把 `auto_tune_result.json` 里 Top 10 参数做 A/B 测试
- 评分函数接上真实用户反馈（点赞/纠正/沉默时长）
