# -*- coding: utf-8 -*-
"""
每日资讯聚合模块（从每日资讯聚合 main.py 提取）
抓取四类资讯各 10 条：程序员新闻 / GitHub 热门 / IT 行业动态 / 大模型资讯
build_report() 返回 Markdown 字符串供 GUI 展示，不写磁盘文件。
"""

import requests
import xml.etree.ElementTree as ET
import datetime
import re
import html as html_mod
import time
from concurrent.futures import ThreadPoolExecutor

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

TIMEOUT = 8  # 单源超时秒数，并行模式下降低单源等待

# 翻译接口（双API容灾：有道为主，MyMemory为备）
TRANSLATE_YOUDAO = "https://aidemo.youdao.com/trans"
TRANSLATE_MYMEMORY = "https://api.mymemory.translated.net/get"
_translate_cache = {}
_translate_delay = 0.6  # 动态退避间隔，起始0.6秒


def _call_youdao(text):
    """调用有道翻译，返回译文或None。修复errorCode判断BUG。"""
    try:
        r = requests.post(TRANSLATE_YOUDAO, data={
            "q": text, "from": "en", "to": "zh-CHS",
        }, headers={"Referer": "https://fanyi.youdao.com/"}, timeout=8)
        if r.status_code == 200:
            data = r.json()
            # 有道成功响应也含errorCode字段，值为"0"表示成功，非"0"为限流
            if str(data.get("errorCode", "0")) != "0":
                return None
            ts_list = data.get("translation", [])
            ts = ts_list[0] if ts_list else ""
            if ts and ts.strip() and ts.strip() != text:
                return ts.strip()
        return None
    except Exception:
        return None


def _call_mymemory(text):
    """调用MyMemory翻译作为兜底，返回译文或None。"""
    try:
        r = requests.get(TRANSLATE_MYMEMORY, params={
            "q": text, "langpair": "en|zh-CN",
        }, headers=HEADERS, timeout=8)
        if r.status_code == 200:
            data = r.json()
            ts = data.get("responseData", {}).get("translatedText", "")
            # 过滤异常响应（MYMEMORY WARNING / INVALID TEXT等）
            if ts and ts.strip() and "MYMEMORY" not in ts and "INVALID" not in ts:
                return ts.strip()
        return None
    except Exception:
        return None


def translate_title(title):
    """英文标题翻译为中文，中文标题原样返回。返回 (原文, 译文或None)。
    双API容灾：有道为主（动态退避），MyMemory为备，全部失败则展示英文原文。"""
    global _translate_delay
    if not title:
        return title, None
    if title in _translate_cache:
        return title, _translate_cache[title]
    # 判断是否含中文字符（含中文则不翻译）
    if re.search(r"[\u4e00-\u9fff]", title):
        _translate_cache[title] = None
        return title, None
    # 主翻译：有道，最多重试3次，动态退避
    for attempt in range(3):
        if attempt > 0:
            _translate_delay = min(_translate_delay + 0.5, 3.0)  # 退避+0.5s，上限3s
            time.sleep(_translate_delay)
        ts = _call_youdao(title)
        if ts:
            _translate_delay = max(_translate_delay - 0.2, 0.6)  # 成功恢复，下限0.6s
            _translate_cache[title] = ts
            return title, ts
    # 备用翻译：MyMemory兜底
    ts = _call_mymemory(title)
    if ts:
        _translate_cache[title] = ts
        return title, ts
    # 全部失败：缓存None，展示英文原文
    _translate_cache[title] = None
    return title, None


# 批量翻译每批最多条数（单次API请求换行分隔多条文本）
BATCH_SIZE = 8


def translate_batch(texts):
    """批量翻译：多条英文文本合并为一次有道API请求（换行分隔），逐条拆回结果写入缓存。
    返回成功翻译的条数。"""
    global _translate_delay
    if not texts:
        return 0
    # 过滤已缓存、空、中文的文本，只保留需要翻译的
    pending = []
    for t in texts:
        if not t or t in _translate_cache:
            continue
        if re.search(r"[\u4e00-\u9fff]", t):
            _translate_cache[t] = None
            continue
        pending.append(t)
    if not pending:
        return 0
    success = 0
    # 分批，每批BATCH_SIZE条
    for batch_start in range(0, len(pending), BATCH_SIZE):
        batch = pending[batch_start:batch_start + BATCH_SIZE]
        joined = "\n".join(batch)
        translated = False
        # 有道批量翻译，重试2次
        for attempt in range(2):
            if attempt > 0:
                _translate_delay = min(_translate_delay + 0.5, 3.0)
                time.sleep(_translate_delay)
            try:
                r = requests.post(TRANSLATE_YOUDAO, data={
                    "q": joined, "from": "en", "to": "zh-CHS",
                }, headers={"Referer": "https://fanyi.youdao.com/"}, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    if str(data.get("errorCode", "0")) != "0":
                        continue
                    ts_list = data.get("translation", [])
                    ts_text = ts_list[0] if ts_list else ""
                    if not ts_text:
                        continue
                    # 按换行拆回各条译文
                    parts = ts_text.strip().split("\n")
                    if len(parts) == len(batch):
                        for orig, trans in zip(batch, parts):
                            trans = trans.strip()
                            if trans and trans != orig:
                                _translate_cache[orig] = trans
                                success += 1
                            else:
                                _translate_cache[orig] = None
                        translated = True
                        _translate_delay = max(_translate_delay - 0.2, 0.6)
                        break
                    else:
                        # 拆分数量不匹配（换行被合并），回退逐条翻译
                        continue
            except Exception:
                continue
        # 有道批量失败，逐条兜底（含MyMemory）
        if not translated:
            for t in batch:
                translate_title(t)
                if _translate_cache.get(t):
                    success += 1
    return success


def fmt_title(item):
    """生成标题展示文本：中文 + 英文原题(如翻译过)"""
    title = item.get("title", "")
    trans = _translate_cache.get(title)
    if trans:
        return f"{trans}（{title}）"
    return title


def fmt_desc(item):
    """生成描述展示文本：中文 + 英文原句(如翻译过)"""
    desc = item.get("desc", "")
    if not desc:
        return ""
    trans = _translate_cache.get(desc)
    if trans:
        return f"{trans}（{desc}）"
    return desc


# ---------- 数据源定义 ----------
SOURCE_GITHUB = None  # 动态拼接
SOURCE_HN = "https://hacker-news.firebaseio.com/v0/topstories.json"
SOURCE_HN_ITEM = "https://hacker-news.firebaseio.com/v0/item/{id}.json"

# RSS 源
SOURCE_ITHOME = "https://www.ithome.com/rss/"
SOURCE_INFOQ = "https://www.infoq.cn/feed"
SOURCE_OSCHINA = "https://www.oschina.net/news/rss"
SOURCE_QBITAI = "https://www.qbitai.com/feed"

# 大模型相关关键词
LLM_KEYWORDS = ["大模型", "AI", "人工智能", "GPT", "ChatGPT", "Claude", "Gemini", "LLM", "机器学习",
                "深度学习", "OpenAI", "Anthropic", "多模态", "神经网络", "Stable Diffusion", "Midjourney",
                "智能体", "Agent", "DeepSeek", "Qwen", "通义", "文心", "智谱", "Kimi", "豆包", "Copilot",
                "模型", "具身智能", "MCP", "推理"]

# 程序员/技术相关关键词
TECH_EN_WORDS = ["program", "programming", "code", "coding", "software", "developer", "development",
                 "LLM", "machine learning", "deep learning", "API", "library", "framework",
                 "compiler", "language", "Python", "JavaScript", "Rust", "GoLang", "Java", "C++", "C#", "React",
                 "database", "server", "algorithm", "engineer", "open source",
                 "Linux", "debugging", "Git", "Docker", "Kubernetes", "cloud",
                 "network", "database", "GPU", "CPU", "MCP", "model", "LSP",
                 "AI-native", "security researcher"]
TECH_CN_WORDS = ["分布式", "后端", "前端", "数据库", "算法", "开源", "操作系统",
                 "编程", "程序员", "代码", "开发者", "开发框架", "架构", "软件工程", "大模型", "人工智能"]
TECH_EN_EXACT = ["AI", "GPT", "Claude", "Gemini", "OpenAI", "Agent", "C++", "Rust", "Go", "C"]


def clean_text(s):
    """清理 HTML 实体和标签"""
    if not s:
        return ""
    s = html_mod.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def get_text(url, headers=None, timeout=TIMEOUT, retries=2, retry_delay=1):
    """带SSL重试的GET请求，SSL断连时自动重试，可自定义重试次数和间隔"""
    for attempt in range(retries + 1):
        try:
            return requests.get(url, headers=headers or HEADERS, timeout=timeout)
        except requests.exceptions.SSLError:
            if attempt < retries:
                time.sleep(retry_delay)
                continue
            raise
        except Exception:
            if attempt < retries:
                time.sleep(retry_delay)
                continue
            raise


def fetch_github_top(n=10, days=30):
    """GitHub 热门：最近N天创建，按 star 排序。天数越少门槛越低。"""
    items = []
    try:
        cut = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        # 天数越少，新项目还没攒星，门槛和请求量调整
        min_stars = 100 if days >= 30 else (50 if days >= 7 else 15)
        per_page = 15 if days >= 7 else 30  # 48小时档多请求一些
        url = ("https://api.github.com/search/repositories"
               f"?q=created:>{cut}+stars:>{min_stars}&sort=stars&order=desc&per_page={per_page}")
        r = get_text(url)
        if r.status_code != 200:
            return items, f"HTTP {r.status_code}"
        data = r.json()
        for it in data.get("items"):
            # 过滤恶意/垃圾项目（drainer, cheat, hack, crack, mod-menu等）
            repo_name = (it.get("full_name") or "").lower()
            repo_desc = (it.get("description") or "").lower()
            if any(kw in repo_name or kw in repo_desc for kw in
                   ["drainer", "cheat", "mod-menu", "crack", "hack-tool", "stealer", "phishing"]):
                continue
            items.append({
                "title": it.get("full_name", ""),
                "desc": (it.get("description") or "")[:60],
                "stars": it.get("stargazers_count", 0),
                "link": it.get("html_url", ""),
            })
            if len(items) >= n:
                break
        return items, "OK"
    except Exception as e:
        return items, f"{type(e).__name__}: {e}"


def is_tech(title, desc):
    """判断是否技术相关。英文用单词边界，中文用子串。"""
    text = (title + " " + desc)
    low = text.lower()
    if any(k in text for k in TECH_CN_WORDS):
        return True
    for w in TECH_EN_EXACT:
        if re.search(r"\b" + re.escape(w) + r"\b", text, re.IGNORECASE):
            return True
    for w in TECH_EN_WORDS:
        if re.search(r"\b" + re.escape(w) + r"\b", low):
            return True
    return False


def _fetch_hn_item(tid):
    """并发获取单个HN item详情，返回处理后的dict或None"""
    try:
        ir = get_text(SOURCE_HN_ITEM.format(id=tid), timeout=5, retries=1, retry_delay=0.5)
        if ir.status_code != 200:
            return None
        item = ir.json()
        title = item.get("title", "")
        if not title or not is_tech(title, item.get("text", "")):
            return None
        raw_text = clean_text(item.get("text") or "")
        raw_text = re.sub(r'https?://\S+', '', raw_text).strip()
        return {
            "title": title,
            "desc": raw_text[:150],
            "link": item.get("url") or f"https://news.ycombinator.com/item?id={tid}",
        }
    except Exception:
        return None


def fetch_hn(n=10):
    """Hacker News：Top stories（严格过滤非技术内容）
    并发请求item详情，SSL断连时单个失败不影响整体"""
    items = []
    try:
        r = get_text(SOURCE_HN)
        if r.status_code != 200:
            return items, f"HTTP {r.status_code}"
        top_ids = r.json()[:n * 3]
        # 并发请求所有item详情
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(_fetch_hn_item, top_ids))
        for item in results:
            if item:
                items.append(item)
                if len(items) >= n:
                    break
        return items, "OK"
    except Exception as e:
        return items, f"{type(e).__name__}: {e}"


def full_kw_match(keywords, text):
    low = text.lower()
    return any(k.lower() in low for k in keywords)


def fetch_rss(url, n=10, keyword_filter=None, tech_filter=False):
    """通用 RSS 抓取，可选关键词过滤/技术过滤"""
    items = []
    try:
        r = get_text(url)
        if r.status_code != 200:
            return items, f"HTTP {r.status_code}"
        content_type = r.headers.get("Content-Type", "")
        if "xml" not in content_type and "rss" not in content_type:
            pass
        root = ET.fromstring(r.content)
        ns = ""
        for channel in root.iter():
            tag = channel.tag.lower()
            if tag.endswith("channel"):
                for item in channel.findall("item"):
                    t = item.findtext("title") or ""
                    link = item.findtext("link") or ""
                    desc = item.findtext("description") or ""
                    if keyword_filter and not any(k.lower() in (t + desc).lower() for k in keyword_filter):
                        continue
                    if tech_filter and not is_tech(t, desc):
                        continue
                    desc = clean_text(desc)
                    if desc in ("点击查看原文", "点击查看原文>", "查看原文", "") or desc.startswith("点击查看"):
                        desc = ""
                    items.append({
                        "title": clean_text(t),
                        "desc": desc[:150],
                        "link": clean_text(link),
                    })
                    if len(items) >= n:
                        return items, "OK"
                break
            elif tag.endswith("feed"):  # Atom
                for entry in list(channel):  # 只遍历 feed 直接子节点，避免嵌套误匹配
                    if not entry.tag.lower().endswith("entry"):
                        continue
                    t = entry.findtext("{http://www.w3.org/2005/Atom}title") or ""
                    link_el = entry.find("{http://www.w3.org/2005/Atom}link")
                    link = link_el.get("href") if link_el is not None else ""
                    desc = entry.findtext("{http://www.w3.org/2005/Atom}summary") or ""
                    if keyword_filter and not full_kw_match(keyword_filter, t + desc):
                        continue
                    if tech_filter and not is_tech(t, desc):
                        continue
                    items.append({"title": clean_text(t), "desc": clean_text(desc)[:150], "link": clean_text(link)})
                    if len(items) >= n:
                        return items, "OK"
                break
        return items, "OK"
    except Exception as e:
        return items, f"{type(e).__name__}: {e}"


def _fetch_cat1():
    """程序员新闻：Hacker News + IT之家"""
    hn, st1 = fetch_hn(10)
    it, st2 = fetch_rss(SOURCE_ITHOME, 6, tech_filter=True)
    combined = (hn + it)[:10]
    return combined, st1, st2


def _fetch_cat2():
    """GitHub 热门项目：30天/7天/48小时三档各取前10，并行抓取"""
    with ThreadPoolExecutor(max_workers=3) as pool:
        f30 = pool.submit(fetch_github_top, 10, 30)
        f7  = pool.submit(fetch_github_top, 10, 7)
        f1  = pool.submit(fetch_github_top, 10, 2)
        gh30, st30 = f30.result()
        gh7,  st7  = f7.result()
        gh1,  st1  = f1.result()
    return (gh30, st30), (gh7, st7), (gh1, st1)


def _fetch_cat3():
    """IT 行业动态：InfoQ + 开源中国"""
    infoq, st1 = fetch_rss(SOURCE_INFOQ, 8)
    osc, st2 = fetch_rss(SOURCE_OSCHINA, 8)
    it_news = (infoq + osc)[:10]
    return it_news, st1, st2


def _fetch_cat4():
    """大模型资讯：量子位 + InfoQ AI"""
    qb, st1 = fetch_rss(SOURCE_QBITAI, 15, keyword_filter=LLM_KEYWORDS)
    infoq_llm, st2 = fetch_rss(SOURCE_INFOQ, 12, keyword_filter=LLM_KEYWORDS)
    # 按标题去重（量子位自身可能重复发同一标题不同link）
    seen_titles = set()
    llm = []
    for item in (qb + infoq_llm):
        t = item.get("title", "")
        if t and t not in seen_titles:
            seen_titles.add(t)
            llm.append(item)
        if len(llm) >= 15:  # 多抓一些，跨类别去重后还有余量
            break
    return llm, st1, st2


def build_report():
    """并行抓取四类资讯，返回 Markdown 字符串（不写磁盘）。"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    lines = []
    lines.append(f"# 每日资讯聚合 {today}")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 四类资讯并行抓取
    with ThreadPoolExecutor(max_workers=4) as pool:
        f1 = pool.submit(_fetch_cat1)
        f2 = pool.submit(_fetch_cat2)
        f3 = pool.submit(_fetch_cat3)
        f4 = pool.submit(_fetch_cat4)

        cat1, st1a, st1b = f1.result()
        (gh30, st30), (gh7, st7), (gh1, st1) = f2.result()
        it_news, st3a, st3b = f3.result()
        llm, st4a, st4b = f4.result()

    # 批量翻译：GitHub desc + 新闻标题/描述合并，一次请求8条，大幅减少API调用次数
    # GitHub的title是项目路径(full_name)不需要翻译，只翻译desc
    all_texts = []
    for gh in (gh30, gh7, gh1):
        for item in gh:
            if item.get("desc"):
                all_texts.append(item["desc"])
    all_items = list(cat1) + list(it_news) + list(llm)
    for item in all_items:
        if item.get("title"):
            all_texts.append(item["title"])
        if item.get("desc"):
            all_texts.append(item["desc"])
    translate_batch(all_texts)

    gh_all = gh30 + gh7 + gh1

    # 跨类别去重：大模型资讯中去掉已在IT行业动态出现的link（IT行业动态先展示）
    seen_links = set(it.get("link", "") for it in it_news)
    llm_deduped = [it for it in llm if it.get("link", "") not in seen_links]

    results = {
        "程序员新闻": cat1,
        "GitHub 热门": gh_all,
        "IT 行业动态": it_news,
        "大模型资讯": llm_deduped,
    }

    # 1. 程序员新闻
    lines.append("## 1. 程序员新闻（Top 10）")
    lines.append(f"> 来源：Hacker News({st1a}) + IT之家({st1b})")
    lines.append("")
    for i, it in enumerate(cat1, 1):
        lines.append(f"{i}. **{fmt_title(it)}**")
        if it.get("desc"):
            lines.append(f"   {fmt_desc(it)}")
        lines.append(f"   {it['link']}")
    lines.append("")

    # 2. GitHub 热门（三档）
    lines.append("## 2. GitHub 热门项目（三档合并）")
    lines.append(f"> 来源: GitHub API，按 star 排序 | 30天({st30}) + 7天({st7}) + 48小时({st1})")
    lines.append("")
    lines.append("### 近 30 天（Top 10）")
    lines.append("")
    for i, it in enumerate(gh30, 1):
        lines.append(f"{i}. **{it['title']}** ⭐{it.get('stars', 0)}")
        d = fmt_desc(it)
        if d:
            lines.append(f"   {d}")
        lines.append(f"   {it['link']}")
    lines.append("")
    lines.append("### 近 7 天（Top 10）")
    lines.append("")
    for i, it in enumerate(gh7, 1):
        lines.append(f"{i}. **{it['title']}** ⭐{it.get('stars', 0)}")
        d = fmt_desc(it)
        if d:
            lines.append(f"   {d}")
        lines.append(f"   {it['link']}")
    lines.append("")
    lines.append("### 近 48 小时（Top 10）")
    lines.append("")
    for i, it in enumerate(gh1, 1):
        lines.append(f"{i}. **{it['title']}** ⭐{it.get('stars', 0)}")
        d = fmt_desc(it)
        if d:
            lines.append(f"   {d}")
        lines.append(f"   {it['link']}")
    lines.append("")

    # 3. IT 行业动态
    lines.append("## 3. IT 行业动态（Top 10）")
    lines.append(f"> 来源: InfoQ({st3a}) + 开源中国({st3b})")
    lines.append("")
    for i, it in enumerate(it_news, 1):
        lines.append(f"{i}. **{fmt_title(it)}**")
        if it.get("desc"):
            lines.append(f"   {fmt_desc(it)}")
        lines.append(f"   {it['link']}")
    lines.append("")

    # 4. 大模型资讯
    lines.append("## 4. 大模型资讯（Top 10）")
    lines.append(f"> 来源: 量子位({st4a}) + InfoQ AI({st4b})")
    lines.append("")
    for i, it in enumerate(llm_deduped, 1):
        lines.append(f"{i}. **{fmt_title(it)}**")
        if it.get("desc"):
            lines.append(f"   {fmt_desc(it)}")
        lines.append(f"   {it['link']}")
    lines.append("")

    # 统计
    lines.append("---")
    lines.append("")
    lines.append("**抓取统计**")
    for k, v in results.items():
        lines.append(f"- {k}: {len(v)} 条")

    return "\n".join(lines)
