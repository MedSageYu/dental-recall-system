"""
Ollama 本地模型接口 v3
新增：多维度评分、每日总结、大文件分块合并
"""
import json
import urllib.request
import urllib.error
from typing import Optional


OLLAMA_BASE = "http://localhost:11434"
DEFAULT_MODEL = "qwen3.5:2b"


def _call_ollama(prompt: str, model: str = None,
                 system: str = "", temperature: float = 0.3,
                 images: list = None) -> str:
    model = model or DEFAULT_MODEL
    url = f"{OLLAMA_BASE}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "system": system,
        "stream": False,
        "think": False,  # 禁用思考模式（大幅提速）
        "options": {"temperature": temperature, "num_predict": 4096}
    }
    if images:
        payload["images"] = images
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("response", "")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ollama 连接失败: {e}")
    except Exception as e:
        raise RuntimeError(f"Ollama 调用失败: {e}")


def check_ollama() -> dict:
    try:
        url = f"{OLLAMA_BASE}/api/tags"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = [m["name"] for m in data.get("models", [])]
            return {"running": True, "models": models}
    except Exception:
        return {"running": False, "models": []}


def _extract_json(text: str) -> dict:
    """从 AI 回复中提取 JSON"""
    for marker in ["```json", "```"]:
        if marker in text:
            parts = text.split(marker)
            if len(parts) >= 2:
                json_str = parts[1].split("```")[0].strip()
                try:
                    result = json.loads(json_str)
                    return result
                except json.JSONDecodeError:
                    pass
    # 尝试直接解析
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        # 尝试找第一个 { 到最后一个 }
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    print(f"[_extract_json] Failed, attempting repair...", file=sys.stderr, flush=True)
    # 尝试修复截断的 JSON
    start = text.find("{")
    if start >= 0:
        partial = text[start:]
        # 找到最后一个完整的 } 对象
        depth = 0
        last_valid = -1
        for i, c in enumerate(partial):
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    last_valid = i
                    break
        if last_valid >= 0:
            try:
                return json.loads(partial[:last_valid + 1])
            except json.JSONDecodeError:
                pass
    print(f"[_extract_json] All attempts failed", file=sys.stderr, flush=True)
    return {}


# ============================================================
# PDF 知识点提取（分块 + 合并）
# ============================================================

SUMMARIZE_SYSTEM = """你是一位口腔医学考研辅导专家。你的任务是从 OCR 识别的文字中提取知识点。

核心原则：
1. **保留原始文字** — 你是在处理 OCR 识别结果，文字可能有错别字
2. **只修正明显的 OCR 错误** — 如“牙槽嵴”写成“牙槽骑”、“粘膜”写成“粘莫”等明显错字
3. **不要改写、压缩、概括原文** — 保持原始表述和专业术语的完整说法
4. **content 字段直接用修正后的原文片段**，不要重新组织语言
5. 严格 JSON 输出。"""

SUMMARIZE_PROMPT = """请从以下 OCR 识别的口腔医学教材文字中提取知识点。

⚠️ 重要规则：
- 这是 OCR 识别结果，可能有错别字，请修正明显错误后保留原文
- content 字段必须使用修正后的原文，不要改写、压缩或重新组织
- 保持原始的专业术语和表述方式
- 每个知识点包含：title（简短标题）、content（修正后的原文内容）、key_points（3-5个关键考点）、difficulty（难度1-5）

原始 OCR 文字：
{text}

严格按 JSON 格式输出：
```json
{{
  "sections": [
    {{
      "section_name": "章节名称",
      "topics": [
        {{
          "title": "标题",
          "content": "修正后的原文内容",
          "key_points": ["考点1", "考点2"],
          "difficulty": 3
        }}
      ]
    }}
  ]
}}
```"""

MERGE_PROMPT = """以下是从口腔医学教材不同段落提取的知识点列表。请合并去重、调整分组，输出最终版本。

已提取的知识点：
{text}

要求：
1. 合并内容重复的知识点
2. 按主题重新分组（如果需要）
3. 保持每个知识点的完整性
4. 输出严格 JSON：
```json
{{
  "sections": [
    {{
      "section_name": "章节名称",
      "topics": [
        {{
          "title": "标题",
          "content": "内容",
          "key_points": ["考点1", "考点2"],
          "difficulty": 3
        }}
      ]
    }}
  ]
}}
```"""


def summarize_topics(raw_text: str, progress_cb=None) -> dict:
    """将 PDF 文本转为结构化知识点（分块处理 + 合并）
    progress_cb: 可选回调 progress_cb(current_chunk, total_chunks)
    """
    chunk_size = 3000
    chunks = []
    lines = raw_text.split("\n")
    current_chunk = []
    for line in lines:
        current_chunk.append(line)
        if len("\n".join(current_chunk)) > chunk_size:
            chunks.append("\n".join(current_chunk))
            current_chunk = []
    if current_chunk:
        chunks.append("\n".join(current_chunk))

    all_sections = []
    for i, chunk in enumerate(chunks):
        if progress_cb:
            progress_cb(i + 1, len(chunks))
        prompt = SUMMARIZE_PROMPT.format(text=chunk)
        response = _call_ollama(prompt, system=SUMMARIZE_SYSTEM, temperature=0.2)
        data = _extract_json(response)
        if "sections" in data:
            all_sections.extend(data["sections"])

    # 如果有多个块，做一次合并
    if len(chunks) > 1 and all_sections:
        if progress_cb:
            progress_cb(len(chunks), len(chunks))  # 合并阶段
        try:
            merge_text = json.dumps(all_sections, ensure_ascii=False, indent=1)
            if len(merge_text) > 6000:
                merge_text = merge_text[:6000] + "\n...(已截断)"
            merge_prompt = MERGE_PROMPT.format(text=merge_text)
            merge_resp = _call_ollama(merge_prompt, system=SUMMARIZE_SYSTEM, temperature=0.1)
            merged = _extract_json(merge_resp)
            if "sections" in merged:
                all_sections = merged["sections"]
        except Exception:
            pass

    return {"sections": all_sections}


# ============================================================
# 多维度背诵评分（核心新功能）
# ============================================================

SCORE_SYSTEM = """你是一位口腔医学考研辅导老师。对学生背诵进行多维度评分。

评分维度（每个0-100分）：
1. completeness（知识完整性）：是否覆盖了原文的主要知识点
2. keypoints（关键考点覆盖）：核心考点是否提到
3. accuracy（表述准确性）：关键概念是否表述正确
4. logic（逻辑条理性）：背诵是否有条理、有逻辑
5. depth（理解深度）：是否展现对知识的深入理解，而非简单复述

注意：
- 不要求一字一句一致，意思对即可
- 用不同表述覆盖相同意思，算覆盖
- 评分要有区分度：差的给30-50，一般的50-70，好的70-90，优秀的90+
- 避免所有评分都集中在70-90"""

SCORE_PROMPT = """请对比【背诵内容】和【原文知识点】，进行多维度评分。

知识点：{title}
关键考点：{key_points}

原文：
{original}

学生背诵：
{recited}

严格按 JSON 格式输出：
```json
{{
  "completeness": 75,
  "keypoints": 80,
  "accuracy": 70,
  "logic": 65,
  "depth": 60,
  "total": 71,
  "matched_points": ["已覆盖的考点"],
  "missing_points": [
    {{
      "point": "遗漏考点",
      "importance": "high/medium/low",
      "suggestion": "记忆建议"
    }}
  ],
  "comment": "总体评价"
}}
```"""



import re


def strict_compare_recitation(title: str, original: str, recited: str) -> dict:
    """严格逐字匹配评分（不依赖AI）
    
    核心逻辑：提取原文所有中文字符 → 与背诵内容做字级匹配
    输入"一" → 得分接近0
    完整背诵 → 得分接近100
    """
    # 1. 提取原文中的所有中文字符作为"标准字符集"
    original_chars = set(re.findall(r'[\u4e00-\u9fff]', original))
    recited_chars = set(re.findall(r'[\u4e00-\u9fff]', recited))
    
    if not original_chars:
        return {"total": 0, "score": 0, "comment": "原文无有效内容",
                "matched_points": [], "missing_points": [], "coverage": "无"}
    
    # 2. 字级命中率
    hit_chars = original_chars & recited_chars
    char_hit_rate = len(hit_chars) / len(original_chars)
    
    # 3. 关键句提取与逐句匹配
    key_sentences = []
    for line in original.split('\n'):
        line = line.strip()
        # 匹配: (1) (1） 1） （1） ① 等子条目
        m = re.match(r'^\s*[（(]?\d+[）)]\s*(.+)', line)
        if not m:
            m = re.match(r'^\s*[（(][一二三四五六七八九十]+[）)]\s*(.+)', line)
        if not m:
            m = re.match(r'^\s*[①②③④⑤⑥⑦⑧⑨⑩]\s*(.+)', line)
        if m and len(m.group(1)) > 5:
            key_sentences.append(m.group(1).strip())
    
    if not key_sentences:
        lines = [l.strip() for l in original.split('\n') if l.strip()]
        key_sentences = [lines[i] for i in range(1, min(5, len(lines)))]
    
    # 4. 逐句评分
    matched_points = []
    missing_points = []
    sentence_scores = []
    
    for ks in key_sentences:
        ks_chars = set(re.findall(r'[\u4e00-\u9fff]', ks))
        if not ks_chars:
            continue
        ks_hit = ks_chars & recited_chars
        sentence_rate = len(ks_hit) / len(ks_chars)
        sentence_score = round(sentence_rate * 100)
        sentence_scores.append(sentence_score)
        
        if sentence_score >= 50:
            matched_points.append(ks[:60] + ('...' if len(ks) > 60 else ''))
        else:
            imp = "high" if sentence_score < 20 else "medium"
            missing_points.append({
                "point": ks[:80],
                "importance": imp,
                "suggestion": "请重点背诵此要点"
            })
    
    # 5. 综合得分
    if sentence_scores:
        total_score = round(char_hit_rate * 100 * 0.5 + round(sum(sentence_scores) / len(sentence_scores)) * 0.5)
    else:
        # 没有关键句时，纯靠字级命中率
        total_score = round(char_hit_rate * 100)
    total_score = max(0, min(100, total_score))
    
    # 6. 评价
    if total_score >= 85:
        comment = "背诵完整准确，关键点全覆盖。"
        coverage = "优秀"
    elif total_score >= 60:
        comment = f"背诵覆盖了{len(matched_points)}个关键点，遗漏{len(missing_points)}个。"
        coverage = "良好"
    elif total_score >= 30:
        comment = f"背诵不完整，仅覆盖{len(matched_points)}个关键点，请加强。"
        coverage = "需加强"
    else:
        comment = "背诵严重缺失，请对照原文重新背诵。"
        coverage = "不及格"
    
    return {
        "completeness": total_score,
        "keypoints": total_score,
        "accuracy": total_score,
        "logic": total_score,
        "depth": total_score,
        "total": total_score,
        "score": total_score,
        "matched_points": matched_points,
        "missing_points": missing_points,
        "comment": comment,
        "coverage": coverage,
        "char_hit_rate": round(char_hit_rate * 100, 1),
        "sentence_scores": sentence_scores
    }


def compare_recitation(title: str, key_points: list, original: str,
                       recited: str) -> dict:
    """多维度 AI 语义对比（已弃用，改用 strict_compare_recitation）"""
    # 直接调用严格比对，不再走 AI
    return strict_compare_recitation(title, original, recited)


def compare_recitation_ai(title: str, key_points: list, original: str,
                       recited: str) -> dict:
    """多维度 AI 语义对比（保留原版，作为备用）"""
    prompt = SCORE_PROMPT.format(
        title=title,
        key_points="、".join(key_points) if key_points else "无",
        original=original[:2000],
        recited=recited)
    response = _call_ollama(prompt, system=SCORE_SYSTEM, temperature=0.2)
    result = _extract_json(response)
    # 确保有默认值
    defaults = {"completeness": 0, "keypoints": 0, "accuracy": 0, "logic": 0,
                "depth": 0, "total": 0, "matched_points": [], "missing_points": [],
                "comment": "", "score": 0}
    for k, v in defaults.items():
        if k not in result:
            result[k] = v
    # 计算加权总分（如果没有）
    if result["total"] == 0:
        result["total"] = round(
            result["completeness"] * 0.2 +
            result["keypoints"] * 0.3 +
            result["accuracy"] * 0.2 +
            result["logic"] * 0.15 +
            result["depth"] * 0.15, 1)
    # 兼容旧接口
    result["score"] = result["total"]
    result["coverage"] = "优秀" if result["total"] >= 85 else "良好" if result["total"] >= 70 else "一般" if result["total"] >= 55 else "需加强"
    return result


# ============================================================
# 每日总结生成
# ============================================================

DAY_SUMMARY_SYSTEM = """你是口腔医学考研辅导老师。根据学生今日背诵情况生成每日总结。
要求：简洁、鼓励为主、指出具体薄弱点、给出明日建议。"""

DAY_SUMMARY_PROMPT = """请根据以下今日背诵数据生成总结报告。

今日背诵：{topics_studied} 个知识点
平均分：{avg_score}分
最高分：{max_score}分
最低分：{min_score}分
正确率：{correct_rate}%

各知识点详情：
{topic_details}

明日计划预览：
{tomorrow_info}

请输出：
```json
{{
  "summary": "今日学习总结（2-3句话）",
  "strengths": ["做得好的方面"],
  "weaknesses": ["需要加强的方面"],
  "suggestion": "明日学习建议",
  "encouragement": "鼓励的话"
}}
```"""


def generate_day_summary(study_records: list, tomorrow_info: str = "") -> dict:
    """生成每日总结报告"""
    if not study_records:
        return {"summary": "今日暂无背诵记录", "strengths": [], "weaknesses": [], "suggestion": "", "encouragement": ""}

    topics_studied = len(study_records)
    scores = [r.get("total_score", 0) or r.get("score", 0) * 100 for r in study_records]
    avg_score = round(sum(scores) / len(scores), 1) if scores else 0
    max_score = max(scores) if scores else 0
    min_score = min(scores) if scores else 0
    correct_count = sum(1 for r in study_records if r.get("is_correct"))
    correct_rate = round(correct_count / topics_studied * 100, 1) if topics_studied else 0

    details = []
    for r in study_records[:10]:  # 最多显示10个
        details.append(f"- {r.get('title', '未知')}: {r.get('total_score', 0):.0f}分")
    topic_details = "\n".join(details)

    prompt = DAY_SUMMARY_PROMPT.format(
        topics_studied=topics_studied,
        avg_score=avg_score,
        max_score=max_score,
        min_score=min_score,
        correct_rate=correct_rate,
        topic_details=topic_details,
        tomorrow_info=tomorrow_info or "暂无明日计划")

    response = _call_ollama(prompt, system=DAY_SUMMARY_SYSTEM, temperature=0.4)
    result = _extract_json(response)

    result.setdefault("summary", "")
    result.setdefault("strengths", [])
    result.setdefault("weaknesses", [])
    result.setdefault("suggestion", "")
    result.setdefault("encouragement", "")
    result["topics_studied"] = topics_studied
    result["avg_score"] = avg_score
    result["max_score"] = max_score
    result["min_score"] = min_score
    result["correct_rate"] = correct_rate
    return result


# ============================================================
# 薄弱点笔记生成
# ============================================================

def generate_weakness_notes(title: str, missing_points: list, weak_areas: list) -> str:
    missing_text = "\n".join(
        f"- {p.get('point', p) if isinstance(p, dict) else p}"
        for p in missing_points)
    prompt = f"""根据以下遗漏考点生成复习笔记：
知识点：{title}
遗漏考点：
{missing_text}
薄弱领域：{'、'.join(weak_areas) if weak_areas else '无'}

要求：简洁解释 + 记忆技巧 + 考点关联。Markdown 格式。"""
    return _call_ollama(prompt, system="你是口腔医学考研辅导老师。", temperature=0.4)


# ============================================================
# 多模态图片分析
# ============================================================

def analyze_image(image_b64: str, question: str = "请描述这张图片中的口腔状况") -> str:
    system = "你是口腔医学专家，擅长分析口腔图片。"
    return _call_ollama(question, system=system, images=[image_b64], temperature=0.3)


if __name__ == "__main__":
    status = check_ollama()
    print(f"Ollama: {'运行中' if status['running'] else '未运行'}")
    print(f"模型: {status['models']}")
