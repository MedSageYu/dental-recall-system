"""
口腔医学考研带背系统 - PDF解析模块
使用 PyMuPDF 提取文本，RapidOCR 处理扫描版 PDF
"""
import re
import os
import math
import tempfile
from collections import Counter, defaultdict

import fitz  # PyMuPDF

# OCR 懒加载（首次使用时初始化）
_ocr_engine = None

def _get_ocr():
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
    return _ocr_engine


def extract_text_from_pdf(pdf_path: str, progress_cb=None) -> str:
    """提取 PDF 全部文本（自动处理扫描版）
    progress_cb: 可选回调函数 progress_cb(current_page, total_pages)
    """
    doc = fitz.open(pdf_path)
    full_text = []
    has_text = False
    has_images = False

    for page in doc:
        text = page.get_text().strip()
        if text:
            has_text = True
            full_text.append(text)
        if page.get_images():
            has_images = True
    doc.close()

    # 如果提取到正常文字，直接返回
    if has_text and len("\n".join(full_text).replace("·", "").replace(".", "").strip()) > 20:
        return "\n".join(full_text)

    # 扫描版 PDF：用 OCR
    if has_images:
        return _ocr_extract(pdf_path, progress_cb)

    return "\n".join(full_text)


def _ocr_extract(pdf_path: str, progress_cb=None) -> str:
    """对扫描版 PDF 进行 OCR 识别"""
    ocr = _get_ocr()
    doc = fitz.open(pdf_path)
    all_text = []
    total_pages = len(doc)

    # 限制 OCR 页数（避免超时）
    max_pages = min(total_pages, 100)

    for i in range(max_pages):
        page = doc[i]
        pix = page.get_pixmap(dpi=150)  # 150 DPI 够用且快
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            pix.save(tmp.name)
            result, _ = ocr(tmp.name)
            os.unlink(tmp.name)
        if result:
            page_text = "\n".join(line[1] for line in result)
            all_text.append(page_text)
        # 回调进度
        if progress_cb:
            progress_cb(i + 1, total_pages)

    doc.close()
    return "\n".join(all_text)


def split_into_topics(text: str) -> list[dict]:
    """
    将文本按标题/知识点拆分为独立条目
    识别常见分隔模式：
    - 数字编号（1. / 1、/ (1) / 一、）
    - 章节标题（第X章 / 第X节）
    - 连续空行
    """
    lines = text.split("\n")
    topics = []
    current_title = ""
    current_content = []

    # 标题匹配模式
    title_patterns = [
        r"^(第[一二三四五六七八九十百]+[章节篇])",  # 第X章
        r"^(\d+[\.、])",  # 1. / 1、
        r"^(\([一二三四五六七八九十\d]+\))",  # (1) / (一)
        r"^([一二三四五六七八九十]+[、\.])",  # 一、
        r"^(第\d+章)",  # 第1章
        r"^(Chapter\s+\d+)",
        r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s*[:：])",  # English titles
    ]
    title_regex = re.compile("|".join(title_patterns))

    # 知识点行（带关键词标记）
    keyword_line = re.compile(
        r"(?:考点|重点|高频|必背|核心|概念|定义|特点|特征|临床|表现|诊断|治疗|病因|病理|鉴别)"
    )

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current_content:
                current_content.append("")
            continue

        # 判断是否为新标题
        is_title = title_regex.match(stripped)

        # 也检查是否是较短的行（可能是标题）
        if not is_title and len(stripped) < 60 and not stripped.endswith("。") and not stripped.endswith("；"):
            # 可能是标题
            if not any(c in stripped for c in "，、；") and len(stripped) > 2:
                is_title = True

        if is_title:
            # 保存上一个知识点
            if current_content:
                content = "\n".join(current_content).strip()
                if content:
                    topics.append({
                        "title": current_title,
                        "content": content,
                        "keywords": extract_keywords(content),
                    })
            current_title = stripped
            current_content = []
        else:
            current_content.append(stripped)

    # 保存最后一个
    if current_content:
        content = "\n".join(current_content).strip()
        if content:
            topics.append({
                "title": current_title or f"知识点 {len(topics)+1}",
                "content": content,
                "keywords": extract_keywords(content),
            })

    return topics


def extract_keywords(text: str, top_n: int = 8) -> list[str]:
    """
    基于 TF-IDF 思想提取关键词（针对口腔医学优化）
    使用滑动窗口 + 领域词典匹配
    """
    # 口腔医学领域词典（优先匹配）
    domain_dict = [
        # 疾病
        "龋病", "龋齿", "牙髓炎", "根尖周炎", "牙龈炎", "牙周炎",
        "口腔黏膜病", "口腔溃疡", "口腔癌", "涎腺疾病", "颞下颌关节病",
        "牙周脓肿", "牙周-牙髓联合病变", "急性根尖周炎", "慢性根尖周炎",
        "可复性牙髓炎", "不可复性牙髓炎", "化脓性根尖周炎",
        # 解剖
        "釉质", "牙本质", "牙骨质", "牙周膜", "牙槽骨", "牙龈",
        "牙髓", "根管", "根尖孔", "髓室", "根管口",
        "牙冠", "牙根", "牙颈", "牙尖", "窝沟", "点隙",
        # 修复
        "口腔修复", "全瓷冠", "金属冠", "烤瓷冠", "嵌体", "高嵌体",
        "种植", "义齿", "桥体", "基牙", "固位体", "连接体",
        "全口义齿", "可摘局部义齿", "固定义齿", "覆盖义齿",
        "印模", "模型", "颌位关系", "面弓", "哥特式弓",
        # 正畸
        "口腔正畸", "错颌畸形", "安氏分类", "深覆合", "深覆盖",
        "反合", "开合", "锁合", "中线偏斜",
        "矫治器", "托槽", "弓丝", "橡皮圈", "种植钉",
        # 外科
        "口腔颌面外科", "拔牙", "阻生齿", "智齿", "颌骨囊肿",
        "口腔颌面部感染", "口腔颌面部创伤", "颞下颌关节",
        # 材料
        "银汞合金", "复合树脂", "玻璃离子", "磷酸锌", "氧化锌",
        "聚羧酸锌", "丁香油", "氢氧化钙", "MTA", "iRoot",
        "纤维桩", "铸造桩", "氧化锆", "钴铬合金", "纯钛",
        # 牙周
        "菌斑", "牙石", "龈上牙石", "龈下牙石", "牙周探诊",
        "附着丧失", "牙周袋", "骨吸收", "松动度",
        "龈上洁治", "龈下刮治", "根面平整", "牙周手术",
        # 预防
        "窝沟封闭", "氟化物", "再矿化", "脱矿", "氟斑牙",
        "牙外伤", "牙齿折断", "牙齿脱位", "牙再植",
        # 口解
        "乳牙", "恒牙", "切牙", "尖牙", "前磨牙", "磨牙",
        "上颌骨", "下颌骨", "腭骨", "舌骨",
        "咀嚼肌", "翼内肌", "翼外肌", "咬肌", "颞肌",
        # 口组
        "成釉细胞", "牙本质细胞", "成牙骨质细胞", "牙周膜细胞",
        "釉质龋", "牙本质龋", "骨组织", "结缔组织",
        # 其他高频
        "局麻", "利多卡因", "阿替卡因", "布比卡因",
        "X线", "曲面断层", "CBCT", "根尖片", "翼片",
        "刷牙", "Bass刷牙法", "牙线", "牙间隙刷",
        "根管治疗", "干髓术", "塑化治疗", "盖髓术",
        "直接盖髓", "间接盖髓", "活髓切断",
        "牙龈切除术", "翻瓣术", "植骨术", "引导组织再生",
    ]

    # 分词（滑动窗口匹配领域词典）
    found_terms = []
    text_lower = text.lower()

    # 先用领域词典匹配
    for term in domain_dict:
        if term in text_lower:
            found_terms.append(term)

    # 补充分词：按标点/空格切分 + 2-6字滑动窗口
    segments = re.split(r'[\s，。；：、！？（）\(\)\[\]\{\}]', text)
    for seg in segments:
        if 2 <= len(seg) <= 6 and seg not in found_terms:
            found_terms.append(seg)

    # 去重并统计词频
    freq = Counter(found_terms)

    # 停用词
    stop_words = {
        "的", "是", "在", "有", "和", "与", "为", "等", "或", "其",
        "可以", "进行", "通过", "以及", "对于", "由于", "因此",
        "如果", "当", "而", "则", "所", "被", "将", "对", "从",
        "这", "那", "此", "之", "中", "上", "下", "内", "外",
        "以", "到", "使", "导致", "引起", "发生", "形成", "产生",
        "是由于", "主要是", "表现为", "包括", "分为", "属于",
        "临床", "特点", "表现", "诊断", "治疗", "病因", "病理",
    }
    for w in stop_words:
        freq.pop(w, None)

    # 按词频排序取 top_n
    sorted_words = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    result = []
    seen = set()
    for word, _ in sorted_words:
        if word not in seen and len(word) >= 2:
            result.append(word)
            seen.add(word)
        if len(result) >= top_n:
            break

    return result


def parse_pdf_to_topics(pdf_path: str) -> list[dict]:
    """主入口：PDF → 知识点列表"""
    text = extract_text_from_pdf(pdf_path)
    if not text.strip():
        return []

    topics = split_into_topics(text)
    # 为每个知识点添加 ID
    for i, topic in enumerate(topics):
        topic["id"] = i + 1

    return topics


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        topics = parse_pdf_to_topics(sys.argv[1])
        for t in topics:
            print(f"--- {t['title']} ---")
            print(f"关键词: {', '.join(t['keywords'])}")
            print(f"内容: {t['content'][:100]}...")
            print()
