/* 口腔考研带背 v3 — 前端逻辑（一键到底 + 多维度评分） */

let currentBookId = null;
let studyTopics = [];
let studyIndex = 0;
let currentSessionId = null;
let sessionStats = { correct: 0, wrong: 0, scores: [] };
let calendarYear = new Date().getFullYear();
let calendarMonth = new Date().getMonth() + 1;
// AI 异步评分状态
let aiScoreResults = {};  // topic_id -> {status, score}
let aiPollTimers = {};    // topic_id -> intervalId

// ============================================================
// 初始化
// ============================================================

document.addEventListener('DOMContentLoaded', () => {
    checkOllama();
    loadBooks();
    setupNav();
});

function setupNav() {
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.addEventListener('click', () => switchPage(btn.dataset.page));
    });
}

function switchPage(page) {
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    const target = document.querySelector(`[data-page="${page}"]`);
    if (target) target.classList.add('active');
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.getElementById('page-' + page).classList.add('active');
    if (page === 'library') loadBooks();
    if (page === 'calendar') loadCalendar();
    if (page === 'stats') loadStats();
}

// ============================================================
// Ollama 状态
// ============================================================

async function checkOllama() {
    const badge = document.getElementById('ollama-badge');
    try {
        const r = await fetch('/api/ollama-status');
        const d = await r.json();
        if (d.data.running && d.data.models.length > 0) {
            badge.textContent = '✅ ' + d.data.models[0];
            badge.className = 'badge badge-ok';
        } else if (d.data.running) {
            badge.textContent = '⏳ 模型加载中';
            badge.className = 'badge badge-loading';
        } else {
            badge.textContent = '❌ Ollama 未运行';
            badge.className = 'badge badge-err';
        }
    } catch {
        badge.textContent = '❌ 连接失败';
        badge.className = 'badge badge-err';
    }
}

// ============================================================
// 书库
// ============================================================

async function loadBooks() {
    try {
        const r = await fetch('/api/books');
        const d = await r.json();
        const books = d.data || [];
        renderBooks(books);
        populateBookSelect(books);
    } catch (e) { console.error('loadBooks:', e); }
}

function populateBookSelect(books) {
    const sel = document.getElementById('study-book-select');
    const current = sel.value;
    sel.innerHTML = '<option value="">选择一本书...</option>';
    books.forEach(b => {
        const opt = document.createElement('option');
        opt.value = b.id;
        opt.textContent = b.name;
        sel.appendChild(opt);
    });
    if (current) sel.value = current;
}

function renderBooks(books) {
    const el = document.getElementById('book-list');
    if (!books.length) {
        el.innerHTML = '<div class="empty-state">还没有上传任何书籍<br><br>点击「+ 上传 PDF」开始</div>';
        return;
    }
    el.innerHTML = books.map(b => {
        const p = b.progress || {};
        const pct = p.mastery_rate || 0;
        const plan = b.target_days ? `${b.target_days}天计划` : '未设计划';
        return `
        <div class="book-card" onclick="enterStudy(${b.id})">
            <div class="book-card-header">
                <div class="book-card-title">${esc(b.name)}</div>
                <div class="book-card-actions">
                    <button onclick="event.stopPropagation();showRenameModal(${b.id},'${esc(b.name)}')" title="重命名">✏️</button>
                    <button onclick="event.stopPropagation();promptReplan(${b.id})" title="重新规划">📅</button>
                    <button onclick="event.stopPropagation();deleteBook(${b.id})" title="删除">🗑️</button>
                </div>
            </div>
            <div class="book-card-meta">${b.topic_count || 0} 个知识点 · ${plan}</div>
            <div class="book-progress"><div class="book-progress-fill" style="width:${pct}%"></div></div>
            <div class="book-progress-text">
                <span>掌握 ${pct}%</span>
                <span>${p.mastered || 0}/${p.total || 0}</span>
            </div>
            <div class="book-plan">
                每日约 ${p.topics_per_day || p.daily_avg || 0} 个 · 今日已背 ${p.today_records || 0} 个
            </div>
        </div>`;
    }).join('');
}

function enterStudy(bookId) {
    currentBookId = bookId;
    switchPage('study');
    document.getElementById('study-book-select').value = bookId;
    loadTodayPlan(bookId);
}

// ============================================================
// 上传
// ============================================================

function showUploadModal() {
    document.getElementById('upload-modal').style.display = 'flex';
    document.getElementById('upload-progress').style.display = 'none';
}
function hideUploadModal() { document.getElementById('upload-modal').style.display = 'none'; }

async function doUpload() {
    const name = document.getElementById('upload-name').value || '';
    const file = document.getElementById('upload-file').files[0];
    const days = document.getElementById('upload-days').value || '';
    const examDate = document.getElementById('upload-exam-date').value || '';
    if (!file) { alert('请选择 PDF 文件'); return; }

    const progressDiv = document.getElementById('upload-progress');
    const progressFill = document.getElementById('upload-progress-fill');
    const statusSpan = document.getElementById('upload-status');
    progressDiv.style.display = 'block';
    progressFill.style.width = '0%';
    statusSpan.textContent = '⏳ 正在上传文件...';

    // 禁用上传按钮
    const btn = document.querySelector('#upload-modal .btn-primary');
    btn.disabled = true;
    btn.textContent = '处理中...';

    const fd = new FormData();
    fd.append('pdf', file);
    if (name) fd.append('name', name);
    if (days) fd.append('target_days', days);
    if (examDate) fd.append('exam_date', examDate);

    try {
        const r = await fetch('/api/upload-pdf', { method: 'POST', body: fd });
        const reader = r.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            // 解析 SSE 消息
            const lines = buffer.split('\n');
            buffer = lines.pop(); // 保留不完整的行
            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                try {
                    const d = JSON.parse(line.slice(6));
                    if (d.type === 'progress') {
                        const pct = d.percent || 0;
                        progressFill.style.width = pct + '%';
                        if (d.stage === 'ocr') {
                            statusSpan.textContent = `📖 正在识别第 ${d.current}/${d.total} 页 (${pct}%)`;
                        } else if (d.stage === 'extract') {
                            statusSpan.textContent = '📄 正在提取文字...';
                        } else if (d.stage === 'ai') {
                            statusSpan.textContent = `🧠 AI 正在分析 (${d.current}/${d.total} 段)`;
                        }
                    } else if (d.type === 'done') {
                        progressFill.style.width = '100%';
                        statusSpan.textContent = '✅ ' + d.message;
                        hideUploadModal();
                        loadBooks();
                        alert(d.message);
                    } else if (d.type === 'error') {
                        statusSpan.textContent = '❌ ' + d.error;
                        alert('上传失败: ' + d.error);
                    }
                } catch (e) { /* ignore parse errors */ }
            }
        }
    } catch (e) {
        statusSpan.textContent = '❌ 上传失败';
        alert('上传失败: ' + (e.message || e));
    }
    btn.disabled = false;
    btn.textContent = '上传并解析';
    progressDiv.style.display = 'none';
}

// ============================================================
// 书库操作
// ============================================================

function showRenameModal(bookId, name) {
    document.getElementById('rename-modal').style.display = 'flex';
    document.getElementById('rename-book-id').value = bookId;
    document.getElementById('rename-input').value = name;
}
function hideRenameModal() { document.getElementById('rename-modal').style.display = 'none'; }

async function doRename() {
    const bookId = document.getElementById('rename-book-id').value;
    const name = document.getElementById('rename-input').value;
    if (!name) return;
    await fetch('/api/update-book', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: parseInt(bookId), name })
    });
    hideRenameModal();
    loadBooks();
}

function promptReplan(bookId) {
    const days = prompt('重新设置背诵天数：');
    if (!days || isNaN(days)) return;
    fetch('/api/replan', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ book_id: bookId, target_days: parseInt(days) })
    }).then(r => r.json()).then(d => {
        alert(d.message || '重新规划完成');
        loadBooks();
    });
}

async function deleteBook(bookId) {
    if (!confirm('确定删除这本书及所有学习记录？此操作不可恢复。')) return;
    await fetch('/api/delete-book', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: bookId })
    });
    loadBooks();
}

// ============================================================
// 一键到底学习流程
// ============================================================

function onBookSelect(bookId) {
    if (!bookId) {
        showStudyState('init');
        return;
    }
    currentBookId = parseInt(bookId);
    loadTodayPlan(currentBookId);
}

async function loadTodayPlan(bookId) {
    if (!bookId) return;
    currentBookId = parseInt(bookId);
    try {
        const r = await fetch(`/api/today-plan?book_id=${bookId}`);
        const d = await r.json();
        const plan = d.data || {};
        studyTopics = plan.topics || [];

        if (studyTopics.length === 0) {
            document.getElementById('study-init').textContent = '🎉 今天没有需要背诵的内容！明天再来吧。';
            showStudyState('init');
            return;
        }

        // 显示计划概览
        const summary = document.getElementById('plan-summary');
        summary.innerHTML = `
            <div class="plan-item">
                <span class="plan-count">${plan.total_count || studyTopics.length}</span>
                <span>个知识点等待背诵</span>
            </div>
            <div class="plan-item">📌 新学 ${plan.new_count || 0} 个 · 🔄 复习 ${plan.review_count || 0} 个</div>
        `;
        showStudyState('plan');
    } catch (e) {
        console.error('loadTodayPlan:', e);
        document.getElementById('study-init').textContent = '加载失败，请重试';
        showStudyState('init');
    }
}

function showStudyState(state) {
    document.getElementById('study-init').style.display = state === 'init' ? 'block' : 'none';
    document.getElementById('study-plan-overview').style.display = state === 'plan' ? 'block' : 'none';
    document.getElementById('study-content').style.display = state === 'study' ? 'block' : 'none';
    document.getElementById('study-complete').style.display = state === 'complete' ? 'block' : 'none';
}

function startStudyFlow() {
    studyIndex = 0;
    sessionStats = { correct: 0, wrong: 0, scores: [] };
    clearScoreBar();
    showStudyState('study');
    // 启动会话
    fetch('/api/start-session', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ book_id: currentBookId })
    }).then(r => r.json()).then(d => {
        currentSessionId = d.session_id;
    });
    renderStudyTopic();
}

function renderStudyTopic() {
    if (studyIndex >= studyTopics.length) {
        finishStudySession();
        return;
    }
    const t = studyTopics[studyIndex];
    document.getElementById('study-topic-title').textContent = t.title;
    document.getElementById('study-topic-content').textContent = '';
    document.getElementById('study-topic-content').style.display = 'none';
    const sec = document.getElementById('study-topic-section');
    sec.textContent = t.section || '';
    sec.style.display = t.section ? 'inline' : 'none';
    const diff = document.getElementById('study-topic-difficulty');
    const stars = '★'.repeat(t.difficulty || 1) + '☆'.repeat(5 - (t.difficulty || 1));
    diff.textContent = stars;
    document.getElementById('recite-input').value = '';
    document.getElementById('ai-analysis').style.display = 'none';
    document.getElementById('ai-loading').style.display = 'none';
    const pct = Math.round((studyIndex / studyTopics.length) * 100);
    document.getElementById('study-progress-fill').style.width = pct + '%';
    document.getElementById('study-progress-text').textContent = `${studyIndex + 1} / ${studyTopics.length}`;
}

async function submitRecite() {
    const t = studyTopics[studyIndex];
    const text = document.getElementById('recite-input').value.trim();
    if (!text) { alert('请输入背诵内容'); return; }

    const btns = document.querySelectorAll('#study-content .btn-primary');
    btns.forEach(b => b.disabled = true);

    // 立即显示标准答案
    const contentEl = document.getElementById('study-topic-content');
    if (t.content) {
        contentEl.innerHTML = '<div style="color:#888;font-size:12px;margin-bottom:6px">📖 标准答案：</div>' + esc(t.content);
        contentEl.style.display = 'block';
    }

    // 显示 AI 评分中状态
    const analysisEl = document.getElementById('ai-analysis');
    analysisEl.style.display = 'block';
    document.getElementById('analysis-score').innerHTML = '<div class="spinner" style="display:inline-block;vertical-align:middle;margin-right:8px"></div> <span style="color:#888">AI 正在评分...</span>';
    document.getElementById('analysis-dims').style.display = 'none';
    document.getElementById('analysis-matched').innerHTML = '';
    document.getElementById('analysis-missing').innerHTML = '';
    document.getElementById('analysis-comment').innerHTML = '';

    try {
        const r = await fetch('/api/study', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ topic_id: t.id, book_id: currentBookId, recited_text: text })
        });
        const d = await r.json();

        if (d.success) {
            // 秒出：显示同义表达评分
            if (d.comparison && Object.keys(d.comparison).length > 0) {
                showAnalysis(d.comparison, t.content);
            }
            // 后台 AI 深度评分（状态栏轮询）
            startAiPolling(t.id, t.title);
        } else {
            alert('提交失败: ' + (d.error || '未知错误'));
        }
    } catch (e) {
        console.error('submitRecite:', e);
        alert('提交失败: ' + e.message);
    }
    btns.forEach(b => b.disabled = false);
}

// ============================================================
// 底部评分状态栏 + AI 异步轮询
// ============================================================

function updateScoreBar(topicId, title, score, source) {
    aiScoreResults[topicId] = { status: source === 'ai' ? 'done' : 'regex', score: score, title: title };
    renderScoreBar();
}

function startAiPolling(topicId, title) {
    aiScoreResults[topicId] = { status: 'pending', score: null, title: title };
    renderScoreBar();
    let elapsed = 0;
    const timer = setInterval(async () => {
        elapsed += 2;
        if (elapsed > 120) {
            clearInterval(timer);
            aiScoreResults[topicId].status = 'timeout';
            renderScoreBar();
            return;
        }
        try {
            const r = await fetch('/api/ai-score?topic_id=' + topicId);
            const d = await r.json();
            if (d.status === 'done' && d.score) {
                clearInterval(timer);
                var aiTotal = d.score.total || d.score.score || 0;
                aiScoreResults[topicId] = { status: 'done', score: aiTotal, title: title, full: d.score };
                renderScoreBar();
                updateSessionScore(topicId, aiTotal);
                // 更新分析卡片（显示 AI 评分详情）
                showAnalysis(d.score, null);
            } else if (d.status === 'error') {
                clearInterval(timer);
                aiScoreResults[topicId].status = 'error';
                renderScoreBar();
                document.getElementById('analysis-score').innerHTML = '<span style="color:#f44">AI 评分失败，请重试</span>';
            }
        } catch (e) { /* ignore network errors */ }
    }, 2000);
    aiPollTimers[topicId] = timer;
}

function updateSessionScore(topicId, newScore) {
    var idx = studyTopics.findIndex(function(t) { return t.id === topicId; });
    if (idx >= 0 && idx < sessionStats.scores.length) {
        var oldScore = sessionStats.scores[idx];
        sessionStats.scores[idx] = newScore;
        if (oldScore < 55 && newScore >= 55) { sessionStats.correct++; sessionStats.wrong--; }
        else if (oldScore >= 55 && newScore < 55) { sessionStats.correct--; sessionStats.wrong++; }
    }
}

function renderScoreBar() {
    var bar = document.getElementById('score-status-bar');
    if (!bar) return;
    var entries = Object.entries(aiScoreResults);
    if (entries.length === 0) { bar.style.display = 'none'; return; }
    bar.style.display = 'flex';
    var html = '';
    for (var i = 0; i < entries.length; i++) {
        var tid = entries[i][0];
        var e = entries[i][1];
        var shortTitle = (e.title || '').substring(0, 8);
        if (e.status === 'done') {
            var cls = e.score >= 85 ? 'score-excellent' : e.score >= 70 ? 'score-good' : e.score >= 55 ? 'score-ok' : 'score-bad';
            html += '<div class="sb-item sb-done" title="' + esc(e.title) + ': ' + e.score + '分">' +
                '<span class="sb-title">' + esc(shortTitle) + '</span>' +
                '<span class="sb-score ' + cls + '">' + e.score + '</span></div>';
        } else if (e.status === 'pending') {
            html += '<div class="sb-item sb-pending" title="' + esc(e.title) + ': AI评分中...">' +
                '<span class="sb-title">' + esc(shortTitle) + '</span>' +
                '<span class="sb-wait">⏳</span></div>';
        } else {
            html += '<div class="sb-item sb-error" title="' + esc(e.title) + ': 失败">' +
                '<span class="sb-title">' + esc(shortTitle) + '</span>' +
                '<span class="sb-score">-</span></div>';
        }
    }
    bar.innerHTML = html;
}

function clearScoreBar() {
    aiScoreResults = {};
    for (var k in aiPollTimers) clearInterval(aiPollTimers[k]);
    aiPollTimers = {};
    renderScoreBar();
}

function skipTopic() {
    sessionStats.scores.push(0);
    sessionStats.wrong++;
    nextStudyTopic();
}

function showAnalysis(c, originalContent) {
    const el = document.getElementById('ai-analysis');
    el.style.display = 'block';

    // 显示标准答案
    const contentEl = document.getElementById('study-topic-content');
    if (originalContent) {
        contentEl.innerHTML = '<div style="color:#888;font-size:12px;margin-bottom:6px">📖 标准答案：</div>' + esc(originalContent);
        contentEl.style.display = 'block';
    }

    const total = c.total || c.score || 0;
    const cls = total >= 85 ? 'score-excellent' : total >= 70 ? 'score-good' : total >= 55 ? 'score-ok' : 'score-bad';
    document.getElementById('analysis-score').innerHTML = `<span class="score-total ${cls}">${total}分</span> <span style="font-size:14px;color:#888">${c.coverage || ''}</span>`;

    // 多维度
    const dims = document.getElementById('analysis-dims');
    if (c.completeness !== undefined) {
        dims.innerHTML = renderDimBars(c);
    }

    // 已覆盖
    const matched = c.matched_points || [];
    document.getElementById('analysis-matched').innerHTML = matched.length ?
        '<div class="analysis-section"><h5>✅ 已覆盖</h5><ul>' +
        matched.map(p => `<li>${esc(typeof p === 'string' ? p : p.point || p)}</li>`).join('') + '</ul></div>' : '';

    // 遗漏
    const missing = c.missing_points || [];
    document.getElementById('analysis-missing').innerHTML = missing.length ?
        '<div class="analysis-section"><h5>❌ 遗漏</h5><ul>' +
        missing.map(p => {
            const text = typeof p === 'string' ? p : (p.point || '');
            const imp = typeof p === 'object' && p.importance ? ` [${p.importance}]` : '';
            const sug = typeof p === 'object' && p.suggestion ? ` → ${p.suggestion}` : '';
            return `<li>${esc(text)}${imp}${sug ? '<br><span style="color:#666;font-size:12px">' + esc(sug) + '</span>' : ''}</li>`;
        }).join('') + '</ul></div>' : '';

    document.getElementById('analysis-comment').innerHTML = c.comment ?
        `<p style="color:#666;margin-top:10px;font-size:13px">${esc(c.comment)}</p>` : '';

    // 滚动到分析区域
    el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderDimBars(c) {
    const dims = [
        { label: '知识完整性', key: 'completeness', weight: '20%' },
        { label: '关键考点', key: 'keypoints', weight: '30%' },
        { label: '表述准确性', key: 'accuracy', weight: '20%' },
        { label: '逻辑条理', key: 'logic', weight: '15%' },
        { label: '理解深度', key: 'depth', weight: '15%' },
    ];
    return dims.map(d => {
        const val = c[d.key] || 0;
        const cls = val >= 80 ? '#4ade80' : val >= 60 ? '#fbbf24' : '#f87171';
        return `<div class="dim-row">
            <span class="dim-label">${d.label} (${d.weight})</span>
            <div class="dim-bar"><div class="dim-bar-fill" style="width:${val}%;background:${cls}"></div></div>
            <span class="dim-value" style="color:${cls}">${val}</span>
        </div>`;
    }).join('');
}

function toggleDims() {
    const dims = document.getElementById('analysis-dims');
    dims.style.display = dims.style.display === 'none' ? 'block' : 'none';
}

function nextStudyTopic() {
    studyIndex++;
    renderStudyTopic();
}

// ============================================================
// 学习完成 → AI 汇总报告
// ============================================================

async function finishStudySession() {
    showStudyState('complete');
    document.getElementById('complete-loading').style.display = 'block';
    document.getElementById('complete-report').style.display = 'none';

    // 结束会话
    if (currentSessionId) {
        const avgScore = sessionStats.scores.length ?
            sessionStats.scores.reduce((a, b) => a + b, 0) / sessionStats.scores.length : 0;
        fetch('/api/end-session', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: currentSessionId,
                topics_studied: studyIndex,
                correct: sessionStats.correct,
                wrong: sessionStats.wrong,
                avg_score: avgScore
            })
        }).catch(() => {});
        currentSessionId = null;
    }

    // 请求 AI 生成每日总结
    try {
        const r = await fetch('/api/day-summary', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ book_id: currentBookId })
        });
        const d = await r.json();
        const report = d.data || {};
        renderCompleteReport(report);
    } catch (e) {
        console.error('finishStudySession:', e);
        renderCompleteReport({
            summary: '总结生成失败',
            topics_studied: studyIndex,
            avg_score: sessionStats.scores.length ?
                Math.round(sessionStats.scores.reduce((a, b) => a + b, 0) / sessionStats.scores.length) : 0,
        });
    }

    document.getElementById('complete-loading').style.display = 'none';
    document.getElementById('complete-report').style.display = 'block';
}

function renderCompleteReport(report) {
    const avg = report.avg_score || 0;
    const cls = avg >= 85 ? 'score-excellent' : avg >= 70 ? 'score-good' : avg >= 55 ? 'score-ok' : 'score-bad';

    document.getElementById('report-scores').innerHTML = `
        <div class="report-score-item">
            <div class="score-val ${cls}">${avg}</div>
            <div class="score-label">平均分</div>
        </div>
        <div class="report-score-item">
            <div class="score-val">${report.topics_studied || studyIndex}</div>
            <div class="score-label">背诵数</div>
        </div>
        <div class="report-score-item">
            <div class="score-val">${sessionStats.correct}</div>
            <div class="score-label">通过</div>
        </div>
        <div class="report-score-item">
            <div class="score-val">${report.correct_rate || Math.round(sessionStats.correct / Math.max(studyIndex, 1) * 100)}%</div>
            <div class="score-label">正确率</div>
        </div>
    `;

    let summaryHTML = '';
    if (report.summary) summaryHTML += `<p>${esc(report.summary)}</p>`;
    if (report.encouragement) summaryHTML += `<p style="color:#4ade80;margin-top:8px">${esc(report.encouragement)}</p>`;
    document.getElementById('report-summary').innerHTML = summaryHTML || '<p>今日背诵完成！</p>';

    // 优势/薄弱
    let dimsHTML = '';
    if (report.strengths && report.strengths.length) {
        dimsHTML += '<div style="margin-bottom:10px"><strong style="color:#4ade80">💪 做得好</strong><ul style="padding-left:16px;margin-top:4px">';
        report.strengths.forEach(s => { dimsHTML += `<li style="font-size:13px;color:#999;margin:2px 0">${esc(s)}</li>`; });
        dimsHTML += '</ul></div>';
    }
    if (report.weaknesses && report.weaknesses.length) {
        dimsHTML += '<div><strong style="color:#fbbf24">⚠️ 需要加强</strong><ul style="padding-left:16px;margin-top:4px">';
        report.weaknesses.forEach(w => { dimsHTML += `<li style="font-size:13px;color:#999;margin:2px 0">${esc(w)}</li>`; });
        dimsHTML += '</ul></div>';
    }
    document.getElementById('report-dims').innerHTML = dimsHTML;

    // 明日预览
    loadTomorrowPreview();
}

async function loadTomorrowPreview() {
    try {
        const r = await fetch(`/api/tomorrow-preview?book_id=${currentBookId}`);
        const d = await r.json();
        const tmr = d.data || {};
        const el = document.getElementById('report-tomorrow');
        el.innerHTML = `
            <h4>📅 明日计划 (${tmr.date || ''})</h4>
            <p>新学 ${tmr.new_count || 0} 个 · 复习 ${tmr.review_count || 0} 个</p>
            ${(tmr.new_topics || []).slice(0, 5).map(t => `<p style="font-size:12px;color:#888;margin-top:4px">• ${esc(t.title)}</p>`).join('')}
            ${tmr.new_count > 5 ? `<p style="font-size:12px;color:#555">...还有 ${tmr.new_count - 5} 个</p>` : ''}
        `;
    } catch (e) {
        document.getElementById('report-tomorrow').innerHTML = '<h4>📅 明日计划</h4><p style="color:#666">加载中...</p>';
    }
}

// ============================================================
// 日历
// ============================================================

async function loadCalendar() {
    document.getElementById('calendar-month-label').textContent = `${calendarYear}年${calendarMonth}月`;
    try {
        const r = await fetch(`/api/calendar?year=${calendarYear}&month=${calendarMonth}`);
        const d = await r.json();
        window._calData = d.data || [];
        renderCalendar(window._calData);
    } catch (e) { console.error(e); }
}

function changeMonth(delta) {
    calendarMonth += delta;
    if (calendarMonth > 12) { calendarMonth = 1; calendarYear++; }
    if (calendarMonth < 1) { calendarMonth = 12; calendarYear--; }
    loadCalendar();
}

function renderCalendar(data) {
    const grid = document.getElementById('calendar-grid');
    const firstDay = new Date(calendarYear, calendarMonth - 1, 1).getDay();
    const daysInMonth = new Date(calendarYear, calendarMonth, 0).getDate();
    const today = new Date();
    const dataMap = {};
    data.forEach(d => { dataMap[d.date] = d; });
    let html = ['日','一','二','三','四','五','六'].map(d => `<div class="calendar-header">${d}</div>`).join('');
    for (let i = 0; i < firstDay; i++) html += '<div class="calendar-day empty"></div>';
    for (let d = 1; d <= daysInMonth; d++) {
        const dateStr = `${calendarYear}-${String(calendarMonth).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
        const isToday = today.getFullYear() === calendarYear && today.getMonth() + 1 === calendarMonth && today.getDate() === d;
        const info = dataMap[dateStr];
        const classes = ['calendar-day'];
        if (isToday) classes.push('today');
        if (info) classes.push('has-data');
        let dot = '';
        if (info) { const s = info.avg_score || 0; dot = `<div class="day-dot ${s >= 80 ? 'dot-green' : s >= 60 ? 'dot-yellow' : 'dot-red'}"></div>`; }
        html += `<div class="${classes.join(' ')}" onclick="showDayDetail('${dateStr}')">
            <div class="day-num">${d}</div>${info ? `<div class="day-score">${Math.round(info.avg_score)}分</div>` : ''}${dot}</div>`;
    }
    grid.innerHTML = html;
}

function showDayDetail(dateStr) {
    const info = (window._calData || []).find(d => d.date === dateStr);
    const el = document.getElementById('calendar-day-detail');
    if (!info) { el.style.display = 'none'; return; }
    el.style.display = 'block';
    document.getElementById('calendar-detail-date').textContent = dateStr;
    let html = `<p>背诵 ${info.topics_studied} 个 · 平均 ${Math.round(info.avg_score)} 分 · 正确率 ${info.correct_rate}%</p>`;
    if (info.duration_minutes > 0) html += `<p>时长 ${info.duration_minutes} 分钟 · 效率 ${info.efficiency}%</p>`;
    if (info.books && info.books.length) {
        html += '<p style="margin-top:6px">科目: ' + info.books.map(b => `${b.name}(${b.count})`).join('、') + '</p>';
    }
    document.getElementById('calendar-detail-content').innerHTML = html;
}

// ============================================================
// 统计
// ============================================================

async function loadStats() {
    const sel = document.getElementById('study-book-select');
    const bookId = sel ? sel.value : '';
    if (!bookId) {
        document.getElementById('stats-content').innerHTML = '<div class="empty-state">请先选择一本书</div>';
        return;
    }
    try {
        const [statsR, scoreR] = await Promise.all([
            fetch(`/api/stats?book_id=${bookId}`),
            fetch(`/api/estimate-score?book_id=${bookId}`)
        ]);
        const stats = (await statsR.json()).data || {};
        const score = (await scoreR.json()).data || {};
        document.getElementById('stats-content').innerHTML = `
            <div class="stat-card"><div class="stat-value">${stats.total || 0}</div><div class="stat-label">总知识点</div></div>
            <div class="stat-card"><div class="stat-value" style="color:#4ade80">${stats.mastered || 0}</div><div class="stat-label">已掌握</div></div>
            <div class="stat-card"><div class="stat-value" style="color:#fbbf24">${stats.in_progress || 0}</div><div class="stat-label">学习中</div></div>
            <div class="stat-card"><div class="stat-value">${stats.mastery_rate || 0}%</div><div class="stat-label">掌握率</div></div>
            <div class="stat-card"><div class="stat-value" style="color:#60a5fa">${score.estimated_score || 0}</div><div class="stat-label">预估分数</div><div class="stat-sub">置信度 ${score.confidence || 0}%</div></div>
            <div class="stat-card"><div class="stat-value">${stats.today_records || 0}</div><div class="stat-label">今日背诵</div><div class="stat-sub">今日均分 ${stats.today_avg_score || 0}</div></div>
        `;
    } catch (e) { console.error(e); }
}

// ============================================================
// 工具
// ============================================================

async function triggerShutdownSave() {
    try {
        await fetch('/api/shutdown-save', { method: 'POST' });
        alert('✅ 数据已保存');
    } catch { alert('保存失败'); }
}

function esc(s) {
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
}
