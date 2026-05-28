"""
口腔考研带背系统 - 存储层 v3
核心原则：
1. 原始输入不可覆盖，AI 分析是副本
2. 背诵原文同步备份到 txt
3. 每日计划管理
4. 数据安全：WAL + 即时 commit + 启动备份
"""
import sqlite3
import json
import os
import shutil
from datetime import datetime, timedelta
from typing import Optional

DB_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DB_DIR, "recall.db")
BACKUP_DIR = os.path.join(DB_DIR, "backups")
RECITATION_DIR = os.path.join(DB_DIR, "recitations")
DAILY_LOG_DIR = os.path.join(DB_DIR, "daily_logs")
EXPORT_DIR = os.path.join(DB_DIR, "exports")


def _ensure_dirs():
    for d in [DB_DIR, BACKUP_DIR, RECITATION_DIR, DAILY_LOG_DIR, EXPORT_DIR]:
        os.makedirs(d, exist_ok=True)


def get_db():
    _ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS books (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        original_filename TEXT,
        pdf_path TEXT,
        topic_count INTEGER DEFAULT 0,
        target_days INTEGER DEFAULT 0,
        exam_date TEXT,
        raw_text TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS topics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id INTEGER NOT NULL,
        topic_index INTEGER NOT NULL,
        title TEXT NOT NULL,
        content TEXT,
        keywords TEXT,
        key_expressions TEXT,
        section TEXT DEFAULT '',
        difficulty INTEGER DEFAULT 3,
        memory_level INTEGER DEFAULT 0,
        plan_date TEXT,
        next_review_date TEXT,
        last_review_date TEXT,
        correct_count INTEGER DEFAULT 0,
        wrong_count INTEGER DEFAULT 0,
        status TEXT DEFAULT 'new',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS study_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic_id INTEGER NOT NULL,
        book_id INTEGER NOT NULL,
        is_correct INTEGER NOT NULL,
        recited_text TEXT NOT NULL,
        ai_analysis TEXT,
        matched_keywords TEXT,
        missing_keywords TEXT,
        score REAL DEFAULT 0,
        dim_completeness REAL DEFAULT 0,
        dim_keypoints REAL DEFAULT 0,
        dim_accuracy REAL DEFAULT 0,
        dim_logic REAL DEFAULT 0,
        dim_depth REAL DEFAULT 0,
        total_score REAL DEFAULT 0,
        study_date TEXT DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (topic_id) REFERENCES topics(id) ON DELETE CASCADE,
        FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS daily_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id INTEGER NOT NULL,
        plan_date TEXT NOT NULL,
        new_topic_ids TEXT DEFAULT '[]',
        review_topic_ids TEXT DEFAULT '[]',
        completed_count INTEGER DEFAULT 0,
        total_count INTEGER DEFAULT 0,
        deferred INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE,
        UNIQUE(book_id, plan_date)
    );

    CREATE TABLE IF NOT EXISTS study_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id INTEGER,
        start_time TEXT NOT NULL,
        end_time TEXT,
        duration_minutes REAL DEFAULT 0,
        topics_studied INTEGER DEFAULT 0,
        correct_count INTEGER DEFAULT 0,
        wrong_count INTEGER DEFAULT 0,
        avg_score REAL DEFAULT 0,
        efficiency_score REAL DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS activity_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT NOT NULL,
        detail TEXT,
        created_at TEXT DEFAULT (datetime('now', 'localtime'))
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS day_summaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id INTEGER NOT NULL,
        summary_date TEXT NOT NULL,
        topics_studied INTEGER DEFAULT 0,
        avg_score REAL DEFAULT 0,
        total_score REAL DEFAULT 0,
        strengths TEXT DEFAULT '[]',
        weaknesses TEXT DEFAULT '[]',
        ai_summary TEXT DEFAULT '',
        tomorrow_preview TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE,
        UNIQUE(book_id, summary_date)
    );
    """)
    # 迁移：给旧表加新列
    _migrate_schema(conn)
    conn.commit()
    conn.close()


def _migrate_schema(conn):
    """安全迁移：给旧数据库加新列（IF NOT EXISTS 模拟）"""
    migrations = [
        ("topics", "plan_date", "TEXT"),
        ("topics", "status", "TEXT DEFAULT 'new'"),
        ("books", "raw_text", "TEXT"),
        ("study_records", "dim_completeness", "REAL DEFAULT 0"),
        ("study_records", "dim_keypoints", "REAL DEFAULT 0"),
        ("study_records", "dim_accuracy", "REAL DEFAULT 0"),
        ("study_records", "dim_logic", "REAL DEFAULT 0"),
        ("study_records", "dim_depth", "REAL DEFAULT 0"),
        ("study_records", "total_score", "REAL DEFAULT 0"),
        ("daily_plans", "deferred", "INTEGER DEFAULT 0"),
        ("topics", "key_expressions", "TEXT"),
    ]
    for table, col, col_type in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # 列已存在


# ============================================================
# 活动日志
# ============================================================

def log_activity(action: str, detail: dict = None):
    conn = get_db()
    conn.execute("INSERT INTO activity_log (action, detail) VALUES (?, ?)",
                 (action, json.dumps(detail or {}, ensure_ascii=False)))
    conn.commit()
    conn.close()


def get_activity_log(limit: int = 100):
    conn = get_db()
    rows = conn.execute("SELECT * FROM activity_log ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_today_activity():
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    rows = conn.execute("SELECT * FROM activity_log WHERE DATE(created_at) = ? ORDER BY created_at", (today,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============================================================
# 书库管理
# ============================================================

def add_book(name: str, pdf_path: str = None, original_filename: str = None,
             target_days: int = 0, exam_date: str = None, raw_text: str = None) -> int:
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO books (name, pdf_path, original_filename, target_days, exam_date, raw_text) VALUES (?, ?, ?, ?, ?, ?)",
        (name, pdf_path, original_filename, target_days, exam_date, raw_text))
    book_id = cur.lastrowid
    conn.commit()
    conn.close()
    log_activity("add_book", {"book_id": book_id, "name": name})
    return book_id


def get_books():
    conn = get_db()
    rows = conn.execute("SELECT * FROM books ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_book_by_id(book_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_book(book_id: int, **kwargs):
    """通用更新：支持 name, target_days, exam_date 等任意字段"""
    conn = get_db()
    updates = []
    params = []
    allowed = {"name", "target_days", "exam_date", "topic_count", "raw_text"}
    for k, v in kwargs.items():
        if k in allowed:
            updates.append(f"{k} = ?")
            params.append(v)
    if updates:
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(book_id)
        conn.execute(f"UPDATE books SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
    conn.close()


def delete_book(book_id: int):
    conn = get_db()
    conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
    conn.commit()
    conn.close()


def get_book_progress(book_id: int) -> dict:
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) as c FROM topics WHERE book_id=?", (book_id,)).fetchone()["c"]
    mastered = conn.execute("SELECT COUNT(*) as c FROM topics WHERE book_id=? AND memory_level>=4", (book_id,)).fetchone()["c"]
    in_progress = conn.execute("SELECT COUNT(*) as c FROM topics WHERE book_id=? AND memory_level>0 AND memory_level<4", (book_id,)).fetchone()["c"]
    not_started = conn.execute("SELECT COUNT(*) as c FROM topics WHERE book_id=? AND memory_level=0", (book_id,)).fetchone()["c"]
    today = datetime.now().strftime("%Y-%m-%d")
    today_records = conn.execute(
        "SELECT COUNT(*) as c FROM study_records WHERE book_id=? AND DATE(study_date)=?",
        (book_id, today)).fetchone()["c"]
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    week_count = conn.execute(
        "SELECT COUNT(*) as c FROM study_records WHERE book_id=? AND DATE(study_date)>=?",
        (book_id, week_ago)).fetchone()["c"]
    daily_avg = round(week_count / 7, 1)
    remaining = total - mastered
    book = conn.execute("SELECT target_days FROM books WHERE id=?", (book_id,)).fetchone()
    target_days = dict(book)["target_days"] if book else 0
    if target_days > 0 and total > 0:
        topics_per_day = max(1, round(total / target_days))
        est_days = target_days
    elif daily_avg > 0 and remaining > 0:
        topics_per_day = max(1, round(daily_avg))
        est_days = int(remaining / daily_avg)
    else:
        topics_per_day = 0
        est_days = 0
    conn.close()
    return {
        "total": total, "mastered": mastered, "in_progress": in_progress,
        "not_started": not_started, "today_records": today_records,
        "mastery_rate": round(mastered / total * 100, 1) if total > 0 else 0,
        "daily_avg": daily_avg, "est_days": est_days,
        "topics_per_day": topics_per_day,
    }


# ============================================================
# 知识点
# ============================================================

def add_topics(book_id: int, topics: list[dict]):
    conn = get_db()
    for i, t in enumerate(topics):
        conn.execute(
            "INSERT INTO topics (book_id, topic_index, title, content, keywords, section, difficulty) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (book_id, t.get("id", i + 1), t["title"], t.get("content", ""),
             json.dumps(t.get("keywords", []), ensure_ascii=False),
             t.get("section", ""), t.get("difficulty", 3)))
    conn.execute("UPDATE books SET topic_count = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                 (len(topics), book_id))
    conn.commit()
    conn.close()


def get_topics(book_id: int):
    conn = get_db()
    rows = conn.execute("SELECT * FROM topics WHERE book_id=? ORDER BY topic_index", (book_id,)).fetchall()
    conn.close()
    return [{**dict(r), "keywords": json.loads(dict(r)["keywords"]) if dict(r)["keywords"] else []} for r in rows]


def get_topic_by_id(topic_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
    conn.close()
    if row:
        d = dict(row)
        d["keywords"] = json.loads(d["keywords"]) if d["keywords"] else []
        return d
    return None


def get_due_topics(book_id: int):
    """获取今天需要背诵/复习的知识点（plan_date <= today 或 next_review_date <= today）"""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM topics WHERE book_id=?
           AND status != 'mastered'
           AND (plan_date IS NULL OR plan_date <= ?)
           AND (next_review_date IS NULL OR next_review_date <= ?)
           ORDER BY
             CASE WHEN memory_level = 0 THEN 0 ELSE 1 END,
             difficulty DESC,
             next_review_date ASC""",
        (book_id, today, today)).fetchall()
    conn.close()
    return [{**dict(r), "keywords": json.loads(dict(r)["keywords"]) if dict(r)["keywords"] else []} for r in rows]


def update_topic_progress(topic_id: int, is_correct: bool, new_level: int, next_review: str):
    conn = get_db()
    status = 'mastered' if new_level >= 5 else ('learning' if new_level > 0 else 'new')
    if is_correct:
        conn.execute(
            "UPDATE topics SET memory_level=?, next_review_date=?, last_review_date=datetime('now','localtime'), correct_count=correct_count+1, status=? WHERE id=?",
            (new_level, next_review, status, topic_id))
    else:
        conn.execute(
            "UPDATE topics SET memory_level=?, next_review_date=?, last_review_date=datetime('now','localtime'), wrong_count=wrong_count+1, status=? WHERE id=?",
            (new_level, next_review, status, topic_id))
    conn.commit()
    conn.close()


# ============================================================
# 每日计划管理
# ============================================================

def generate_daily_plan(book_id: int, target_days: int):
    """根据目标天数，将知识点均匀分配到每天"""
    topics = get_topics(book_id)
    if not topics or target_days <= 0:
        return
    conn = get_db()
    # 清除旧计划
    conn.execute("DELETE FROM daily_plans WHERE book_id=?", (book_id,))
    topics_per_day = max(1, len(topics) // target_days)
    start_date = datetime.now()
    for day_idx in range(target_days):
        plan_date = (start_date + timedelta(days=day_idx)).strftime("%Y-%m-%d")
        start = day_idx * topics_per_day
        end = start + topics_per_day if day_idx < target_days - 1 else len(topics)
        day_topic_ids = [t["id"] for t in topics[start:end]]
        if not day_topic_ids:
            break
        # 更新 topic 的 plan_date
        for tid in day_topic_ids:
            conn.execute("UPDATE topics SET plan_date=? WHERE id=?", (plan_date, tid))
        conn.execute(
            "INSERT OR REPLACE INTO daily_plans (book_id, plan_date, new_topic_ids, review_topic_ids, total_count) VALUES (?, ?, ?, ?, ?)",
            (book_id, plan_date, json.dumps(day_topic_ids), "[]", len(day_topic_ids)))
    conn.commit()
    conn.close()
    log_activity("generate_plan", {"book_id": book_id, "days": target_days, "total_topics": len(topics)})


def get_today_plan(book_id: int) -> dict:
    """获取今日计划：新学 + 待复习"""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    # 今日新学
    new_topics = conn.execute(
        "SELECT * FROM topics WHERE book_id=? AND plan_date=? AND status='new' ORDER BY topic_index",
        (book_id, today)).fetchall()
    # 今日待复习（艾宾浩斯到期）
    review_topics = conn.execute(
        "SELECT * FROM topics WHERE book_id=? AND next_review_date<=? AND status != 'new' AND memory_level > 0 ORDER BY next_review_date ASC",
        (book_id, today)).fetchall()
    # 之前未完成顺延的
    deferred = conn.execute(
        "SELECT * FROM topics WHERE book_id=? AND plan_date<? AND status='new' ORDER BY plan_date, topic_index",
        (book_id, today)).fetchall()
    conn.close()

    def parse_row(r):
        d = dict(r)
        d["keywords"] = json.loads(d["keywords"]) if d["keywords"] else []
        return d

    all_topics = [parse_row(r) for r in deferred] + [parse_row(r) for r in new_topics] + [parse_row(r) for r in review_topics]
    return {
        "new_count": len(new_topics) + len(deferred),
        "review_count": len(review_topics),
        "total_count": len(all_topics),
        "topics": all_topics,
        "plan_date": today,
    }


def mark_plan_completed(book_id: int, date: str = None):
    """标记某天计划完成"""
    date = date or datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    conn.execute(
        "UPDATE daily_plans SET completed=1, completed_count=(SELECT COUNT(*) FROM study_records WHERE book_id=? AND DATE(study_date)=?) WHERE book_id=? AND plan_date=?",
        (book_id, date, book_id, date))
    conn.commit()
    conn.close()


# ============================================================
# 学习记录（原始文本保护 + txt 备份）
# ============================================================

def _save_recitation_txt(topic_id: int, topic_title: str, recited_text: str):
    """将背诵原文备份到独立 txt 文件"""
    _ensure_dirs()
    today = datetime.now().strftime("%Y-%m-%d")
    day_dir = os.path.join(RECITATION_DIR, today)
    os.makedirs(day_dir, exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    safe_title = "".join(c for c in topic_title if c.isalnum() or c in "._- ")[:30]
    filepath = os.path.join(day_dir, f"{ts}_topic{topic_id}_{safe_title}.txt")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"知识点ID: {topic_id}\n")
        f.write(f"标题: {topic_title}\n")
        f.write(f"时间: {datetime.now().isoformat()}\n")
        f.write(f"{'='*50}\n")
        f.write(recited_text)
    return filepath


def add_study_record(topic_id: int, book_id: int, is_correct: bool,
                     recited_text: str = "", matched: list = None,
                     missing: list = None, score: float = 0,
                     ai_analysis: str = "",
                     dims: dict = None):
    """保存学习记录。recited_text 是用户原始输入，绝不修改。同时备份到 txt。"""
    # 备份原文到 txt
    topic = get_topic_by_id(topic_id)
    topic_title = topic["title"] if topic else f"topic_{topic_id}"
    _save_recitation_txt(topic_id, topic_title, recited_text)

    dims = dims or {}
    conn = get_db()
    conn.execute(
        """INSERT INTO study_records
           (topic_id, book_id, is_correct, recited_text, ai_analysis,
            matched_keywords, missing_keywords, score,
            dim_completeness, dim_keypoints, dim_accuracy, dim_logic, dim_depth, total_score)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (topic_id, book_id, 1 if is_correct else 0, recited_text, ai_analysis,
         json.dumps(matched or [], ensure_ascii=False),
         json.dumps(missing or [], ensure_ascii=False), score,
         dims.get("completeness", 0), dims.get("keypoints", 0),
         dims.get("accuracy", 0), dims.get("logic", 0),
         dims.get("depth", 0), dims.get("total", score * 100)))
    conn.commit()
    conn.close()
    log_activity("recite", {"topic_id": topic_id, "score": score, "correct": is_correct, "text_len": len(recited_text) if recited_text else 0})


def get_study_stats(book_id: int) -> dict:
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) as c FROM topics WHERE book_id=?", (book_id,)).fetchone()["c"]
    mastered = conn.execute("SELECT COUNT(*) as c FROM topics WHERE book_id=? AND memory_level>=4", (book_id,)).fetchone()["c"]
    in_progress = conn.execute("SELECT COUNT(*) as c FROM topics WHERE book_id=? AND memory_level>0 AND memory_level<4", (book_id,)).fetchone()["c"]
    not_started = conn.execute("SELECT COUNT(*) as c FROM topics WHERE book_id=? AND memory_level=0", (book_id,)).fetchone()["c"]
    today = datetime.now().strftime("%Y-%m-%d")
    today_records = conn.execute("SELECT COUNT(*) as c FROM study_records WHERE book_id=? AND DATE(study_date)=?", (book_id, today)).fetchone()["c"]
    # 今日平均分
    today_avg = conn.execute("SELECT AVG(total_score) as a FROM study_records WHERE book_id=? AND DATE(study_date)=?", (book_id, today)).fetchone()["a"]
    conn.close()
    return {
        "total": total, "mastered": mastered, "in_progress": in_progress,
        "not_started": not_started, "today_records": today_records,
        "today_avg_score": round(today_avg, 1) if today_avg else 0,
        "mastery_rate": round(mastered / total * 100, 1) if total > 0 else 0,
    }


# ============================================================
# 学习会话
# ============================================================

def start_study_session(book_id: int = None) -> int:
    conn = get_db()
    cur = conn.execute("INSERT INTO study_sessions (book_id, start_time) VALUES (?, ?)",
                       (book_id, datetime.now().isoformat()))
    session_id = cur.lastrowid
    conn.commit()
    conn.close()
    return session_id


def end_study_session(session_id: int, topics_studied: int, correct: int, wrong: int, avg_score: float):
    conn = get_db()
    duration = 0
    row = conn.execute("SELECT start_time FROM study_sessions WHERE id=?", (session_id,)).fetchone()
    if row:
        start = datetime.fromisoformat(row["start_time"])
        duration = (datetime.now() - start).total_seconds() / 60.0
    accuracy = correct / max(correct + wrong, 1)
    speed_bonus = min(topics_studied / max(duration, 1) * 10, 1) if duration > 0 else 0
    efficiency = round((accuracy * 0.6 + avg_score * 0.4 + speed_bonus * 0.3) * 100, 1)
    conn.execute(
        """UPDATE study_sessions SET end_time=?, duration_minutes=?, topics_studied=?,
           correct_count=?, wrong_count=?, avg_score=?, efficiency_score=? WHERE id=?""",
        (datetime.now().isoformat(), round(duration, 1), topics_studied,
         correct, wrong, round(avg_score, 1), efficiency, session_id))
    conn.commit()
    conn.close()
    return efficiency


# ============================================================
# 日历数据
# ============================================================

def get_calendar_data(year: int, month: int, book_id: int = None) -> list[dict]:
    conn = get_db()
    start = f"{year}-{month:02d}-01"
    end = f"{year + 1}-01-01" if month == 12 else f"{year}-{month + 1:02d}-01"
    if book_id:
        rows = conn.execute(
            """SELECT DATE(study_date) as day, COUNT(*) as cnt,
               AVG(total_score) as avg_score,
               SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) as correct
               FROM study_records WHERE book_id=? AND DATE(study_date)>=? AND DATE(study_date)<?
               GROUP BY DATE(study_date)""",
            (book_id, start, end)).fetchall()
    else:
        rows = conn.execute(
            """SELECT DATE(study_date) as day, COUNT(*) as cnt,
               AVG(total_score) as avg_score,
               SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) as correct
               FROM study_records WHERE DATE(study_date)>=? AND DATE(study_date)<?
               GROUP BY DATE(study_date)""",
            (start, end)).fetchall()
    sessions = conn.execute(
        "SELECT DATE(start_time) as day, SUM(duration_minutes) as total_min, AVG(efficiency_score) as avg_eff FROM study_sessions WHERE DATE(start_time)>=? AND DATE(start_time)<? GROUP BY DATE(start_time)",
        (start, end)).fetchall()
    # 每天背了哪些科目
    books_data = {}
    if not book_id:
        brows = conn.execute(
            """SELECT DATE(sr.study_date) as day, b.name as book_name, COUNT(*) as cnt
               FROM study_records sr JOIN books b ON sr.book_id = b.id
               WHERE DATE(sr.study_date)>=? AND DATE(sr.study_date)<?
               GROUP BY DATE(sr.study_date), sr.book_id""",
            (start, end)).fetchall()
        for br in brows:
            bd = dict(br)
            books_data.setdefault(bd["day"], []).append({"name": bd["book_name"], "count": bd["cnt"]})
    conn.close()
    sess_map = {dict(s)["day"]: dict(s) for s in sessions}
    result = []
    for r in rows:
        d = dict(r)
        day = d["day"]
        s = sess_map.get(day, {})
        result.append({
            "date": day,
            "topics_studied": d["cnt"],
            "avg_score": round((d["avg_score"] or 0) * 100, 1),
            "correct_rate": round(d["correct"] / d["cnt"] * 100, 1) if d["cnt"] > 0 else 0,
            "duration_minutes": round(s.get("total_min", 0) or 0, 1),
            "efficiency": round(s.get("avg_eff", 0) or 0, 1),
            "books": books_data.get(day, []),
        })
    return result


# ============================================================
# 分数预估
# ============================================================

def estimate_score(book_id: int) -> dict:
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) as c FROM topics WHERE book_id=?", (book_id,)).fetchone()["c"]
    if total == 0:
        conn.close()
        return {"estimated_score": 0, "confidence": 0, "breakdown": {}}
    levels = conn.execute(
        "SELECT memory_level, COUNT(*) as c FROM topics WHERE book_id=? GROUP BY memory_level", (book_id,)).fetchall()
    level_dist = {r["memory_level"]: r["c"] for r in levels}
    retention_rates = {0: 0.0, 1: 0.3, 2: 0.5, 3: 0.7, 4: 0.85, 5: 0.95}
    weighted_sum = sum(level_dist.get(lv, 0) * retention_rates.get(lv, 0) for lv in range(6))
    estimated = round(weighted_sum / total * 100, 1) if total > 0 else 0
    total_records = conn.execute("SELECT COUNT(*) as c FROM study_records WHERE book_id=?", (book_id,)).fetchone()["c"]
    confidence = min(total_records / max(total, 1) * 100, 100)
    conn.close()
    return {
        "estimated_score": estimated,
        "confidence": round(confidence, 1),
        "breakdown": {f"level_{k}": v for k, v in sorted(level_dist.items())},
        "total_topics": total,
    }


# ============================================================
# 每日总结
# ============================================================

def save_day_summary(book_id: int, summary_date: str, data: dict):
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO day_summaries
           (book_id, summary_date, topics_studied, avg_score, total_score,
            strengths, weaknesses, ai_summary, tomorrow_preview)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (book_id, summary_date,
         data.get("topics_studied", 0), data.get("avg_score", 0),
         data.get("total_score", 0),
         json.dumps(data.get("strengths", []), ensure_ascii=False),
         json.dumps(data.get("weaknesses", []), ensure_ascii=False),
         data.get("ai_summary", ""),
         data.get("tomorrow_preview", "")))
    conn.commit()
    conn.close()


def get_day_summary(book_id: int, date: str = None):
    date = date or datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    row = conn.execute("SELECT * FROM day_summaries WHERE book_id=? AND summary_date=?",
                       (book_id, date)).fetchone()
    conn.close()
    if row:
        d = dict(row)
        d["strengths"] = json.loads(d["strengths"]) if d["strengths"] else []
        d["weaknesses"] = json.loads(d["weaknesses"]) if d["weaknesses"] else []
        return d
    return None


# ============================================================
# 设置
# ============================================================

def get_setting(key: str, default: str = None):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)", (key, value))
    conn.commit()
    conn.close()


# ============================================================
# 备份
# ============================================================

def backup_database():
    _ensure_dirs()
    if not os.path.exists(DB_PATH):
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"recall_{ts}.db")
    shutil.copy2(DB_PATH, backup_path)
    backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith(".db")])
    for f in backups[:-30]:
        os.remove(os.path.join(BACKUP_DIR, f))
    return backup_path


def save_daily_summary():
    today = datetime.now().strftime("%Y-%m-%d")
    activity = get_today_activity()
    _ensure_dirs()
    filepath = os.path.join(DAILY_LOG_DIR, f"{today}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({"date": today, "activities": activity, "saved_at": datetime.now().isoformat()},
                  f, ensure_ascii=False, indent=2)
    return filepath


# 初始化
init_db()
