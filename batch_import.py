#!/usr/bin/env python3
"""批量导入：OCR → 正则切题 → 存数据库（不调用本地AI）"""
import re, os, sys, shutil, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pdf_parser import extract_text_from_pdf
from storage import init_db, add_book, add_topics, log_activity

PDF_DIR = os.path.expanduser("~/Desktop/课本")
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
TXT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "recitations")

def parse_topics(raw_text, section_name):
    """正则切题：按编号分割知识点"""
    # 清理OCR噪音
    text = re.sub(r'微信搜索公众号.*?\n', '', raw_text)
    text = re.sub(r'记乎APP.*?\n', '', text)
    text = re.sub(r'\d+\s*/\s*\d+\s*\n', '', text)  # 页码
    text = re.sub(r'银河研旅.*?\n', '', text)
    
    # 按 "数字." 或 "数字、" 分割
    parts = re.split(r'\n\s*(\d{1,3})\s*[.．、]\s*', text)
    # parts: [前言, "1", 内容1, "2", 内容2, ...]
    
    topics = []
    if len(parts) < 3:
        # 没有编号格式，把整段作为一个topic
        if len(text.strip()) > 50:
            topics.append({
                "id": 1, "title": section_name,
                "content": text.strip()[:2000],
                "keywords": [], "section": section_name, "difficulty": 3
            })
        return topics
    
    idx = 1
    for i in range(1, len(parts)-1, 2):
        content = parts[i+1].strip() if i+1 < len(parts) else ""
        if len(content) < 10:
            continue
        
        # 提取标题：第一行
        first_line = content.split('\n')[0].strip()
        title = re.sub(r'[★sS]{1,5}', '', first_line).strip()
        title = re.sub(r'\s+', ' ', title)
        if len(title) > 50:
            title = title[:50] + '...'
        
        # 难度：数★号
        stars = content.count('★')
        difficulty = min(5, max(2, stars + 1))
        
        # 提取关键考点
        key_points = []
        for line in content.split('\n'):
            line = line.strip()
            m = re.match(r'^[（(]\d+[）)]\s*(.*)', line)
            if m and len(m.group(1)) > 5:
                key_points.append(m.group(1)[:80])
        
        topics.append({
            "id": idx, "title": title,
            "content": content,
            "keywords": key_points[:5],
            "section": section_name,
            "difficulty": difficulty,
        })
        idx += 1
    
    return topics

def process_one(fname):
    """处理单个PDF：OCR→切题→存库"""
    src = os.path.join(PDF_DIR, fname)
    size_mb = os.path.getsize(src) / 1024 / 1024
    print(f"\n📖 {fname} ({size_mb:.0f}MB)")
    
    # 复制
    dst = os.path.join(UPLOAD_DIR, f"builtin_{fname}")
    if not os.path.exists(dst):
        shutil.copy2(src, dst)
    
    # 检查是否已有OCR文字
    txt_path = os.path.join(TXT_DIR, f"builtin_{fname}.txt")
    if os.path.exists(txt_path):
        with open(txt_path, 'r', encoding='utf-8') as f:
            raw_text = f.read()
        print(f"  ⏭️  已有OCR文字({len(raw_text)}字)，跳过OCR")
    else:
        print(f"  🔍 OCR中...")
        t0 = time.time()
        def on_progress(cur, total):
            if cur % 5 == 0 or cur == total:
                print(f"    第 {cur}/{total} 页", flush=True)
        raw_text = extract_text_from_pdf(dst, progress_cb=on_progress)
        os.makedirs(TXT_DIR, exist_ok=True)
        with open(txt_path, 'w', encoding='utf-8') as f:
            f.write(raw_text)
        print(f"  ✅ OCR: {len(raw_text)}字, {time.time()-t0:.0f}s")
    
    if not raw_text.strip() or len(raw_text.strip()) < 50:
        print(f"  ❌ 内容太少({len(raw_text.strip())}字)，跳过")
        return False
    
    # 切题
    section_name = fname.replace('.pdf', '').replace('builtin_', '')
    # 去掉前缀数字
    section_name = re.sub(r'^\d+_', '', section_name)
    topics = parse_topics(raw_text, section_name)
    
    if not topics:
        print(f"  ❌ 未切出知识点，跳过")
        return False
    
    # 存库
    book_id = add_book(section_name, dst, fname, raw_text=raw_text)
    add_topics(book_id, topics)
    log_activity("import", {"book_id": book_id, "name": section_name, "topic_count": len(topics)})
    print(f"  💾 book_id={book_id}, {len(topics)}个知识点")
    return True

def main():
    init_db()
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(TXT_DIR, exist_ok=True)
    
    pdfs = sorted([f for f in os.listdir(PDF_DIR) if f.endswith('.pdf')])
    print(f"📚 {len(pdfs)}个PDF待处理\n")
    
    success = 0
    fail = 0
    for idx, fname in enumerate(pdfs, 1):
        print(f"[{idx}/{len(pdfs)}]", end="")
        if process_one(fname):
            success += 1
        else:
            fail += 1
    
    print(f"\n🎉 完成！成功{success}个，失败{fail}个")

if __name__ == "__main__":
    main()
