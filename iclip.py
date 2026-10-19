#!/usr/bin/env python3
"""网页文章 -> Markdown 剪藏（交互式循环）
运行: python clip.py ，然后不断粘贴网址回车即可；空行 / q 退出。
保存目录固定为 ./Clippings/
依赖: 仅 Python 标准库，适合 iSH / Alpine Linux
"""
import re, sys, json, datetime, select, html
from html.parser import HTMLParser
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse, urljoin

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
    """使用标准库请求网页，避免 requests 等第三方依赖。"""
    req = Request(url, headers={
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "close",
    })
    try:
        with urlopen(req, timeout=30) as response:
            data = response.read(5 * 1024 * 1024 + 1)
            if len(data) > 5 * 1024 * 1024:
                raise RuntimeError("网页超过 5 MB，已停止读取")
            charset = response.headers.get_content_charset() or "utf-8"
    except HTTPError as exc:
        raise RuntimeError("HTTP 错误 %s：%s" % (exc.code, exc.reason))
    except URLError as exc:
        raise RuntimeError("网络请求失败：%s" % exc.reason)
    try:
        return data.decode(charset)
    except (LookupError, UnicodeDecodeError):
        for enc in ("utf-8", "gb18030", "big5"):
            try:
                return data.decode(enc)
            except (LookupError, UnicodeDecodeError):
                pass
        return data.decode("utf-8", "replace")


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
    api_url = f"https://api.fxtwitter.com/{m.group(1)}/status/{m.group(2)}"
    raw = get_html(api_url, UA)
    try:
        return json.loads(raw).get("tweet")
    except ValueError:
        raise RuntimeError("fxtwitter 返回的不是有效 JSON")


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


def confirm_duplicate(base):
    """发现同标题剪藏文件时询问；60 秒没有输入就取消。"""
    matches = []
    exact = SAVE_DIR / (base + ".md")
    if exact.exists():
        matches.append(exact)
    # 也识别此前确认重复后产生的“标题 (2).md”等文件
    for candidate in SAVE_DIR.glob(base + " (*).md"):
        if candidate.is_file() and re.match(r"^" + re.escape(base) + r" \(\d+\)\.md$", candidate.name):
            matches.append(candidate)
    if not matches:
        return True

    print("  ! 检测到可能已经剪藏过的文章：")
    for candidate in sorted(set(matches), key=lambda x: x.name)[:5]:
        print("    -", candidate.name)
    if len(matches) > 5:
        print("    ……以及其他 %d 个文件" % (len(matches) - 5))
    print("是否仍要剪藏并另存为新文件？[y/N]（60 秒无回复将取消）: ", end="", flush=True)
    try:
        ready, _, _ = select.select([sys.stdin], [], [], 60)
        if not ready:
            print("\n  已超时，取消保存。")
            return False
        answer = sys.stdin.readline().strip().lower()
    except (OSError, ValueError):
        # 若当前终端不支持 select，宁可取消也不无限等待
        print("\n  当前终端不支持超时输入，取消保存。")
        return False
    if answer in ("y", "yes", "是", "要"):
        return True
    print("  已取消保存。")
    return False


def write_md(title_for_name, fm_fields, body):
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    base = safe_name(title_for_name)
    if not confirm_duplicate(base):
        return None

    now = datetime.date.today().strftime("%Y-%m-%d")
    fm = ["---"]
    for k, v in fm_fields:
        fm.append(f"{k}: {v}" if k == "published" else f"{k}: {yq(v)}")
    fm += [f"created: {now}", "tags:", "  - clippings", "---", ""]
    path = SAVE_DIR / f"{base}.md"
    n = 2
    while path.exists():
        path = SAVE_DIR / f"{base} ({n}).md"
        n += 1
    path.write_text("\n".join(fm) + (body or "").strip() + "\n", encoding="utf-8")
    return path


class SimpleHTMLExtractor(HTMLParser):
    """轻量网页提取器；适用于 iSH，不需要 lxml / trafilatura。"""
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form", "button", "iframe"}

    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.meta = {}
        self.title_parts = []
        self.in_title = False
        self.skip = 0
        self.parts = []
        self.in_article = 0
        self.in_para = False
        self.para = []
        self.in_heading = None
        self.heading = []
        self.in_link = None
        self.link_text = []
        self.in_pre = False
        self.pre = []
        self.in_li = False
        self.li = []
        self.in_list = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"].strip()
        if tag == "title":
            self.in_title = True
        if tag in self.SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in ("article", "main"):
            self.in_article += 1
        if tag in ("h1", "h2", "h3", "h4"):
            self.in_heading, self.heading = tag, []
        elif tag == "p":
            self.in_para, self.para = True, []
        elif tag == "pre":
            self.in_pre, self.pre = True, []
        elif tag in ("ul", "ol"):
            self.in_list.append([tag, 0])
        elif tag == "li":
            self.in_li, self.li = True, []
        elif tag == "a" and a.get("href"):
            self.in_link, self.link_text = urljoin(self.url, a["href"]), []
        elif tag in ("div", "section", "article", "main", "br"):
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in self.SKIP:
            if self.skip:
                self.skip -= 1
            return
        if self.skip:
            return
        if tag in ("article", "main") and self.in_article:
            self.in_article -= 1
        if tag in ("h1", "h2", "h3", "h4") and self.in_heading:
            text = self._join(self.heading)
            if text:
                self.parts.append("\n\n" + "#" * int(self.in_heading[1]) + " " + text + "\n\n")
            self.in_heading = None
        elif tag == "p" and self.in_para:
            text = self._join(self.para)
            if text:
                self.parts.append("\n\n" + text + "\n\n")
            self.in_para = False
        elif tag == "pre" and self.in_pre:
            text = "".join(self.pre).strip()
            if text:
                self.parts.append("\n\n```\n" + text + "\n```\n\n")
            self.in_pre = False
        elif tag == "li" and self.in_li:
            text = self._join(self.li)
            if text:
                marker = "- "
                if self.in_list and self.in_list[-1][0] == "ol":
                    self.in_list[-1][1] += 1
                    marker = str(self.in_list[-1][1]) + ". "
                self.parts.append("\n" + marker + text)
            self.in_li = False
        elif tag in ("ul", "ol") and self.in_list:
            self.in_list.pop()
            self.parts.append("\n\n")
        elif tag == "a" and self.in_link:
            text = self._join(self.link_text)
            if text:
                self.parts.append("[" + text + "](" + self.in_link + ")")
            self.in_link = None
        elif tag in ("div", "section", "article", "main"):
            self.parts.append("\n\n")

    def handle_data(self, data):
        if self.skip or not data:
            return
        data = re.sub(r"\s+", " ", data)
        if self.in_title:
            self.title_parts.append(data)
        if self.in_pre:
            self.pre.append(data)
        elif self.in_heading:
            self.heading.append(data)
        elif self.in_para:
            self.para.append(data)
        elif self.in_li:
            self.li.append(data)
        elif self.in_link:
            self.link_text.append(data)
        else:
            self.parts.append(data)

    @staticmethod
    def _join(parts):
        text = "".join(parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n[ \t]+", "\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def result(self):
        return self._join(self.title_parts), self._join(self.parts), self.meta


def clip_generic(url):
    page = get_html(url)
    parser = SimpleHTMLExtractor(url)
    try:
        parser.feed(page)
        parser.close()
    except Exception:
        pass
    tag_title, body, meta = parser.result()
    title = (meta.get("og:title") or meta.get("twitter:title") or tag_title or "untitled").strip()
    author = meta.get("author") or meta.get("article:author") or meta.get("twitter:creator") or ""
    published = norm_date(meta.get("article:published_time") or meta.get("datepublished") or meta.get("pubdate") or meta.get("date"))
    desc = meta.get("og:description") or meta.get("twitter:description") or meta.get("description") or ""
    site = meta.get("og:site_name") or urlparse(url).netloc
    if not body or len(body.strip()) < 40:
        if desc:
            body = desc
        else:
            raise RuntimeError("未能提取正文（页面可能依赖 JavaScript 动态加载）")
    fields = [("title", title), ("source", url), ("author", author), ("site", site),
              ("published", published), ("description", desc)]
    return write_md(title, fields, body)


def clip(url):
    host = (urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
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
                result = clip(url)
                if result is not None:
                    print("  ✓", result)
            except Exception as e:
                print("  ✗", url, "->", e)


if __name__ == "__main__":
    main()
