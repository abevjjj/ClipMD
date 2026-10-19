#!/usr/bin/env python3
"""网页文章 -> Markdown 剪藏（交互式循环）
运行: python clip.py ，然后不断粘贴网址回车即可；空行 / q 退出。
保存目录固定为 ./Clippings/
依赖: pip install trafilatura requests
"""
import re, sys, json, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
import trafilatura

SAVE_DIR = Path("./Clippings")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BOT_UA = "Twitterbot/1.0"  # x.com 对爬虫 UA 才返回带 og 标签的静态页
X_HOSTS = {"x.com", "twitter.com", "mobile.twitter.com"}

# Windows 保留设备名（不区分大小写，带扩展名也非法）
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}


def yq(s):
    s = (s or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()
    return f'"{s}"'


def safe_name(s, maxlen=80):
    """去除 Windows/macOS/Linux/Obsidian 的非法或保留字符，保证文件名可用"""
    s = s or ""
    s = re.sub(r'[\x00-\x1f\x7f]', " ", s)          # 控制字符(含换行/制表)
    s = re.sub(r'[\\/:*?"<>|]', " ", s)             # Win 非法 + 路径分隔符 (/ 为 Linux/macOS 非法, : 为旧 macOS 非法)
    s = re.sub(r'[#^\[\]]', " ", s)                 # Obsidian 链接保留字符
    s = re.sub(r'[\u200b-\u200f\u202a-\u202e\u2060\ufeff]', "", s)  # 零宽/方向控制字符
    s = re.sub(r"\s+", " ", s).strip()
    s = s[:maxlen].strip()
    s = s.rstrip(" .")                              # Windows 不允许以空格或点结尾
    s = s.lstrip(".")                               # 避免变成隐藏文件
    if s.split(".")[0].upper() in WIN_RESERVED:     # CON / NUL.txt 等
        s = "_" + s
    return s or "untitled"


def get_html(url, ua=UA):
    r = requests.get(url, headers={"User-Agent": ua}, timeout=30)
    r.raise_for_status()
    if r.encoding is None or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r.text


def meta_fallback(html, names):
    for n in names:
        m = re.search(r'<meta[^>]+(?:name|property)=["\']%s["\'][^>]*content=["\']([^"\']*)' % re.escape(n), html, re.I) \
            or re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:name|property)=["\']%s["\']' % re.escape(n), html, re.I)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""


def unescape(s):
    import html as _h
    return _h.unescape(s or "")


def norm_date(v):
    """各种时间表示 -> 本地时区 yyyy-mm-dd；无法识别返回空串"""
    if not v:
        return ""
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            dt = datetime.datetime.fromtimestamp(int(v))
        else:
            v = str(v).strip()
            try:
                dt = parsedate_to_datetime(v)                     # Sat Oct 10 12:00:00 +0000 2026 / RFC 2822
            except Exception:
                try:
                    dt = datetime.datetime.strptime(v, "%a %b %d %H:%M:%S %z %Y")
                except ValueError:
                    dt = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
            if dt.tzinfo:
                dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d")
    except Exception:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(v))
        return m.group(0) if m else ""


def snowflake_date(url):
    """推文 ID 内含时间戳，可离线还原发布日期"""
    m = re.search(r"/status/(\d+)", url)
    if not m:
        return ""
    ms = (int(m.group(1)) >> 22) + 1288834974657
    return norm_date(ms // 1000)


def clean_x_author(s):
    """'艾地声Edysen (@KA594594) on X: "..."' -> '艾地声Edysen (@KA594594)'"""
    return re.split(r"\s+on\s+(?:X|Twitter)\b", s or "", maxsplit=1)[0].strip()


def x_api(url):
    """fxtwitter 公共接口：长推文/X 文章(Article)的完整内容"""
    m = re.search(r"/([^/]+)/status/(\d+)", url)
    if not m:
        return None
    r = requests.get(f"https://api.fxtwitter.com/{m.group(1)}/status/{m.group(2)}",
                     headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.json().get("tweet")


def article_to_md(article):
    """X Article 的 draft.js 块 -> Markdown"""
    blocks = ((article.get("content") or {}).get("blocks")) or []
    out, n = [], 0
    for b in blocks:
        t, txt = b.get("type", ""), (b.get("text") or "").strip()
        n = n + 1 if t == "ordered-list-item" else 0
        if not txt:
            continue
        if t == "header-one":
            out.append(f"# {txt}")
        elif t == "header-two":
            out.append(f"## {txt}")
        elif t == "header-three":
            out.append(f"### {txt}")
        elif t == "unordered-list-item":
            out.append(f"- {txt}")
        elif t == "ordered-list-item":
            out.append(f"{n}. {txt}")
        elif t == "blockquote":
            out.append(f"> {txt}")
        elif t == "code-block":
            out.append(f"```\n{txt}\n```")
        else:
            out.append(txt)
    return "\n\n".join(out)


def media_md(tweet):
    lines = []
    for m in ((tweet.get("media") or {}).get("all")) or []:
        u = m.get("url")
        if not u:
            continue
        lines.append(f"![]({u})" if m.get("type") == "photo" else f"[{m.get('type', 'media')}]({u})")
    return "\n\n".join(lines)


def clip_x(url):
    tweet, desc, author, published, body = None, "", "", "", ""
    try:
        tweet = x_api(url)
    except Exception as e:
        print("  (fxtwitter 接口失败，改用页面元数据)", e)
    if tweet:
        a = tweet.get("author") or {}
        author = f"{a.get('name', '')} (@{a.get('screen_name', '')})".strip()
        published = norm_date(tweet.get("created_timestamp") or tweet.get("created_at"))
        article = tweet.get("article")
        if article:
            desc = (article.get("title") or tweet.get("text") or "").strip()
            body = article_to_md(article)
            if not body:   # 结构不认识：存下原始 JSON 便于排查
                SAVE_DIR.mkdir(parents=True, exist_ok=True)
                dbg = SAVE_DIR / "_debug_x.json"
                dbg.write_text(json.dumps(tweet, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"  ! 文章正文为空，原始数据已存到 {dbg}")
            cover = (((article.get("cover_media") or {}).get("media_info")) or {}).get("original_img_url")
            if cover:
                body = f"![]({cover})\n\n" + body
        else:
            desc = (tweet.get("text") or "").strip()
            body = desc
        extra = media_md(tweet)
        if extra:
            body += "\n\n" + extra
        q = tweet.get("quote")
        if q:
            body += "\n\n> 引用 " + (q.get("url") or "") + "\n> " + (q.get("text") or "").replace("\n", "\n> ")
    if not desc:  # 接口失败：退回 og 标签（内容可能被截断）
        html = get_html(url, BOT_UA)
        desc = unescape(meta_fallback(html, ["og:description", "twitter:description", "description"]))
        author = clean_x_author(unescape(meta_fallback(html, ["og:title", "twitter:title"])))
        body = desc
    if not desc:
        raise RuntimeError("未取到推文内容（可能需要登录或被限制）")
    desc = desc.strip().strip('“”"')
    author = clean_x_author(author)
    published = published or snowflake_date(url)
    fields = [("title", desc[:100]), ("source", url), ("author", author),
              ("site", "X"), ("published", published), ("description", desc)]
    return write_md(desc, fields, body)       # 文件名 = description


def write_md(title_for_name, fm_fields, body):
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.date.today().strftime("%Y-%m-%d")
    fm = ["---"]
    for k, v in fm_fields:
        fm.append(f"{k}: {v}" if k == "published" else f"{k}: {yq(v)}")
    fm += [f"created: {now}", "tags:", "  - clippings", "---", ""]
    base = safe_name(title_for_name)
    path = SAVE_DIR / f"{base}.md"
    n = 2
    while path.exists():
        path = SAVE_DIR / f"{base} ({n}).md"
        n += 1
    path.write_text("\n".join(fm) + body.strip() + "\n", encoding="utf-8")
    return path


def clip_generic(url):
    html = get_html(url)
    meta = trafilatura.extract_metadata(html, default_url=url)
    body = trafilatura.extract(html, url=url, output_format="markdown",
                               include_links=True, include_images=True,
                               include_tables=True, include_formatting=True,
                               favor_recall=True)
    if not body:
        raise RuntimeError("未能提取正文")
    title = (meta.title if meta else None) or "untitled"
    author = (meta.author if meta else None) or meta_fallback(html, ["author", "article:author", "twitter:creator"])
    published = norm_date((meta.date if meta else None) or meta_fallback(html, ["article:published_time", "datePublished", "pubdate"]))
    desc = (meta.description if meta else None) or ""
    site = (meta.sitename if meta else None) or urlparse(url).netloc
    fields = [("title", title), ("source", url), ("author", author), ("site", site),
              ("published", published), ("description", desc)]
    return write_md(title, fields, body)


def clip(url):
    host = (urlparse(url).netloc or "").lower().removeprefix("www.")
    return clip_x(url) if host in X_HOSTS else clip_generic(url)


def main():
    print(f"网页剪藏 → {SAVE_DIR.resolve()}\n粘贴网址回车；空行或 q 退出。")
    while True:
        try:
            line = input("URL> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line or line.lower() in {"q", "quit", "exit"}:
            break
        for url in line.split():                      # 一行可粘贴多个网址
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            try:
                print("  ✓", clip(url))
            except Exception as e:
                print("  ✗", url, "->", e)


if __name__ == "__main__":
    main()
