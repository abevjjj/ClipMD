#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网页文章 -> Markdown 剪藏，面向 iSH / Alpine Linux 的轻依赖版本。

特点：
- 只依赖 Python 标准库，无需 pip install requests / trafilatura / lxml。
- 支持普通网页和 X/Twitter 链接（X 内容尝试使用 fxtwitter 公共接口）。
- 粘贴一个或多个网址回车；空行、q 或 Ctrl-D 退出。
- Markdown 保存到脚本当前目录下的 Clippings/。
- 兼容较旧 Python：不使用 str.removeprefix 等新语法。
运行：python3 clip_ish.py
"""

import os
import re
import sys
import json
import html
import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse, urljoin, quote, parse_qsl, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

SAVE_DIR = Path("./Clippings")
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
      "Mobile/15E148 Safari/604.1")
BOT_UA = "Twitterbot/1.0"
X_HOSTS = {"x.com", "twitter.com", "mobile.twitter.com"}
TIMEOUT = 25
MAX_HTML_BYTES = 5 * 1024 * 1024

WIN_RESERVED = set(
    ["CON", "PRN", "AUX", "NUL"] +
    ["COM%d" % i for i in range(1, 10)] +
    ["LPT%d" % i for i in range(1, 10)]
)


def yq(value):
    value = (value or "").replace("\\", "\\\\").replace('"', '\\"')
    value = value.replace("\r", " ").replace("\n", " ").strip()
    return '"' + value + '"'


def safe_name(value, maxlen=100):
    value = value or ""
    value = re.sub(r'[\x00-\x1f\x7f]', " ", value)
    value = re.sub(r'[\\/:*?"<>|]', " ", value)
    value = re.sub(r'[#^\[\]]', " ", value)
    value = re.sub(r'[\u200b-\u200f\u202a-\u202e\u2060\ufeff]', "", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = value[:maxlen].strip().rstrip(" .").lstrip(".")
    if value.split(".")[0].upper() in WIN_RESERVED:
        value = "_" + value
    return value or "untitled"


def fetch_bytes(url, ua=UA):
    req = Request(url, headers={
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "close",
    })
    try:
        with urlopen(req, timeout=TIMEOUT) as response:
            data = response.read(MAX_HTML_BYTES + 1)
            charset = response.headers.get_content_charset()
    except HTTPError as exc:
        raise RuntimeError("HTTP 错误 %s：%s" % (exc.code, exc.reason))
    except URLError as exc:
        raise RuntimeError("网络请求失败：%s" % exc.reason)
    if len(data) > MAX_HTML_BYTES:
        raise RuntimeError("网页超过 5 MB，已停止读取")
    return data, charset


def decode_bytes(data, charset=None):
    candidates = []
    if charset:
        candidates.append(charset)
    # 从 HTML 前部识别 charset
    head = data[:8192].decode("ascii", "ignore")
    m = re.search(r'charset\s*=\s*["\']?\s*([a-zA-Z0-9._-]+)', head, re.I)
    if m:
        candidates.append(m.group(1))
    candidates.extend(["utf-8", "gb18030", "big5"])
    for enc in candidates:
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            pass
    return data.decode("utf-8", "replace")


def get_text(url, ua=UA):
    data, charset = fetch_bytes(url, ua)
    return decode_bytes(data, charset)


def norm_date(value):
    if not value:
        return ""
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            stamp = float(value)
            if stamp > 100000000000:
                stamp /= 1000.0
            dt = datetime.datetime.fromtimestamp(stamp)
        else:
            value = str(value).strip()
            try:
                dt = parsedate_to_datetime(value)
            except Exception:
                try:
                    dt = datetime.datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
                except ValueError:
                    dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo:
                dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d")
    except Exception:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value))
        return m.group(0) if m else ""


def snowflake_date(url):
    match = re.search(r"/status/(\d+)", url)
    if not match:
        return ""
    millis = (int(match.group(1)) >> 22) + 1288834974657
    return norm_date(millis / 1000.0)


def clean_x_author(value):
    return re.split(r"\s+on\s+(?:X|Twitter)\b", value or "", maxsplit=1)[0].strip()


class PageParser(HTMLParser):
    """轻量 HTML 解析器：提取 meta、标题及主要正文，避免 lxml 等编译依赖。"""
    SKIP_TAGS = set(["script", "style", "noscript", "svg", "canvas", "nav",
                     "footer", "header", "aside", "form", "button", "iframe"])

    def __init__(self, base_url):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.base_url = base_url
        self.meta = {}
        self.title_parts = []
        self.in_title = False
        self.skip_depth = 0
        self.body_parts = []
        self.capture_depth = 0
        self.current_link = None
        self.current_link_text = []
        self.list_stack = []
        self.pending_space = False
        self.in_pre = False
        self.pre_parts = []
        self.in_blockquote = False
        self.in_heading = None
        self.heading_parts = []
        self.in_li = False
        self.li_parts = []
        self.in_paragraph = False
        self.p_parts = []
        self.candidates = []
        self.candidate_depth = 0
        self.candidate_parts = []
        self.in_article = False
        self.article_depth = 0
        self.main_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            val = a.get("content", "").strip()
            if key and val and key not in self.meta:
                self.meta[key] = val
        if tag == "title":
            self.in_title = True
        if tag in ("article", "main"):
            self.capture_depth += 1
            self.candidate_depth += 1
            if tag == "article":
                self.in_article = True
            if tag == "main":
                self.main_depth += 1
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in ("h1", "h2", "h3", "h4"):
            self.in_heading = tag
            self.heading_parts = []
        elif tag == "p":
            self.in_paragraph = True
            self.p_parts = []
        elif tag == "blockquote":
            self.in_blockquote = True
        elif tag == "pre":
            self.in_pre = True
            self.pre_parts = []
        elif tag in ("ul", "ol"):
            self.list_stack.append({"tag": tag, "n": 0})
        elif tag == "li":
            self.in_li = True
            self.li_parts = []
        elif tag == "a" and a.get("href"):
            self.current_link = urljoin(self.base_url, a["href"])
            self.current_link_text = []
        elif tag == "br":
            self._append_text("\n")
        elif tag in ("div", "section", "article", "main", "tr"):
            self._append_text("\n\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in self.SKIP_TAGS:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag in ("article", "main"):
            if self.capture_depth:
                self.capture_depth -= 1
            if tag == "article":
                self.in_article = False
            if tag == "main" and self.main_depth:
                self.main_depth -= 1
        if tag in ("h1", "h2", "h3", "h4") and self.in_heading:
            text = self._join(self.heading_parts)
            if text:
                level = int(self.in_heading[1])
                self.body_parts.append("\n\n" + "#" * level + " " + text + "\n\n")
                self.candidate_parts.append("\n\n" + "#" * level + " " + text + "\n\n")
            self.in_heading = None
        elif tag == "p" and self.in_paragraph:
            text = self._join(self.p_parts)
            if text:
                block = "\n\n" + text + "\n\n"
                self.body_parts.append(block)
                self.candidate_parts.append(block)
            self.in_paragraph = False
        elif tag == "blockquote":
            self.in_blockquote = False
        elif tag == "pre" and self.in_pre:
            text = "".join(self.pre_parts).strip()
            if text:
                block = "\n\n```\n" + text + "\n```\n\n"
                self.body_parts.append(block)
                self.candidate_parts.append(block)
            self.in_pre = False
        elif tag == "li" and self.in_li:
            text = self._join(self.li_parts)
            if text:
                marker = "- "
                if self.list_stack and self.list_stack[-1]["tag"] == "ol":
                    self.list_stack[-1]["n"] += 1
                    marker = str(self.list_stack[-1]["n"]) + ". "
                block = "\n" + marker + text
                self.body_parts.append(block)
                self.candidate_parts.append(block)
            self.in_li = False
        elif tag in ("ul", "ol") and self.list_stack:
            self.list_stack.pop()
            self.body_parts.append("\n\n")
            self.candidate_parts.append("\n\n")
        elif tag == "a" and self.current_link:
            text = self._join(self.current_link_text)
            if text:
                rendered = "[" + text + "](" + self.current_link + ")"
                self._append_text(rendered)
            self.current_link = None
            self.current_link_text = []
        elif tag in ("div", "section", "article", "main", "tr"):
            self.body_parts.append("\n\n")
            self.candidate_parts.append("\n\n")

    def _append_text(self, text):
        if self.skip_depth:
            return
        if self.in_title:
            self.title_parts.append(text)
        if self.in_pre:
            self.pre_parts.append(text)
            return
        if self.in_heading:
            self.heading_parts.append(text)
        if self.in_paragraph:
            self.p_parts.append(text)
        if self.in_li:
            self.li_parts.append(text)
        if self.current_link:
            self.current_link_text.append(text)
            return
        if self.in_blockquote:
            text = "> " + text
        self.body_parts.append(text)
        self.candidate_parts.append(text)

    def handle_data(self, data):
        if not data or self.skip_depth:
            return
        data = re.sub(r"\s+", " ", data)
        self._append_text(data)

    @staticmethod
    def _join(parts):
        text = "".join(parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n[ \t]+", "\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def result(self):
        title = self._join(self.title_parts)
        body = self._join(self.body_parts)
        # 若有 article/main 标签，正文候选通常比整页文本更集中；
        # 轻量解析无法像 trafilatura 一样精确识别所有网站的正文。
        candidate = self._join(self.candidate_parts)
        if len(candidate) >= 200 and self.capture_depth == 0:
            body = candidate
        return title, body, self.meta


def parse_page(url, page_html):
    parser = PageParser(url)
    try:
        parser.feed(page_html)
        parser.close()
    except Exception:
        pass
    return parser.result()


def meta_value(meta, *keys):
    for key in keys:
        value = meta.get(key.lower(), "")
        if value:
            return html.unescape(value).strip()
    return ""


def x_api(url):
    match = re.search(r"/([^/?]+)/status/(\d+)", url)
    if not match:
        return None
    api_url = "https://api.fxtwitter.com/%s/status/%s" % (match.group(1), match.group(2))
    raw = get_text(api_url, UA)
    try:
        return json.loads(raw).get("tweet")
    except ValueError:
        raise RuntimeError("fxtwitter 返回的不是有效 JSON")


def _media_url(entity):
    """从 fxtwitter 的 media_entities 条目里取图片/视频地址。"""
    if not isinstance(entity, dict):
        return ""
    info = entity.get("media_info") or {}
    url = info.get("original_img_url") or info.get("url") or entity.get("url")
    if url:
        return url
    best, rate = "", -1
    for var in info.get("variants") or []:
        if not isinstance(var, dict) or not var.get("url"):
            continue
        if "mp4" not in str(var.get("content_type", "mp4")):
            continue
        bitrate = var.get("bit_rate") or var.get("bitrate") or 0
        if bitrate > rate:
            best, rate = var["url"], bitrate
    return best or info.get("preview_image_url") or ""


def _entity_map(content):
    """把 entityMap（列表或字典两种形态）统一成 {key字符串: entity}。"""
    raw = content.get("entityMap") or {}
    result = {}
    if isinstance(raw, dict):
        items = list(raw.items())
    else:
        items = []
        for pos, item in enumerate(raw):
            if isinstance(item, dict) and "key" in item:
                items.append((item.get("key"), item.get("value")))
            else:
                items.append((pos, item))
    for key, value in items:
        if isinstance(value, dict) and "value" in value and "type" not in value:
            value = value["value"]
        result[str(key)] = value if isinstance(value, dict) else {}
    return result


def _atomic_to_md(block, entities, media_urls, used):
    """文章里的“原子块”（图片、代码、分割线等）本身没有文字，内容在 entityMap 里。"""
    out = []
    for rng in block.get("entityRanges") or []:
        ent = entities.get(str(rng.get("key")), {})
        kind = str(ent.get("type", "")).upper()
        data = ent.get("data") or {}
        if kind == "MEDIA":
            caption = (data.get("caption") or "").replace("\n", " ").strip()
            for item in data.get("mediaItems") or []:
                url = ""
                for k in ("mediaId", "media_id", "localMediaId"):
                    if item.get(k) is not None:
                        url = media_urls.get(str(item.get(k)), "")
                        if url:
                            break
                if url:
                    used.add(url)
                    out.append("![%s](%s)" % (caption, url))
        elif kind == "MARKDOWN":
            md = (data.get("markdown") or "").strip()
            if md:
                out.append(md)
        elif kind == "DIVIDER":
            out.append("---")
        elif kind == "TWEET":
            tid = data.get("tweetId") or data.get("id")
            if tid:
                out.append("https://x.com/i/status/%s" % tid)
    return out


def article_to_md(article):
    content = article.get("content") or {}
    blocks = content.get("blocks") or []
    entities = _entity_map(content)
    media_urls = {}
    ordered = []
    for ent in article.get("media_entities") or []:
        if not isinstance(ent, dict):
            continue
        url = _media_url(ent)
        if not url:
            continue
        ordered.append(url)
        for k in ("media_id", "id", "mediaId"):
            if ent.get(k) is not None:
                media_urls[str(ent[k])] = url
    used = set()
    out = []
    number = 0
    for block in blocks:
        kind = block.get("type", "")
        text = (block.get("text") or "").strip()
        if kind == "ordered-list-item":
            number += 1
        else:
            number = 0
        if kind == "atomic":
            out.extend(_atomic_to_md(block, entities, media_urls, used))
            continue
        if not text:
            continue
        if kind == "header-one":
            out.append("# " + text)
        elif kind == "header-two":
            out.append("## " + text)
        elif kind == "header-three":
            out.append("### " + text)
        elif kind == "unordered-list-item":
            out.append("- " + text)
        elif kind == "ordered-list-item":
            out.append(str(number) + ". " + text)
        elif kind == "blockquote":
            out.append("> " + text.replace("\n", "\n> "))
        elif kind == "code-block":
            out.append("```\n" + text + "\n```")
        else:
            out.append(text)
    if not used:
        # 没能从正文位置匹配到图片时，把文章附带的图片放到末尾，避免丢图。
        for url in ordered:
            if url not in used:
                used.add(url)
                out.append("![](%s)" % url)
    return "\n\n".join(out)


def media_md(tweet):
    lines = []
    media = ((tweet.get("media") or {}).get("all")) or []
    for item in media:
        url = item.get("url")
        if not url:
            continue
        if item.get("type") == "photo":
            lines.append("![](%s)" % url)
        else:
            lines.append("[%s](%s)" % (item.get("type", "media"), url))
    return "\n\n".join(lines)


def write_md(title_for_name, fields, body):
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().strftime("%Y-%m-%d")
    frontmatter = ["---"]
    for key, value in fields:
        if key == "published" and value:
            frontmatter.append("%s: %s" % (key, value))
        elif key == "published":
            frontmatter.append('%s: ""' % key)
        else:
            frontmatter.append("%s: %s" % (key, yq(value)))
    frontmatter.extend(["created: " + today, "tags:", "  - clippings", "---", ""])
    base = safe_name(title_for_name)
    path = SAVE_DIR / (base + ".md")
    number = 2
    while path.exists():
        path = SAVE_DIR / ("%s (%d).md" % (base, number))
        number += 1
    content = "\n".join(frontmatter) + (body or "").strip() + "\n"
    path.write_text(content, encoding="utf-8")
    return path


def clip_x(url):
    tweet = None
    try:
        tweet = x_api(url)
    except Exception as exc:
        print("  (fxtwitter 接口失败，改用页面元数据：%s)" % exc)
    desc = author = published = body = ""
    if tweet:
        author_info = tweet.get("author") or {}
        author = "%s (@%s)" % (
            author_info.get("name", ""),
            author_info.get("screen_name", "")
        )
        author = author.strip()
        published = norm_date(tweet.get("created_timestamp") or tweet.get("created_at"))
        article = tweet.get("article")
        if article:
            desc = (article.get("title") or tweet.get("text") or "").strip()
            body = article_to_md(article)
            cover = (((article.get("cover_media") or {}).get("media_info")) or {}).get("original_img_url")
            if cover and cover not in body:
                body = "![](%s)\n\n%s" % (cover, body)
        else:
            desc = (tweet.get("text") or "").strip()
            body = desc
        # 推文自带的媒体：正文里已出现的图片不再重复追加
        extra_lines = []
        for ln in media_md(tweet).split("\n\n"):
            m = re.search(r"\(([^()]+)\)$", ln)
            if ln and not (m and m.group(1) in body):
                extra_lines.append(ln)
        if extra_lines:
            body += "\n\n" + "\n\n".join(extra_lines)
        quoted = tweet.get("quote")
        if quoted:
            body += "\n\n> 引用 " + (quoted.get("url") or "") + "\n> " + (quoted.get("text") or "").replace("\n", "\n> ")
    if not desc:
        page = get_text(url, BOT_UA)
        _, _, meta = parse_page(url, page)
        desc = meta_value(meta, "og:description", "twitter:description", "description")
        author = clean_x_author(meta_value(meta, "og:title", "twitter:title"))
        body = desc
    if not desc:
        raise RuntimeError("未取到推文内容，可能需要登录或被网站限制")
    desc = desc.strip().strip('“”"')
    author = clean_x_author(author)
    published = published or snowflake_date(url)
    fields = [
        ("title", desc[:100]), ("source", url), ("author", author),
        ("site", "X"), ("published", published), ("description", desc)
    ]
    return write_md(desc, fields, body)


def clip_generic(url):
    page = get_text(url)
    title_from_tag, body, meta = parse_page(url, page)
    title = meta_value(meta, "og:title", "twitter:title") or title_from_tag
    author = meta_value(meta, "author", "article:author", "twitter:creator")
    published = norm_date(meta_value(
        meta, "article:published_time", "datepublished", "pubdate",
        "article:modified_time", "date"
    ))
    desc = meta_value(meta, "og:description", "twitter:description", "description")
    site = meta_value(meta, "og:site_name") or urlparse(url).netloc
    if not title:
        title = "untitled"
    if not body or len(body.strip()) < 40:
        # 有些页面正文由 JavaScript 动态生成，标准库无法执行 JS。
        # 至少保留描述，避免生成完全空白的笔记。
        if desc:
            body = desc
        else:
            raise RuntimeError("未能提取到正文（页面可能依赖 JavaScript 动态加载）")
    fields = [
        ("title", title), ("source", url), ("author", author),
        ("site", site), ("published", published), ("description", desc)
    ]
    return write_md(title, fields, body)


# ---------------------------------------------------------------------------
# 去重：用一个隐藏的索引文件记录“已剪藏网址 -> 文件名”。
# 为了在 iSH 里稳定，正常流程只对这一个小文件做普通的读/追加，
# 不遍历目录、不扫描所有 md（这类操作在 iSH 上可能闪退）。
# 旧的剪藏文件可在提示符下输入 rescan 手动导入一次。
# ---------------------------------------------------------------------------
INDEX_NAME = "_iclip_index.tsv"
OLD_INDEX_NAME = ".iclip_index.tsv"

TRACK_PARAMS = set(["fbclid", "gclid", "igshid", "spm", "ref", "ref_src",
                    "mc_cid", "mc_eid", "s", "t"])


def norm_url(url):
    """规范化网址用于比较：忽略 http/https、www、锚点、末尾斜杠和常见追踪参数。"""
    url = (url or "").strip()
    try:
        p = urlparse(url)
        host = (p.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host in X_HOSTS:
            host = "x.com"
            query = ""
        else:
            pairs = []
            for k, v in parse_qsl(p.query, keep_blank_values=True):
                if k.lower().startswith("utm_") or k.lower() in TRACK_PARAMS:
                    continue
                pairs.append((k, v))
            pairs.sort()
            query = urlencode(pairs)
        path = p.path or ""
        while len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        if path == "/":
            path = ""
        return host + path + (("?" + query) if query else "")
    except Exception:
        return url


def index_path():
    return str(SAVE_DIR / INDEX_NAME)


def migrate_index():
    """旧索引文件改名为新名字（只在新文件不存在时进行）。"""
    try:
        old = str(SAVE_DIR / OLD_INDEX_NAME)
        if os.path.exists(old) and not os.path.exists(index_path()):
            os.rename(old, index_path())
    except Exception:
        pass


def load_index():
    """返回 {规范化网址: [文件名, ...]}；任何错误都当作空索引。"""
    index = {}
    try:
        with open(index_path(), "r", encoding="utf-8", errors="replace") as f:
            for line in f.read().splitlines():
                if "\t" not in line:
                    continue
                key, name = line.split("\t", 1)
                if key and name:
                    index.setdefault(key, [])
                    if name not in index[key]:
                        index[key].append(name)
    except Exception:
        pass
    return index


def add_index(url, path):
    """剪藏成功后记录；失败不影响主流程。"""
    try:
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        name = Path(str(path)).name
        with open(index_path(), "a", encoding="utf-8") as f:
            f.write("%s\t%s\n" % (norm_url(url), name))
    except Exception as exc:
        print("  (索引写入失败，不影响剪藏：%s)" % exc)


def find_existing(url):
    """返回该网址已剪藏且文件仍存在的文件名列表。"""
    try:
        names = load_index().get(norm_url(url), [])
        return [n for n in names if os.path.exists(str(SAVE_DIR / n))]
    except Exception:
        return []


def read_source(filepath):
    """读取 md 文件 frontmatter 中的 source 字段；失败返回空串。"""
    try:
        with open(filepath, "rb") as f:
            head = f.read(4096).decode("utf-8", "replace")
    except Exception:
        return ""
    lines = head.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("source:"):
            val = line[len("source:"):].strip()
            if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                val = val[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            return val
    return ""


def rescan():
    """手动命令：扫描 Clippings/ 里已有的 md，把 source 导入索引。"""
    try:
        names = sorted(os.listdir(str(SAVE_DIR)))
    except Exception as exc:
        print("  无法读取目录：%s" % exc)
        return
    index = load_index()
    added = 0
    for name in names:
        if not name.endswith(".md"):
            continue
        src = read_source(os.path.join(str(SAVE_DIR), name))
        if not src:
            continue
        key = norm_url(src)
        index.setdefault(key, [])
        if name not in index[key]:
            index[key].append(name)
            added += 1
    lines = []
    for key in sorted(index):
        for name in index[key]:
            lines.append("%s\t%s" % (key, name))
    try:
        with open(index_path(), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
        print("  索引已更新，新增 %d 条，共 %d 条。" % (added, len(lines)))
    except Exception as exc:
        print("  索引写入失败：%s" % exc)


def confirm(prompt):
    """默认 N；EOF/Ctrl-C 视为否。"""
    try:
        ans = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes", "是", "好")


def clip(url):
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parsed.scheme not in ("http", "https"):
        raise RuntimeError("只支持 http:// 或 https:// 开头的网址")
    if host in X_HOSTS:
        return clip_x(url)
    return clip_generic(url)


def main():
    print("网页剪藏（iSH 轻依赖版） -> %s" % SAVE_DIR.resolve())
    print("粘贴网址回车；一行可放多个网址；空行、q 或 Ctrl-D 退出。")
    migrate_index()
    print("已剪藏过的网址会先询问；输入 rescan 可把旧剪藏导入去重索引。")
    while True:
        try:
            line = input("URL> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line or line.lower() in ("q", "quit", "exit"):
            break
        if line.lower() == "rescan":
            rescan()
            continue
        for url in line.split():
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            existing = find_existing(url)
            if existing:
                print("  ! 已剪藏过：%s" % url)
                for name in existing[:3]:
                    print("    - %s" % name)
                if len(existing) > 3:
                    print("    ...（共 %d 个）" % len(existing))
                if not confirm("  仍要再次剪藏吗？(新文件会加序号) [y/N] "):
                    print("  - 已跳过")
                    continue
            try:
                path = clip(url)
                print("  ✓ %s" % path)
                add_index(url, path)
            except Exception as exc:
                print("  ✗ %s -> %s" % (url, exc))


if __name__ == "__main__":
    main()
