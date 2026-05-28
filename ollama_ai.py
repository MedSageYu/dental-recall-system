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

SCORE_SYSTEM = """你是口腔医学考研阅卷老师。对学生背诵进行评分。

## 评分规则（必须严格执行）

### 第一步：提取要点
从原文提取所有编号子要点（(1)(2)(3) ①②③ 等），列出清单。

### 第二步：逐个判断
对每个要点，判断学生是否覆盖：
- covered：核心意思已表达（允许口语化、同义替换）
- partial：提到了但不完整或有轻微错误
- missed：完全未提及

### 第三步：逐维度打分（每个 0-100，独立打分）
1. completeness（完整性）：covered 数 / 总要点数 × 100
2. keypoints（考点覆盖）：关键考点（标★的）覆盖比例 × 100
3. accuracy（准确性）：学生表述是否有错误，有错误扣分
4. logic（条理性）：背诵是否有条理、分点
5. depth（理解深度）：是否展现了对知识的理解，而非死记硬背

### 第四步：计算总分（你不能直接给总分！必须从子分算）
total = completeness × 0.25 + keypoints × 0.30 + accuracy × 0.20 + logic × 0.125 + depth × 0.125
四舍五入取整。

### 硬性约束
- 严禁给 total 加分或减分，total 必须等于子分加权
- 大多数背诵在 40-65 分之间，给 80+ 必须是真正优秀的背诵
- 口语化表达算覆盖（“角化变少” = “角化程度降低”）
- 不要因为文采好就加分，也不要因为口语化扣分"""

SCORE_PROMPT = """按以下步骤评分，严格按 JSON 输出：

## 原文要点：{key_points}
## 知识点：{title}

## 原文：
{original}

## 学生背诵：
{recited}

## 输出格式（严格 JSON，不要输出其他内容）：
```json
{{
  "points": ["要点1", "要点2", "要点3"],
  "evals": [
    {{"p": "要点1", "s": "covered", "e": "学生说了xxx"}},
    {{"p": "要点2", "s": "missed", "e": "未提及"}}
  ],
  "covered": 3,
  "partial": 1,
  "missed": 2,
  "completeness": 50,
  "keypoints": 45,
  "accuracy": 60,
  "logic": 55,
  "depth": 50,
  "total": 51,
  "matched": ["已覆盖的要点1"],
  "missing": [{{"point": "遗漏要点", "imp": "high", "tip": "记忆建议"}}],
  "comment": "一句话评价"
}}
```"""


# ============================================================
# 上传时预处理：AI 提取同义表达（只跑一次）
# ============================================================

EXPR_SYSTEM = """你是口腔医学教师。你的任务是从课本知识点中提取"关键考点"，并为每个考点列出可能的同义表达。

规则：
1. 找出原文中的编号子要点（(1)(2)(3)或①②③等），每个作为一组
2. 每组列出3-6个同义表达，覆盖：口语化说法、缩写、不同表述方式
3. 同义表达要短（5-20字），用于快速正则匹配
4. 不改变原意，不编造内容
"""

EXPR_PROMPT = """从以下口腔医学知识点中提取关键考点和同义表达：

{content}

严格 JSON 格式输出：
```json
{{
  "groups": [
    {{
      "point": "原始子要点文字",
      "exprs": ["同义表达1", "同义表达2", "同义表达3"]
    }}
  ]
}}
```"""


def generate_key_expressions(topic_content: str) -> list:
    """调用 AI 为知识点生成同义表达组

    Returns:
        [
            {"point": "牙龈上皮角化程度降低",
             "exprs": ["角化降低", "角化减少", "角化变少", "角化程度下降"]},
            ...
        ]
    """
    if not topic_content or len(topic_content) < 30:
        return []
    try:
        response = _call_ollama(
            EXPR_PROMPT.format(content=topic_content[:3000]),
            system=EXPR_SYSTEM, temperature=0.1)
        result = _extract_json(response)
        groups = result.get("groups", [])
        # 过滤无效的
        valid = []
        for g in groups:
            if g.get("point") and g.get("exprs"):
                exprs = [e.strip() for e in g["exprs"] if len(e.strip()) >= 2]
                if exprs:
                    valid.append({"point": g["point"][:100], "exprs": exprs})
        return valid
    except Exception as e:
        print(f"[generate_key_expressions] error: {e}", file=sys.stderr)
        return []


# ============================================================
# 答题时评分：基于预处理的同义表达（秒出分）
# ============================================================

def score_by_expressions(recited_text: str, key_expressions: list) -> dict:
    """基于预处理的同义表达做快速正则评分

    key_expressions: [{point, exprs}, ...]
    每个 group 中只要有任一同义表达被背诵覆盖，该点 = 满分
    """
    if not key_expressions:
        # 无同义表达时，退回纯字级匹配
        return None

    recited = recited_text.strip().lower()
    if not recited:
        return {"total": 0, "matched_points": [], "missing_points": [],
                "completeness": 0, "keypoints": 0, "accuracy": 0,
                "logic": 0, "depth": 0, "coverage": "需加强",
                "comment": "未输入背诵内容", "source": "expr"}

    matched = []
    missing = []
    for group in key_expressions:
        point = group.get("point", "")
        exprs = group.get("exprs", [])
        hit = False
        for expr in exprs:
            if len(expr) >= 2 and expr.lower() in recited:
                hit = True
                break
        if hit:
            matched.append(point[:60])
        else:
            missing.append({
                "point": point[:80],
                "importance": "high" if "★" in point else "medium",
                "suggestion": "请背诵此要点"
            })

    total_points = len(matched) + len(missing)
    if total_points == 0:
        score = 0
    else:
        score = round(len(matched) / total_points * 100)

    return {
        "completeness": score,
        "keypoints": score,
        "accuracy": max(0, score - 10),
        "logic": max(0, score - 5),
        "depth": max(0, score - 5),
        "total": score,
        "score": score,
        "matched_points": matched,
        "missing_points": missing,
        "comment": f"覆盖 {len(matched)}/{total_points} 个要点",
        "coverage": ("优秀" if score >= 85 else "良好" if score >= 70 else
                      "一般" if score >= 55 else "需加强"),
        "source": "expr"
    }


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
    """AI 语义评分（后台调用）
    
    输出格式：5 个维度独立打分，total = 加权求和
    权重：completeness×0.25 + keypoints×0.30 + accuracy×0.20 + logic×0.125 + depth×0.125
    """
    prompt = SCORE_PROMPT.format(
        title=title,
        key_points="、".join(key_points) if key_points else "无",
        original=original[:2500],
        recited=recited[:1500])
    response = _call_ollama(prompt, system=SCORE_SYSTEM, temperature=0.1)
    result = _extract_json(response)

    # 兼容新旧字段名
    covered = result.get("covered", result.get("covered_count", 0))
    partial = result.get("partial", result.get("partial_count", 0))
    missed = result.get("missed", result.get("missed_count", 0))
    matched = result.get("matched", result.get("matched_points", []))
    missing = result.get("missing", result.get("missing_points", []))
    comment = result.get("comment", "")

    # 读取 5 个维度分
    c = result.get("completeness", 0)
    k = result.get("keypoints", 0)
    a = result.get("accuracy", 0)
    l = result.get("logic", 0)
    d = result.get("depth", 0)
    total = result.get("total", 0)

    # 如果 AI 没给 total 或 total=0，强制从子分算
    if total == 0 and (c + k + a + l + d) > 0:
        total = round(c * 0.25 + k * 0.30 + a * 0.20 + l * 0.125 + d * 0.125)

    # 验证 total 是否等于子分加权（允许±3误差）
    expected = round(c * 0.25 + k * 0.30 + a * 0.20 + l * 0.125 + d * 0.125)
    if abs(total - expected) > 3 and expected > 0:
        total = expected  # 强制用子分算的值

    total = max(0, min(100, total))

    return {
        "completeness": c, "keypoints": k, "accuracy": a,
        "logic": l, "depth": d, "total": total, "score": total,
        "matched_points": matched if isinstance(matched, list) else [],
        "missing_points": missing if isinstance(missing, list) else [],
        "comment": comment,
        "covered_count": covered, "partial_count": partial, "missed_count": missed,
        "coverage": ("优秀" if total >= 85 else "良好" if total >= 70 else
                      "一般" if total >= 55 else "需加强"),
        "source": "ai",
    }
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
