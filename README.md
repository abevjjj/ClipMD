# 网页剪藏工具
---
iclip.py是面向 iSH / Alpine Linux 的轻依赖版本。  
clip.py是依赖requests / trafilatura / lxml版本。  
2026年10月19日功能更新：如果剪藏的网页是已经剪藏过的，也就是有存在的文件，提示用户是否剪藏。超过 60 秒未收到回复，则取消保存。  

# 特点：
- 支持普通网页和 X/Twitter 链接（X 内容尝试使用 fxtwitter 公共接口）。
- 粘贴一个或多个网址回车；空行、q 或 Ctrl-D 退出。
- Markdown 保存到脚本当前目录下的 Clippings/。
- 兼容较旧 Python：不使用 str.removeprefix 等新语法。
运行：python3 clip.py
