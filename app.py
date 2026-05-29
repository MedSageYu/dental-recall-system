"""
口腔医学考研带背系统 - 主服务 v3
完全对齐前端 API + 一键到底学习流 + 每日计划
"""
import os
import sys
import re
import json
import cgi
import time
import signal
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import threading
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))

from pdf_parser import extract_text_from_pdf
from ollama_ai import check_ollama, compare_recitation, compare_recitation_ai, generate_day_summary, generate_key_expressions, score_by_expressions
from ebbinghaus import update_progress
from storage import (
    init_db, backup_database, save_daily_summary, log_activity,
    add_book, get_books, get_book_by_id, update_book, delete_book, get_book_progress,
    add_topics, get_topics, get_topic_by_id, get_due_topics, update_topic_progress,
    add_study_record, get_study_stats, start_study_session, end_study_session,
    get_calendar_data, estimate_score, get_setting, set_setting,
    get_activity_log, get_today_activity,
    generate_daily_plan, get_today_plan, mark_plan_completed, save_day_summary, get_day_summary,
    get_db,
)

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# AI 评分缓存（topic_id -> {score, status, ...}）
_ai_score_cache = {}
_ai_cache_lock = threading.Lock()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class RecallHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        routes = {
            "/api/books": self._get_books,
            "/api/ollama-status": self._get_ollama_status,
            "/api/due-topics": self._get_due_topics,
            "/api/stats": self._get_stats,
            "/api/calendar": self._get_calendar,
            "/api/estimate-score": self._get_estimate_score,
            "/api/activity-log": self._get_activity_log,
            "/api/today-log": self._get_today_log,
            "/api/today-plan": self._get_today_plan,
            "/api/tomorrow-preview": self._get_tomorrow_preview,
            "/api/settings": self._get_settings,
            "/api/day-summary": self._get_day_summary,
            "/api/ai-score": self._get_ai_score,
        }
        if path in routes:
            routes[path](query)
        else:
            self._serve_static(path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        content_length = int(self.headers.get("Content-Length", 0))
        content_type = self.headers.get("Content-Type", "")
        body = self.rfile.read(content_length) if content_length > 0 else b""

        # multipart 特殊处理
        if "multipart/form-data" in content_type:
            self._handle_multipart(path, body, content_type)
            return

        routes = {
            "/api/upload-pdf": self._upload_pdf_json,
            "/api/study": self._record_study,
            "/api/start-session": self._start_session,
            "/api/end-session": self._end_session,
            "/api/update-book": self._update_book,
            "/api/delete-book": self._delete_book_post,
            "/api/shutdown-save": self._shutdown_save,
            "/api/day-summary": self._gen_day_summary,
            "/api/replan": self._replan,
        }
        if path in routes:
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                data = {}
            routes[path](data, content_type)
        else:
            self._json({"error": "Not Found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ============================================================
    # multipart 处理（文件上传）
    # ============================================================

    def _handle_multipart(self, path, body, content_type):
        if path != "/api/upload-pdf":
            self._json({"error": "Not Found"}, 404)
            return
        try:
            # 手动解析 multipart（比 cgi.FieldStorage 更可靠）
            boundary = content_type.split("boundary=")[-1].strip()
            if boundary.startswith('"') and boundary.endswith('"'):
                boundary = boundary[1:-1]
            boundary_bytes = boundary.encode()
            
            file_data = None
            file_name = ""
            fields = {}
            
            # 按 boundary 分割
            parts = body.split(b"--" + boundary_bytes)
            for part in parts:
                part = part.strip()
                if not part or part == b"--":
                    continue
                # 分离 headers 和 body
                if b"\r\n\r\n" in part:
                    header_section, part_body = part.split(b"\r\n\r\n", 1)
                elif b"\n\n" in part:
                    header_section, part_body = part.split(b"\n\n", 1)
                else:
                    continue
                
                header_text = header_section.decode("utf-8", errors="replace")
                
                # 解析 Content-Disposition
                name = ""
                fname = ""
                for line in header_text.split("\n"):
                    line = line.strip()
                    if line.lower().startswith("content-disposition"):
                        for seg in line.split(";"):
                            seg = seg.strip()
                            if seg.startswith("name="):
                                name = seg.split("=", 1)[1].strip('" ')
                            elif seg.startswith("filename="):
                                fname = seg.split("=", 1)[1].strip('" ')
                
                if fname and name == "pdf":
                    file_data = part_body
                    if file_data.endswith(b"\r\n"):
                        file_data = file_data[:-2]
                    file_name = fname
                elif name:
                    val = part_body.decode("utf-8", errors="replace").strip()
                    if val.endswith("\r\n"):
                        val = val[:-2]
                    fields[name] = val
            
            if not file_data or not file_name:
                self._json({"error": "未选择文件"}, 400)
                return
            
            filename = f"kb_{int(time.time())}_{file_name}"
            pdf_path = os.path.join(UPLOAD_DIR, filename)
            with open(pdf_path, "wb") as f:
                f.write(file_data)

            kb_name = fields.get("name", file_name.replace(".pdf", ""))
            target_days = int(fields.get("target_days", "0") or "0")
            exam_date = fields.get("exam_date", "") or None

            # --- SSE 流式响应 ---
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            # 进度回调
            def on_progress(current, total):
                self._sse_send({"type": "progress", "stage": "ocr",
                               "current": current, "total": total,
                               "percent": round(current / total * 100) if total else 0})

            # 提取文本
            self._sse_send({"type": "progress", "stage": "extract", "percent": 0})
            raw_text = extract_text_from_pdf(pdf_path, progress_cb=on_progress)
            if not raw_text.strip():
                self._sse_send({"type": "error", "error": "PDF 内容为空，请确认文件有文字内容"})
                return

            # 正则切题（不调用AI，快速可靠）
            self._sse_send({"type": "progress", "stage": "ai", "percent": 50})
            all_topics = self._parse_topics_regex(raw_text, kb_name)
            self._sse_send({"type": "progress", "stage": "ai", "percent": 100})

            if not all_topics:
                self._sse_send({"type": "error", "error": "AI 未能提取到知识点"})
                return

            # 保存
            book_id = add_book(kb_name, pdf_path, file_name,
                               target_days=target_days, exam_date=exam_date,
                               raw_text=raw_text)
            add_topics(book_id, all_topics)

            if target_days > 0:
                generate_daily_plan(book_id, target_days)

            log_activity("upload", {"book_id": book_id, "name": kb_name,
                                     "topic_count": len(all_topics), "target_days": target_days})

            # 后台：为每个知识点生成同义表达（AI 预处理，耗时但只跑一次）
            threading.Thread(
                target=self._background_generate_expressions,
                args=(book_id,),
                daemon=True).start()

            # 发送完成
            self._sse_send({
                "type": "done",
                "success": True, "book_id": book_id,
                "topic_count": len(all_topics),
                "message": f"解析完成：{len(all_topics)} 个知识点" +
                           (f"，已按 {target_days} 天分配" if target_days else "")})
        except Exception as e:
            try:
                self._sse_send({"type": "error", "error": str(e)})
            except Exception:
                pass

    def _upload_pdf_json(self, data, ct):
        """非 multipart 上传的 fallback"""
        self._json({"error": "请使用 FormData 上传文件"}, 400)

    # ============================================================
    # 书库
    # ============================================================

    def _get_books(self, query):
        books = get_books()
        result = []
        for b in books:
            b.pop("raw_text", None)  # 不返回 OCR 原文（太大，前端不需要）
            progress = get_book_progress(b["id"])
            result.append({**b, "progress": progress})
        self._json({"data": result})

    def _update_book(self, data, ct):
        try:
            book_id = data.get("id")
            if not book_id:
                self._json({"error": "缺少 id"}, 400)
                return
            kwargs = {}
            if "name" in data:
                kwargs["name"] = data["name"]
            if "target_days" in data:
                kwargs["target_days"] = data["target_days"]
            if "exam_date" in data:
                kwargs["exam_date"] = data["exam_date"]
            update_book(book_id, **kwargs)
            # 如果更新了天数，重新生成计划
            if "target_days" in data and data["target_days"]:
                generate_daily_plan(book_id, int(data["target_days"]))
            log_activity("update_book", {"book_id": book_id, **kwargs})
            self._json({"success": True})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _delete_book_post(self, data, ct):
        try:
            book_id = data.get("id")
            if not book_id:
                self._json({"error": "缺少 id"}, 400)
                return
            delete_book(book_id)
            log_activity("delete_book", {"book_id": book_id})
            self._json({"success": True})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    # ============================================================
    # 背诵
    # ============================================================

    def _get_due_topics(self, query):
        book_id = int(query.get("book_id", [0])[0])
        if not book_id:
            self._json({"error": "缺少 book_id"}, 400)
            return
        topics = get_due_topics(book_id)
        self._json({"data": topics})

    def _record_study(self, data, ct):
        try:
            topic_id = data.get("topic_id")
            book_id = data.get("book_id")
            recited_text = data.get("recited_text", "")
            is_correct = data.get("is_correct", False)

            if not topic_id or not book_id:
                self._json({"error": "缺少 topic_id 或 book_id"}, 400)
                return

            topic = get_topic_by_id(topic_id)
            if not topic:
                self._json({"error": "知识点不存在"}, 404)
                return

            # 同义表达评分（如果已预处理），秒出分数
            comparison = {}
            dims = {}
            score_val = 0
            if recited_text:
                key_exprs = topic.get("key_expressions", "")
                if key_exprs:
                    try:
                        expr_groups = json.loads(key_exprs) if isinstance(key_exprs, str) else key_exprs
                        comparison = score_by_expressions(recited_text, expr_groups)
                    except Exception:
                        pass
                # 如果没有同义表达，退回正则匹配
                if not comparison:
                    comparison = compare_recitation(
                        title=topic["title"],
                        key_points=topic.get("keywords", []),
                        original=topic.get("content", ""),
                        recited=recited_text)
                score_val = comparison.get("total", 0)
                is_correct = score_val >= 55
                dims = {
                    "completeness": comparison.get("completeness", 0),
                    "keypoints": comparison.get("keypoints", 0),
                    "accuracy": comparison.get("accuracy", 0),
                    "logic": comparison.get("logic", 0),
                    "depth": comparison.get("depth", 0),
                    "total": score_val,
                }

            # 更新记忆等级
            current_level = topic.get("memory_level", 0)
            progress = update_progress(topic_id, is_correct, current_level)
            update_topic_progress(topic_id, is_correct, progress["new_level"], progress["next_review_date"])

            # 保存记录
            add_study_record(
                topic_id, book_id, is_correct, recited_text,
                matched=comparison.get("matched_points", []),
                missing=[p.get("point", p) if isinstance(p, dict) else p
                         for p in comparison.get("missing_points", [])],
                score=score_val / 100.0,
                ai_analysis=json.dumps(comparison, ensure_ascii=False),
                dims=dims)

            log_activity("study", {"topic_id": topic_id, "score": score_val, "correct": is_correct})

            # 后台 AI 评分
            with _ai_cache_lock:
                _ai_score_cache[topic_id] = {"status": "pending", "score": None}
            threading.Thread(
                target=self._background_ai_score,
                args=(topic_id, topic.get("title", ""),
                      topic.get("keywords", []), topic.get("content", ""),
                      recited_text),
                daemon=True).start()

            self._json({
                "success": True,
                "is_correct": is_correct,
                "new_level": progress["new_level"],
                "next_review_date": progress["next_review_date"],
                "ai_pending": True,
                "comparison": comparison,
            })
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _background_ai_score(self, topic_id, title, key_points, original, recited):
        """后台线程：AI 评分，完成后写入缓存"""
        try:
            result = compare_recitation_ai(title, key_points, original, recited)
            with _ai_cache_lock:
                _ai_score_cache[topic_id] = {"status": "done", "score": result}
        except Exception as e:
            with _ai_cache_lock:
                _ai_score_cache[topic_id] = {"status": "error", "score": None, "error": str(e)}

    def _background_generate_expressions(self, book_id):
        """后台线程：为整本书的所有知识点生成同义表达（上传后触发）"""
        topics = get_topics(book_id)
        total = len(topics)
        print(f"[expr-gen] 开始为书 #{book_id} 生成同义表达，共 {total} 个知识点",
              file=sys.stderr, flush=True)
        for i, t in enumerate(topics):
            try:
                if t.get("key_expressions"):
                    continue  # 已生成过，跳过
                content = t.get("content", "")
                if len(content) < 30:
                    continue
                exprs = generate_key_expressions(content)
                if exprs:
                    conn = get_db()
                    conn.execute(
                        "UPDATE topics SET key_expressions=? WHERE id=?",
                        (json.dumps(exprs, ensure_ascii=False), t["id"]))
                    conn.commit()
                    conn.close()
                    print(f"[expr-gen] #{i+1}/{total}: {t['title'][:30]}… → {len(exprs)} 组",
                          file=sys.stderr, flush=True)
            except Exception as e:
                print(f"[expr-gen] #{i+1} 失败: {e}", file=sys.stderr, flush=True)
        print(f"[expr-gen] 书 #{book_id} 同义表达生成完成", file=sys.stderr, flush=True)

    def _get_ai_score(self, query):
        """GET /api/ai-score?topic_id=123 — 轮询 AI 评分结果"""
        topic_id = int(query.get("topic_id", [0])[0])
        if not topic_id:
            self._json({"error": "缺少 topic_id"}, 400)
            return
        with _ai_cache_lock:
            entry = _ai_score_cache.get(topic_id)
        if not entry:
            self._json({"status": "none"})
        else:
            self._json(entry)

    # ============================================================
    # 学习会话
    # ============================================================

    def _start_session(self, data, ct):
        try:
            session_id = start_study_session(data.get("book_id"))
            self._json({"session_id": session_id})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _end_session(self, data, ct):
        try:
            efficiency = end_study_session(
                data.get("session_id"), data.get("topics_studied", 0),
                data.get("correct", 0), data.get("wrong", 0),
                data.get("avg_score", 0))
            log_activity("session_end", {"session_id": data.get("session_id"), "efficiency": efficiency})
            self._json({"efficiency": efficiency})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    # ============================================================
    # 今日计划 + 明日预览
    # ============================================================

    def _get_today_plan(self, query):
        book_id = int(query.get("book_id", [0])[0])
        if not book_id:
            self._json({"error": "缺少 book_id"}, 400)
            return
        plan = get_today_plan(book_id)
        self._json({"data": plan})

    def _get_tomorrow_preview(self, query):
        book_id = int(query.get("book_id", [0])[0])
        if not book_id:
            self._json({"error": "缺少 book_id"}, 400)
            return
        # 查明天计划
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        conn = get_db()
        topics = conn.execute(
            "SELECT id, title, section, difficulty FROM topics WHERE book_id=? AND plan_date=? ORDER BY topic_index LIMIT 20",
            (book_id, tomorrow)).fetchall()
        review = conn.execute(
            "SELECT id, title, section FROM topics WHERE book_id=? AND next_review_date=? AND memory_level>0 ORDER BY topic_index LIMIT 20",
            (book_id, tomorrow)).fetchall()
        conn.close()
        self._json({"data": {
            "date": tomorrow,
            "new_topics": [dict(t) for t in topics],
            "review_topics": [dict(t) for t in review],
            "new_count": len(topics),
            "review_count": len(review),
        }})

    # ============================================================
    # 每日总结
    # ============================================================

    def _get_day_summary(self, query):
        book_id = int(query.get("book_id", [0])[0])
        date = query.get("date", [datetime.now().strftime("%Y-%m-%d")])[0]
        if not book_id:
            self._json({"error": "缺少 book_id"}, 400)
            return
        summary = get_day_summary(book_id, date)
        self._json({"data": summary})

    def _gen_day_summary(self, data, ct):
        try:
            book_id = data.get("book_id")
            if not book_id:
                self._json({"error": "缺少 book_id"}, 400)
                return
            # 获取今日背诵记录
            today = datetime.now().strftime("%Y-%m-%d")
            conn = get_db()
            rows = conn.execute(
                """SELECT sr.*, t.title FROM study_records sr
                   JOIN topics t ON sr.topic_id = t.id
                   WHERE sr.book_id=? AND DATE(sr.study_date)=?
                   ORDER BY sr.study_date""",
                (book_id, today)).fetchall()
            conn.close()
            records = []
            for r in rows:
                d = dict(r)
                d["total_score"] = (d.get("total_score", 0) or 0)
                if d["total_score"] == 0 and d.get("score", 0) > 0:
                    d["total_score"] = d["score"] * 100
                records.append(d)

            # 明日预览
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            conn = get_db()
            tmr = conn.execute(
                "SELECT COUNT(*) as c FROM topics WHERE book_id=? AND plan_date=?",
                (book_id, tomorrow)).fetchone()
            tmr_count = dict(tmr)["c"] if tmr else 0
            conn.close()
            tomorrow_info = f"明日计划新学 {tmr_count} 个知识点"

            # AI 总结
            summary = generate_day_summary(records, tomorrow_info)

            # 保存到 DB
            save_day_summary(book_id, today, summary)
            mark_plan_completed(book_id, today)

            log_activity("day_summary", {"book_id": book_id, "topics": len(records)})
            self._json({"data": summary})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    # ============================================================
    # 重新规划
    # ============================================================

    def _replan(self, data, ct):
        try:
            book_id = data.get("book_id")
            target_days = data.get("target_days")
            if not book_id or not target_days:
                self._json({"error": "缺少参数"}, 400)
                return
            update_book(book_id, target_days=target_days)
            generate_daily_plan(book_id, target_days)
            log_activity("replan", {"book_id": book_id, "target_days": target_days})
            self._json({"success": True, "message": f"已按 {target_days} 天重新规划"})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    # ============================================================
    # 统计 / 日历 / 预估
    # ============================================================

    def _get_stats(self, query):
        book_id = int(query.get("book_id", [0])[0])
        if not book_id:
            self._json({"error": "缺少 book_id"}, 400)
            return
        stats = get_study_stats(book_id)
        self._json({"data": stats})

    def _get_calendar(self, query):
        year = int(query.get("year", [datetime.now().year])[0])
        month = int(query.get("month", [datetime.now().month])[0])
        book_id = int(query.get("book_id", [0])[0]) or None
        data = get_calendar_data(year, month, book_id)
        self._json({"data": data})

    def _get_estimate_score(self, query):
        book_id = int(query.get("book_id", [0])[0])
        if not book_id:
            self._json({"error": "缺少 book_id"}, 400)
            return
        score = estimate_score(book_id)
        self._json({"data": score})

    # ============================================================
    # Ollama / 日志 / 设置
    # ============================================================

    def _get_ollama_status(self, query=None):
        status = check_ollama()
        self._json({"data": status})

    def _get_activity_log(self, query):
        limit = int(query.get("limit", [50])[0])
        logs = get_activity_log(limit)
        self._json({"data": logs})

    def _get_today_log(self, query=None):
        logs = get_today_activity()
        self._json({"data": logs})

    def _get_settings(self, query):
        self._json({"data": {
            "ollama_model": get_setting("ollama_model", "qwen3.5:2b"),
            "auto_close_seconds": get_setting("auto_close_seconds", "2"),
        }})

    # ============================================================
    # 关机保护
    # ============================================================

    def _shutdown_save(self, data=None, ct=None):
        backup_database()
        save_daily_summary()
        self._json({"success": True, "message": "数据已保存"})

    def _parse_topics_regex(self, raw_text, section_name):
        """正则切题：两层结构 — 先按一级标题分段，段内只切顶层编号
        
        核心逻辑：
        1. 先按一级标题（一、二、三…）或顶层编号（1. 2. 3.）分大段
        2. 每段内，(1)(2)(3) 是子要点，不单独切题
        3. 只有在没有顶层编号时，才把 (1)(2) 当题目
        """
        # 清洗水印
        text = re.sub(r'微信搜索公众号.*?\n', '', raw_text)
        text = re.sub(r'记乎APP.*?\n', '', text)
        text = re.sub(r'\d+\s*/\s*\d+\s*\n', '', text)
        text = re.sub(r'银河研旅.*?\n', '', text)
        text = re.sub(r'{.*?}\n', '', text)
        text = re.sub(r'途中口腔医学考研.*?\n', '', text)
        text = re.sub(r'\{笔记\}.*?\n', '', text)

        def make_topic(content, idx, sec):
            """从一段文本生成一个 topic dict"""
            content = content.strip()
            if len(content) < 10:
                return None
            first_line = content.split('\n')[0].strip()[:80]
            title = re.sub(r'[★sS]{1,5}', '', first_line).strip()
            title = re.sub(r'[a-zA-Z]+(?:/[a-zA-Z]+)*[:：]?\s*', '', title).strip()
            title = re.sub(r'\(\d+\)', '', title).strip()
            if not title or len(title) < 2:
                title = first_line[:40]
            if len(title) > 60:
                title = title[:60]
            stars = content[:200].count('★')
            difficulty = min(5, max(2, stars + 2)) if stars > 0 else 3
            # 提取子要点 (1)(2)(3) ①②③
            key_points = []
            for line in content.split('\n'):
                line = line.strip()
                m = re.match(r'^[（(\u2460-\u2468]\d*[）)]?\s*(.+)', line)
                if not m:
                    m = re.match(r'^[①②③④⑤⑥⑦⑧⑨⑩]\s*(.+)', line)
                if m and len(m.group(1)) > 5:
                    key_points.append(m.group(1)[:80])
            return {
                "id": idx, "title": title, "content": content,
                "keywords": key_points[:5], "section": sec, "difficulty": difficulty,
            }

        # ===== 策略1：按顶层编号切（1. 2. 3. 或 1） 2） 3））=====
        # 要求有明确分隔符（. ． 、 ） )），不匹配 (1) 因为 ( 不是 \s
        topics = []
        parts = re.split(r'(?:^|\n)\s*(\d{1,3})\s*[.．、）)]\s*', text)
        if len(parts) >= 3:
            for i in range(1, len(parts) - 1, 2):
                t = make_topic(parts[i + 1] if i + 1 < len(parts) else "", len(topics) + 1, section_name)
                if t:
                    topics.append(t)

        # ===== 策略2：按一级标题切（一、 二、 三．...）=====
        if len(topics) < 3:
            parts2 = re.split(r'(?:^|\n)\s*([（(]?[一二三四五六七八九十]+[）).．、])\s*(?=[\u4e00-\u9fff]{2,})', text)
            if len(parts2) >= 5:
                topics2 = []
                for i in range(1, len(parts2) - 1, 2):
                    t = make_topic(parts2[i + 1] if i + 1 < len(parts2) else "", len(topics2) + 1, section_name)
                    if t:
                        topics2.append(t)
                if len(topics2) > len(topics):
                    topics = topics2

        # ===== 策略3：括号编号 — 仅当上面都失败时兜底 =====
        if len(topics) < 2:
            parts3 = re.split(r'(?:^|\n)\s*[（(](\d{1,3})[）)]\s+', text)
            if len(parts3) >= 3:
                topics3 = []
                for i in range(1, len(parts3) - 1, 2):
                    t = make_topic(parts3[i + 1] if i + 1 < len(parts3) else "", len(topics3) + 1, section_name)
                    if t:
                        topics3.append(t)
                if len(topics3) > len(topics):
                    topics = topics3

        # ===== 后处理：拆分超长topic（>2000字）=====
        final_topics = []
        for t in topics:
            if len(t["content"]) > 2000:
                # 只按带句点的编号拆（不拆括号编号）
                inner = re.split(r'(?:^|\n)\s*(\d{1,3})\s*[.．、]\s*', t["content"])
                if len(inner) >= 5:
                    for j in range(1, len(inner) - 1, 2):
                        st = make_topic(inner[j + 1] if j + 1 < len(inner) else "", len(final_topics) + 1, section_name)
                        if st:
                            final_topics.append(st)
                else:
                    final_topics.append(t)
            else:
                final_topics.append(t)

        # 兜底：整段做一个 topic
        if not final_topics and len(raw_text.strip()) > 10:
            final_topics.append({
                "id": 1, "title": section_name,
                "content": raw_text.strip()[:5000],
                "keywords": [], "section": section_name, "difficulty": 3
            })

        return final_topics

    # ============================================================
    # 静态文件
    # ============================================================

    def _serve_static(self, path):
        if path == "/":
            path = "/index.html"
        base = os.path.dirname(__file__)
        if path.startswith("/static/"):
            file_path = os.path.join(base, path[1:])  # 去掉前导 /
        else:
            file_path = os.path.join(base, "templates", path.lstrip("/"))
        if not os.path.exists(file_path):
            file_path = os.path.join(base, "templates", "index.html")
        ext = os.path.splitext(file_path)[1].lower()
        ct_map = {".html": "text/html", ".css": "text/css", ".js": "application/javascript",
                  ".png": "image/png", ".jpg": "image/jpeg", ".json": "application/json"}
        content_type = ct_map.get(ext, "text/html") + "; charset=utf-8"
        try:
            with open(file_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(content)
        except Exception:
            self._json({"error": "File not found"}, 404)

    # ============================================================
    # 工具
    # ============================================================

    def _json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _sse_send(self, data):
        """发送一条 SSE 消息"""
        msg = json.dumps(data, ensure_ascii=False)
        self.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
        self.wfile.flush()

    def log_message(self, format, *args):
        if "200" not in str(args):
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {format % args}")


def main():
    init_db()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8088

    status = check_ollama()
    if status["running"]:
        print(f"✅ Ollama 运行中，可用模型: {status['models']}")
    else:
        print("⚠️  Ollama 未运行，请先启动 Ollama")

    backup_database()

    def graceful_shutdown(signum, frame):
        print("\n💾 正在保存数据...")
        backup_database()
        save_daily_summary()
        print("✅ 数据已保存，再见！")
        sys.exit(0)

    signal.signal(signal.SIGINT, graceful_shutdown)
    # 不捕获 SIGTERM，让系统正常管理进程

    server = ThreadedHTTPServer(("0.0.0.0", port), RecallHandler)
    print(f"🦷 口腔医学考研带背系统 v3 已启动")
    print(f"📍 http://localhost:{port}")
    print(f"按 Ctrl+C 安全退出（自动保存）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        graceful_shutdown(None, None)
        server.server_close()


if __name__ == "__main__":
    main()
