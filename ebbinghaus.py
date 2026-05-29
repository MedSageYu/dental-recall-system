"""
艾宾浩斯遗忘曲线 — 复习间隔计算
被 storage.py 的 update_topic_progress() 调用
"""

from datetime import datetime, timedelta
import math


# 记忆等级 0-5 对应的下次复习间隔（天）
# 0=未学，每正确一次升一级，错误回退到 0
INTERVALS = [1, 2, 4, 7, 15, 30]


def next_interval(memory_level: int, is_correct: bool) -> int:
    """
    根据当前等级和本次对错，返回下次复习间隔天数。
    错误 → 回到第 0 级（1天后复习）
    正确 → 升一级，取对应间隔
    """
    if not is_correct:
        return INTERVALS[0]
    level = min(memory_level + 1, len(INTERVALS) - 1)
    return INTERVALS[level]


def update_progress(topic_id: int, is_correct: bool,
                  current_level: int) -> dict:
    """
    计算下次复习日期。
    返回 {"new_level", "next_review_date"}
    """
    new_level = max(0, current_level + (1 if is_correct else -1))
    new_level = min(new_level, 5)
    # 错误时回退一级（最低 0）
    if not is_correct:
        new_level = max(0, current_level - 1)

    interval = next_interval(new_level, is_correct)
    next_date = (datetime.now() + timedelta(days=interval)).strftime("%Y-%m-%d")

    return {
        "new_level": new_level,
        "next_interval": interval,
        "next_review_date": next_date,
    }


def memory_strength(last_review_date: str,
                    next_review_date: str) -> float:
    """
    计算当前记忆保持率（0.0-1.0）
    公式：R = e^(-t/S)，S = 间隔天数 / 2
    """
    if not last_review_date or not next_review_date:
        return 0.0
    try:
        last = datetime.fromisoformat(last_review_date)
        nxt = datetime.fromisoformat(next_review_date)
    except Exception:
        return 0.0

    now = datetime.now()
    elapsed = (now - last).total_seconds()
    total = (nxt - last).total_seconds()
    if total <= 0:
        return 1.0

    strength = total / 2.0
    retention = math.exp(-elapsed / strength)
    return max(0.0, min(1.0, retention))


if __name__ == "__main__":
    # 快速测试
    for level in range(6):
        for correct in [True, False]:
            r = update_progress(0, correct, level)
            print(f"  level={level} correct={correct} "
                  f"→ new_level={r['new_level']} "
                  f"interval={r['next_interval']}d")
