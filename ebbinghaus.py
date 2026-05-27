"""
艾宾浩斯遗忘曲线算法
基于间隔重复（Spaced Repetition）生成复习计划
"""
from datetime import datetime, timedelta
import json
import os
import sqlite3
from typing import Optional


# 艾宾浩斯复习间隔（天数）
# 理论曲线：20分钟后遗忘42%，1小时后遗忘56%，1天后遗忘74%
# 间隔重复优化：每次正确回忆后翻倍间隔
EBBINGHAUS_INTERVALS = [1, 2, 4, 7, 15, 30, 60, 120]  # 天

# 简化记忆等级（0-5）
# 0 = 未学，1 = 刚学，2-5 = 不同熟练度
MEMORY_LEVELS = {
    0: 0,    # 未学
    1: 1,    # 第一次学习
    2: 2,    # 第一次复习（1天后）
    3: 4,    # 第二次复习（2天后）
    4: 7,    # 第三次复习（4天后）
    5: 15,   # 第四次复习（7天后）
}


def get_next_review_interval(memory_level: int, is_correct: bool) -> int:
    """
    根据当前记忆等级和回忆是否正确，返回下一次复习间隔（天）

    Args:
        memory_level: 0-5，当前记忆等级
        is_correct: 本次回忆是否正确

    Returns:
        下次复习间隔天数
    """
    if not is_correct:
        # 错误 → 重新从第1级开始
        return EBBINGHAUS_INTERVALS[0]

    # 正确 → 升级
    if memory_level < len(EBBINGHAUS_INTERVALS):
        return EBBINGHAUS_INTERVALS[memory_level]
    return EBBINGHAUS_INTERVALS[-1]


def calculate_memory_strength(last_review: datetime, next_review: datetime) -> float:
    """
    计算当前记忆强度（0-1）
    基于艾宾浩斯遗忘曲线公式：R = e^(-t/S)
    R = 记忆保持率，t = 时间流逝，S = 记忆强度
    """
    if not last_review or not next_review:
        return 0.0

    now = datetime.now()
    elapsed = (now - last_review).total_seconds()
    total_interval = (next_review - last_review).total_seconds()

    if total_interval <= 0:
        return 1.0

    # 记忆强度参数 S（越大衰减越慢）
    strength = total_interval / 2.0  # 半衰期

    if strength <= 0:
        return 1.0

    import math
    retention = math.exp(-elapsed / strength)
    return max(0.0, min(1.0, retention))


def generate_daily_schedule(
    topics: list[dict],
    total_days: int,
    start_date: Optional[str] = None,
    daily_limit: int = 30
) -> dict:
    """
    生成每日背诵计划

    Args:
        topics: 知识点列表 [{"id": ..., "title": ..., "keywords": [...]}]
        total_days: 总背诵天数
        start_date: 开始日期（YYYY-MM-DD），默认今天
        daily_limit: 每天最多背诵的知识点数量

    Returns:
        {
            "start_date": "2025-01-01",
            "total_days": 30,
            "daily_schedule": [
                {
                    "date": "2025-01-01",
                    "new_topics": [...],       # 今日新学
                    "review_topics": [...],     # 今日复习
                    "total_count": 15
                }
            ]
        }
    """
    if start_date:
        start = datetime.strptime(start_date, "%Y-%m-%d")
    else:
        start = datetime.now()

    # 计算每天新学数量
    total_topics = len(topics)
    if total_topics == 0:
        return {"daily_schedule": []}

    # 均匀分配新学内容
    topics_per_day = max(1, total_topics // total_days)

    daily_schedule = []
    topic_index = 0

    for day in range(total_days):
        date = start + timedelta(days=day)
        date_str = date.strftime("%Y-%m-%d")

        # 今日新学
        new_topics = []
        for _ in range(topics_per_day):
            if topic_index < total_topics:
                new_topics.append({
                    "id": topics[topic_index]["id"],
                    "title": topics[topic_index]["title"],
                    "keywords": topics[topic_index]["keywords"],
                    "type": "new"
                })
                topic_index += 1

        # 今日复习（基于艾宾浩斯间隔）
        review_topics = []
        for interval_days in EBBINGHAUS_INTERVALS:
            review_day = day - interval_days
            if review_day >= 0 and review_day < len(daily_schedule):
                prev_schedule = daily_schedule[review_day]
                for t in prev_schedule.get("new_topics", []):
                    review_topics.append({
                        "id": t["id"],
                        "title": t["title"],
                        "keywords": t["keywords"],
                        "type": "review",
                        "interval": interval_days
                    })

        # 截断到每日上限
        total_today = len(new_topics) + len(review_topics)
        if total_today > daily_limit:
            review_topics = review_topics[:daily_limit - len(new_topics)]

        daily_schedule.append({
            "date": date_str,
            "new_topics": new_topics,
            "review_topics": review_topics,
            "total_count": len(new_topics) + len(review_topics),
            "day_number": day + 1
        })

    return {
        "start_date": start.strftime("%Y-%m-%d"),
        "total_days": total_days,
        "total_topics": total_topics,
        "topics_per_day": topics_per_day,
        "daily_schedule": daily_schedule
    }


def update_progress(
    topic_id: int,
    is_correct: bool,
    current_level: int
) -> dict:
    """
    根据本次背诵结果更新记忆等级

    Returns:
        {"new_level": int, "next_interval": int, "next_review_date": str}
    """
    if is_correct:
        new_level = min(current_level + 1, 5)
    else:
        new_level = max(current_level - 1, 0)

    next_interval = get_next_review_interval(new_level, is_correct)
    next_review = datetime.now() + timedelta(days=next_interval)

    return {
        "new_level": new_level,
        "next_interval": next_interval,
        "next_review_date": next_review.strftime("%Y-%m-%d"),
        "memory_strength": calculate_memory_strength(
            datetime.now(),
            next_review
        )
    }


if __name__ == "__main__":
    # 测试
    topics = [{"id": i, "title": f"知识点{i}", "keywords": [f"关键词{i}"]}
              for i in range(100)]
    schedule = generate_daily_schedule(topics, total_days=30)
    for day in schedule["daily_schedule"][:5]:
        print(f"\n📅 {day['date']} (Day {day['day_number']})")
        print(f"  新学: {len(day['new_topics'])} 个")
        print(f"  复习: {len(day['review_topics'])} 个")
        for t in day["new_topics"][:3]:
            print(f"    📝 {t['title']}")
        for t in day["review_topics"][:3]:
            print(f"    🔄 {t['title']} (间隔{t['interval']}天)")
