#!/usr/bin/env python3
"""仓库布局检查：文档链接、gitlink、忽略规则、行尾约定。

用法（仓库任意位置）：

```bash
python3 scripts/check_repo_layout.py
```

这个仓库真实发生过三类问题，本脚本把它们变成机械检查：

1. **文档搬了、引用没跟着改**：Phase 1 把根目录六份文档搬进 `docs/`，其中一处链接
   还写在 `backend/tools/report_hybrid_validation.py` 的字符串模板里。
2. **工作区里多了一份没登记的嵌套 Git 仓库**：`intro-page/` 下是两个独立站点，
   提交进去就是内容缺失的 gitlink —— 克隆的人拿不到内容，也不会得到提示。
3. **行尾被工具改掉**：`*.bat` 必须 CRLF、`*.command`/`*.sh` 必须 LF（见
   `.gitattributes` 与提交 `7cc2265`）。批量替换文档时最容易顺手把整份文件的
   换行改掉，把一行修改放大成上千行差异。

文档链接怎么算通过
------------------

* 只检查**相对链接**（含图片与引用式链接）：`http(s)://`、`mailto:`、纯锚点和
  绝对路径（`/…`，本项目里是历史 Windows 产物路径）都不在范围内。
* 目标必须在 **Git 跟踪树**里解析得到：只在本机存在的未跟踪文件不算通过 —— 克隆
  仓库的人看不到它。指向目录时，该目录下要有跟踪文件。
* 代码块与行内代码里的链接不算引用。
* 已知的历史断链记在 `scripts/doc-link-baseline.json`（按"来源文件＋目标"精确
  匹配，并写明原因）。**新增断链一律失败**；给本轮改动加例外不算修好。

退出码：0 表示没有问题；1 表示有新增断链或其它布局问题。
"""
from __future__ import annotations

import json
import posixpath
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts" / "doc-link-baseline.json"

#: Phase 1 搬走的文档：这些文件名不该再以"根目录相对"的形式被引用。
MOVED_DOCS = (
    "HYBRID_ISOCHRONE_DESIGN.md",
    "15分钟生活圈_需求分析.md",
    "15分钟生活圈_技术实现方案.md",
    "15分钟生活圈_自适应网格算法与测试方案.md",
    "ai_15_nIsJ99EN3y.pdf",
)

#: 方案文档按设计会写下这些旧路径 —— 它记录的正是这次搬迁，不能算断链。
LINK_EXCEPTIONS = ("docs/proposals/",)

#: 参与扫描的文件类型：既覆盖 Markdown，也覆盖"链接写在代码里"的那种情况。
SCANNED_SUFFIXES = (".md", ".py", ".ts", ".tsx", ".yml", ".yaml", ".json", ".txt", ".html")

#: `.gitattributes` 的行尾约定；值指**工作区**应有的行尾。
LINE_ENDINGS = {"start.bat": "crlf", "start.command": "lf"}

LINK = re.compile(r'!?\[[^\]\n]*\]\(\s*(<[^>\n]*>|[^\s)]+)(?:\s+"[^"\n]*")?\s*\)')
REFERENCE = re.compile(r"^ {0,3}\[[^\]\n]+\]:\s*(\S+)", re.MULTILINE)
EXTERNAL = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|#|/)")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
INLINE = re.compile(r"`+[^`\n]*`+")


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def tracked_files() -> list[str]:
    # -z：路径按字节原样给出，不走 core.quotepath 的转义 —— 这个仓库里全是中文
    # 文件名，按行读会把它们读成带引号的转义串，然后静默地一个都打不开。
    return [name for name in git("ls-files", "-z").decode("utf-8").split("\0") if name]


def strip_code(text: str) -> str:
    """把代码块与行内代码换成等长空白，保留行号与列位置。"""
    out: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        marker = FENCE.match(line)
        if marker:
            char = marker.group(1)[0]
            fence = None if fence == char else (char if fence is None else fence)
            out.append(" " * len(line))
            continue
        if fence is not None:
            out.append(" " * len(line))
            continue
        out.append(INLINE.sub(lambda match: " " * len(match.group(0)), line))
    return "\n".join(out)


def links(name: str, text: str):
    """产出 (行号, 目标) —— 行内链接与引用式链接都算。"""
    for number, line in enumerate(strip_code(text).splitlines(), 1):
        for match in LINK.finditer(line):
            yield number, match.group(1)
        for match in REFERENCE.finditer(line):
            yield number, match.group(1)


def resolve(source: str, target: str) -> str | None:
    """把相对链接解析成仓库内路径；外部链接与绝对路径返回 None。"""
    target = target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    if not target or EXTERNAL.match(target):
        return None
    path = urllib.parse.unquote(target.split("#", 1)[0].split("?", 1)[0])
    if not path:
        return None
    return posixpath.normpath(posixpath.join(posixpath.dirname(source), path))


def load_baseline() -> dict[tuple[str, str], str]:
    if not BASELINE.exists():
        return {}
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    return {(row["source"], row["target"]): row.get("reason", "") for row in data["known"]}


def main() -> int:
    problems: list[str] = []
    notes: list[str] = []
    tracked = tracked_files()
    tracked_set = set(tracked)

    def is_tracked(path: str) -> bool:
        if path in tracked_set:
            return True
        prefix = path.rstrip("/") + "/"
        return any(name.startswith(prefix) for name in tracked_set)

    baseline = load_baseline()
    seen_baseline: set[tuple[str, str]] = set()
    checked = 0
    for name in tracked:
        if name.startswith(LINK_EXCEPTIONS) or not name.endswith(SCANNED_SUFFIXES):
            continue
        text = (ROOT / name).read_text(encoding="utf-8", errors="replace")
        markdown = name.endswith(".md")
        for number, target in links(name, text):
            path = resolve(name, target)
            if path is None:
                continue
            if not markdown:
                # 代码文件里只跟一件事：它引用的是不是**搬走的文档**。理由是模板里的
                # 链接按生成文档所在的目录写（例如 backend/tools/*.py 生成的报告落在
                # backend/docs/），按脚本自己的目录解析会误报生成图片一类的相对路径；
                # 而这个仓库真的踩过的坑，正是"文档搬了、代码里的链接没跟着改"。
                if posixpath.basename(path) not in MOVED_DOCS or is_tracked(path):
                    continue
                problems.append(f"{name}:{number}: 对已搬走文档的引用解析不到 -> {target}"
                                f"（按本文件所在目录解析为 {path}）")
                continue
            checked += 1
            if is_tracked(path):
                continue
            key = (name, target)
            if key in baseline:
                seen_baseline.add(key)
                continue
            problems.append(f"{name}:{number}: 链接目标不在 Git 跟踪树里 -> {target}（解析为 {path}）")

    for source, target in sorted(set(baseline) - seen_baseline):
        notes.append(f"基线里这条断链已经不存在了，可以从 doc-link-baseline.json 删掉：{source} -> {target}")

    for name in MOVED_DOCS:
        if name in tracked_set:
            problems.append(f"{name}: 又出现在仓库根目录（Phase 1 已把它搬进 docs/）")

    for line in git("ls-files", "-s").decode("utf-8").splitlines():
        if line.startswith("160000"):
            problems.append(f"gitlink: {line.split(chr(9), 1)[-1]} —— 克隆者拿不到它的内容")

    # 用不存在的探针路径检查忽略规则：干净检出里 intro-page/ 并不存在。
    for directory, why in (("intro-page", "两个独立站点"), (".venv", "根目录虚拟环境")):
        probe = f"{directory}/__layout_probe__"
        if subprocess.run(["git", "check-ignore", "--no-index", "-q", probe], cwd=ROOT).returncode != 0:
            problems.append(f"{directory}/ 不再被 .gitignore 忽略：{why}会被提交成 gitlink")

    for name, want in LINE_ENDINGS.items():
        if name not in tracked_set:
            continue
        index_blob = git("show", f":{name}")
        work = (ROOT / name).read_bytes()
        work_crlf = work.count(b"\r\n")
        if want == "crlf" and work_crlf == 0:
            problems.append(f"{name}: 工作区不是 CRLF（.gitattributes 要求 eol=crlf）")
        if want == "lf" and work_crlf:
            problems.append(f"{name}: 工作区出现了 CRLF（.gitattributes 要求 eol=lf）")
        if index_blob.count(b"\r\n"):
            problems.append(f"{name}: 索引里存的是 CRLF；应存 LF，由 eol 属性在检出时转换")

    for note in notes:
        print(f"[提示] {note}")
    for problem in problems:
        print(f"[问题] {problem}")
    print(f"检查了 {checked} 条 Markdown 相对链接、{len(tracked)} 个跟踪文件；"
          f"新增问题 {len(problems)} 个，基线内历史断链 {len(seen_baseline)} 条")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
