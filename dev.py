#!/usr/bin/env python3
"""One-command dev launcher: prepare the environment, run backend + frontend, open the browser.

    python dev.py                 # full guided flow (this is what start.bat runs)
    python dev.py setup           # environment only, no servers
    python dev.py doctor          # report what is missing, change nothing
    python dev.py backend         # backend only
    python dev.py frontend        # frontend only
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "life-circle-demo"
ALGORITHM = ROOT / "life-circle-algorithm"

DEFAULT_BACKEND_PORT = 8000
DEFAULT_FRONTEND_PORT = 5173
VENV = BACKEND / ".venv"
MIN_PYTHON = (3, 11)
NODE_ENGINE_RANGE = "^20.19.0 || >=22.12.0"

IS_WINDOWS = os.name == "nt"
COLORS = {"dev": "\033[32m", "warn": "\033[33m", "err": "\033[31m", "ok": "\033[32m", "ask": "\033[36m"}
RESET = "\033[0m"
if not sys.stdout.isatty():
    COLORS = {key: "" for key in COLORS}
    RESET = ""


def python_tag() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def parse_node_version(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value.strip())
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def node_version() -> tuple[int, int, int] | None:
    if shutil.which("node") is None:
        return None
    try:
        result = subprocess.run(["node", "-v"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return parse_node_version(result.stdout)


def node_version_supported(version: tuple[int, int, int]) -> bool:
    """Match the Node range declared by the locked Vite 8 toolchain."""
    return (version[0] == 20 and version >= (20, 19, 0)) or version >= (22, 12, 0)


def node_version_text(version: tuple[int, int, int]) -> str:
    return ".".join(map(str, version))


def preflight() -> None:
    """Fail in a second instead of three minutes of downloading into a doomed install."""
    if sys.version_info < MIN_PYTHON:
        raise InstallFailure("python", f"需要 Python 3.11 或更新，当前是 {sys.version.split()[0]}", [
            "到 https://www.python.org/downloads/ 安装，安装时务必勾选 Add python.exe to PATH",
            "装完重新打开终端（让 PATH 生效）再运行本脚本",
        ])
    if sys.version_info >= (3, 15):
        log("warn", f"Python {python_tag()} 较新，锁文件里的依赖可能还没适配；若安装失败可改用 3.12 或 3.13")

    version = node_version()
    if version is None:
        raise InstallFailure("node", "没有找到 node，前端无法启动", [
            f"到 https://nodejs.org/ 安装符合 Vite 8 要求的 Node.js 版本（{NODE_ENGINE_RANGE}）",
            "装完重新打开终端，再运行本脚本",
        ])
    if not node_version_supported(version):
        raise InstallFailure("node", f"Node.js 版本不兼容（当前 {node_version_text(version)}），Vite 8 要求 {NODE_ENGINE_RANGE}", [
            "安装 Node.js 20.19.0 或更新的 20.x，或 22.12.0 及更新版本",
            "装完重新打开终端，再运行本脚本",
        ])
    if shutil.which("npm") is None:
        raise InstallFailure("npm", "有 node 但没有 npm，前端依赖无法安装", [
            "到 https://nodejs.org/ 重新安装 LTS 版本（安装包内同时包含 node 和 npm）",
            "装完重新打开终端，再运行本脚本",
        ])
    log("ok", f"环境预检通过：Python {python_tag()}，Node {node_version_text(version)}")


class InstallFailure(Exception):
    """A dependency install failed; carry advice instead of a traceback."""

    def __init__(self, tool: str, headline: str, hints: list[str]) -> None:
        super().__init__(headline)
        self.tool = tool
        self.headline = headline
        self.hints = hints

    def report(self) -> None:
        print()
        log("err", self.headline)
        for hint in self.hints:
            print(f"    {hint}")
        print()
        log("dev", "上面的步骤解决后再重新运行 python dev.py 即可；已装好的部分不会重来。")


@dataclass(frozen=True)
class Options:
    index_url: str | None = None
    no_fallback_index: bool = False


# Ordered by how reliably they carry wheels for a brand-new Python release.
FALLBACK_INDEXES: tuple[tuple[str, str], ...] = (
    ("清华镜像", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ("阿里云镜像", "https://mirrors.aliyun.com/pypi/simple/"),
    ("官方源", "https://pypi.org/simple"),
)


def log(tag: str, message: str) -> None:
    text = f"{COLORS[tag]}[{tag}]{RESET} {message}"
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        safe = text.encode("ascii", "replace").decode("ascii")
        print(safe, flush=True)


def step(number: int, total: int, title: str) -> None:
    print(f"\n{COLORS['dev']}== {number}/{total} {title} =={RESET}", flush=True)


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


# ---------------------------------------------------------------- environment variables


@dataclass(frozen=True)
class EnvVar:
    path: Path
    key: str
    label: str
    why: str
    where: str
    secret: bool


ENV_VARS = (
    EnvVar(
        path=BACKEND / ".env",
        key="BAIDU_MAP_AK",
        label="百度地图服务端 AK",
        why="真实步行路线、边界搜索和设施检索都依赖它；留空时所有在线查询都会失败。",
        where="百度地图开放平台 → 应用管理 → 我的应用 → 创建应用，类型勾选“服务端”",
        secret=True,
    ),
    EnvVar(
        path=FRONTEND / ".env.local",
        key="VITE_BAIDU_MAP_AK",
        label="百度地图浏览器端 AK",
        why="用于真实底图、地点搜索与定位。API 模式缺少时地图显示不可用提示但可手动输入坐标；只有显式 demo 模式才回退到本地示意图。",
        where="同一个控制台，再建一个应用，类型勾选“浏览器端”",
        secret=False,
    ),
)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_env_value(path: Path, key: str, value: str) -> None:
    """Set KEY=VALUE in place, keeping the file's comments, order and other keys."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replaced = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.partition("=")[0].strip() == key:
            lines[index] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def seed_env(target: Path, template: Path) -> bool:
    """Create the env file from its example. Existing files are never touched."""
    if target.exists():
        return False
    if not template.exists():
        return False
    target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    return True


def ask(prompt: str) -> str:
    return input(f"{COLORS['ask']}{prompt}{RESET}")


def report_env_status() -> list[EnvVar]:
    """Print every expected variable grouped by file; return the empty ones."""
    print()
    missing: list[EnvVar] = []
    for path in dict.fromkeys(var.path for var in ENV_VARS):
        values = read_env(path)
        print(f"  {path.relative_to(ROOT)}")
        for var in (item for item in ENV_VARS if item.path == path):
            value = values.get(var.key)
            if value:
                log("ok", f"    ✓ {var.key}（{var.label}）已填写")
            elif var.key in values:
                log("warn", f"    ✗ {var.key}（{var.label}）为空，需要你决定")
                missing.append(var)
            else:
                log("warn", f"    ✗ {var.key}（{var.label}）缺失，需要你决定")
                missing.append(var)
    return missing


def setup_env_vars() -> list[EnvVar]:
    """Report every expected variable, then let the user fill in or skip the empty ones."""
    missing = report_env_status()
    if not missing:
        print()
        log("ok", "环境变量全部就绪")
        return missing

    for var in missing:
        print(f"\n{COLORS['warn']}需要你处理：{var.label}{RESET}")
        print(f"  写在哪：{var.path.relative_to(ROOT)} 的 {var.key}")
        print(f"  为什么：{var.why}")
        print(f"  怎么拿：{var.where}")
        if not interactive():
            log("warn", "非交互模式，跳过填写")
            continue
        answer = ask("\n  [回车] 现在填写    [s] 跳过    > ").strip().lower()
        if answer in ("s", "skip", "n", "no"):
            log("dev", f"已跳过 {var.label}")
            continue
        if var.secret:
            value = getpass.getpass(f"  粘贴 {var.label}（不回显）> ").strip()
        else:
            value = ask(f"  粘贴 {var.label} > ").strip()
        if not value:
            log("warn", f"{var.label} 仍为空，已跳过")
            continue
        write_env_value(var.path, var.key, value)
        log("ok", f"已写入 {var.path.relative_to(ROOT)} 的 {var.key}")

    return [var for var in ENV_VARS if not read_env(var.path).get(var.key)]


def provider_guard() -> None:
    """Without a server AK the online algorithms cannot run; fall back to synthetic and say so."""
    provider = read_env(BACKEND / ".env").get("ANALYSIS_PROVIDER", "baidu")
    if provider != "baidu":
        log("dev", f"ANALYSIS_PROVIDER={provider}，按 .env 中的设置运行")
        return
    if read_env(BACKEND / ".env").get("BAIDU_MAP_AK"):
        log("dev", "ANALYSIS_PROVIDER=baidu，在线算法可用")
        return
    log("warn", "没有 BAIDU_MAP_AK 却仍是 baidu 模式，在线查询必然失败")
    if interactive():
        answer = ask("  [回车] 切换到离线 synthetic 模式    [k] 保持 baidu 模式    > ").strip().lower()
        if answer != "k":
            write_env_value(BACKEND / ".env", "ANALYSIS_PROVIDER", "synthetic")
            log("ok", "已改为 ANALYSIS_PROVIDER=synthetic（离线圆形，不调用百度）")
            return
        log("dev", "保持 baidu 模式；等你在 backend/.env 填好 BAIDU_MAP_AK 后重启")
    else:
        write_env_value(BACKEND / ".env", "ANALYSIS_PROVIDER", "synthetic")
        log("ok", "已自动改为 ANALYSIS_PROVIDER=synthetic（非交互模式不再追问）")


# ---------------------------------------------------------------- toolchain


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def venv_python_tag() -> str | None:
    """The 3.x tag the existing venv was built with, or None if it cannot be read."""
    if not venv_python().exists():
        return None
    try:
        result = subprocess.run(
            [str(venv_python()), "-c", "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def recreate_venv() -> None:
    log("warn", f"现有虚拟环境由 Python {venv_python_tag()} 创建，与当前的 {python_tag()} 不一致，重新创建")
    shutil.rmtree(VENV, ignore_errors=True)


def make_venv() -> None:
    existing = venv_python_tag()
    if existing == python_tag():
        return
    if venv_python().exists():
        recreate_venv()
    log("dev", f"创建虚拟环境 {VENV.relative_to(ROOT)}（Python {python_tag()}）")
    try:
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    except subprocess.CalledProcessError as error:
        log("err", f"创建虚拟环境失败（退出码 {error.returncode}）")
        raise InstallFailure("venv", "无法创建虚拟环境", [
            "确认 Python 安装完整，必要时重装并勾选 Add python.exe to PATH",
            "若 D 盘空间不足，把仓库移到空间更充足的磁盘",
        ]) from error


def deps_fingerprint() -> str:
    parts = []
    for path in (BACKEND / "requirements.lock.txt", ALGORITHM / "pyproject.toml"):
        parts.append(hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing")
    return " ".join(parts)


def pip_failures(output: str) -> list[str]:
    """Turn pip's noise into the few things a newcomer can actually act on."""
    hints: list[str] = []
    pip_exe = VENV / ("Scripts" if IS_WINDOWS else "bin") / ("pip.exe" if IS_WINDOWS else "pip")
    if "Cache entry deserialization failed" in output:
        hints.append(f"pip 缓存里有损坏条目，先清理：{pip_exe} cache purge")
    if "No matching distribution found" in output or "Could not find a version that satisfies" in output:
        hints.append(f"锁文件里某个包在 Python {python_tag()} 上没有可用轮子，说明当前源没有该版本：")
        for name, url in FALLBACK_INDEXES:
            hints.append(f"  换 {name} 重试：python dev.py setup --index-url {url}")
    if "Connection" in output or "timed out" in output.lower() or "network" in output.lower():
        hints.append("网络不通或被拦截，确认能访问 PyPI；公司网络可能需要配置代理")
    if not hints:
        hints.append("手动重跑以查看完整报错：")
        hints.append(f"  cd {BACKEND.relative_to(ROOT)}")
        hints.append("  .venv\\Scripts\\python.exe -m pip install -r requirements.lock.txt -e ../life-circle-algorithm")
    return hints


def stream(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run a child while echoing its output live, and keep a copy for error analysis.

    A silent three-minute install looks like a hang, so nothing is buffered silently.
    """
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env={**os.environ, **env} if env else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError:
        raise InstallFailure(
            "exec",
            f"要执行的程序不存在：{command[0]}",
            [
                f"确认 {command[0]} 已安装并在 PATH 中",
                "重新打开终端让 PATH 生效后再运行本脚本",
            ],
        ) from None
    except OSError as error:
        raise InstallFailure("exec", f"启动 {command[0]} 失败：{error}", [
            "确认杀毒软件或公司策略没有拦截该程序",
        ]) from error

    captured: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        captured.append(line)
        print(f"    {line.rstrip()}", flush=True)
    process.wait()
    return process.returncode, "".join(captured)


def run_pip(args: list[str], options: Options) -> None:
    """Install, and if the configured source cannot serve the lock file, retry elsewhere."""
    sources: list[tuple[str, str | None]] = [("当前源", options.index_url)]
    if not options.no_fallback_index:
        for name, url in FALLBACK_INDEXES:
            if url != options.index_url:
                sources.append((name, url))

    last_output = ""
    for position, (name, url) in enumerate(sources):
        if position:
            log("warn", f"换用 {name} 重试（{url}）")
        command = [str(venv_python()), "-m", "pip", *args]
        if url:
            command += ["--index-url", url]
        code, last_output = stream(command, BACKEND)
        if code == 0:
            return

    print()
    raise InstallFailure(
        "pip",
        f"后端依赖安装失败：{len(sources)} 个源都试过了（{', '.join(item[0] for item in sources)}）",
        pip_failures(last_output),
    )


def ensure_backend_deps(options: Options) -> None:
    make_venv()
    stamp = VENV / ".dev-deps-stamp"
    if stamp.exists() and stamp.read_text(encoding="utf-8").strip() == deps_fingerprint():
        log("dev", "后端依赖已就绪（跳过安装）")
        return
    log("dev", f"安装后端依赖（Python {python_tag()}，约 30MB，首次较慢；进度实时显示）")
    run_pip(["install", "-r", "requirements.lock.txt", "-e", str(ALGORITHM)], options)
    stamp.write_text(deps_fingerprint(), encoding="utf-8")


def npm_command(*args: str) -> list[str]:
    """A runnable npm invocation.

    On Windows npm ships as npm.cmd, which CreateProcess cannot execute directly, so it
    has to be launched through cmd.exe. shell=True is avoided on purpose.
    """
    npm = shutil.which("npm")
    if npm is None:
        raise InstallFailure("npm", "没有找到 npm，前端依赖无法安装", [
            "到 https://nodejs.org/ 下载 LTS 版本重新安装（安装时保持默认选项即可）",
            "装完重新打开终端让 PATH 生效，再运行本脚本",
            "如果 Node 是便携版，需要把解压目录加进 PATH",
        ])
    return ["cmd", "/c", npm, *args] if IS_WINDOWS else [npm, *args]


def ensure_frontend_deps() -> None:
    if (FRONTEND / "node_modules").exists():
        log("dev", "前端依赖已就绪（跳过安装）")
        return
    log("dev", "安装前端依赖 npm install（首次较慢；进度实时显示）")
    code, output = stream(npm_command("install"), FRONTEND)
    if code == 0:
        return
    print()
    raise InstallFailure("npm", f"前端依赖安装失败（npm 退出码 {code}）", npm_failures(output))


def npm_failures(output: str) -> list[str]:
    hints: list[str] = []
    if "registry" in output.lower() or "ETIMEDOUT" in output or "ENOTFOUND" in output or "ECONNRESET" in output:
        hints.append("npm 连不上默认源，换成国内镜像后重试：")
        hints.append("  npm config set registry https://registry.npmmirror.com")
    if "ENOENT" in output and "package.json" in output:
        hints.append(f"找不到 {FRONTEND.relative_to(ROOT)}/package.json，确认仓库完整（重新 clone）")
    if "EACCES" in output or "EPERM" in output:
        hints.append("权限不足，删除 life-circle-demo/node_modules 后重试；必要时以管理员身份运行")
    if "requires a peer dependency" in output or "ERESOLVE" in output:
        hints.append("依赖版本冲突，删除 life-circle-demo/node_modules 和 package-lock.json 后重试")
    if not hints:
        hints.append("手动重跑以查看完整报错：")
        hints.append(f"  cd {FRONTEND.relative_to(ROOT)}")
        hints.append("  npm install")
    return hints


# ---------------------------------------------------------------- processes


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def require_free_port(label: str, port: int) -> bool:
    if port_open("127.0.0.1", port):
        log("err", f"端口 {port} 已被占用，{label} 无法启动；关掉占用它的程序，或用 --{label}-port换一个端口")
        return False
    return True


def spawn(command: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen:
    kwargs: dict[str, object] = {"cwd": cwd, "env": {**os.environ, **env}}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)  # type: ignore[arg-type]


def wait_for(label: str, port: int, process: subprocess.Popen, limit: float = 120.0) -> bool:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log("err", f"{label} 启动失败，退出码 {process.returncode}，请看上方日志")
            return False
        if port_open("127.0.0.1", port):
            log("ok", f"{label} 已就绪 → http://127.0.0.1:{port}")
            return True
        time.sleep(0.3)
    log("err", f"{label} 在 {limit:.0f} 秒内没有就绪，请看上方日志")
    return False


def run_backend(port: int, provider: str | None) -> subprocess.Popen | None:
    if not require_free_port("backend", port):
        return None
    env = {"ANALYSIS_PROVIDER": provider} if provider else {}
    command = [
        str(venv_python()), "-m", "uvicorn", "app.main:app",
        "--host", "127.0.0.1", "--port", str(port), "--workers", "1", "--no-access-log",
    ]
    process = spawn(command, BACKEND, env)
    return process if wait_for("后端", port, process) else None


def run_frontend(port: int, backend_port: int) -> subprocess.Popen | None:
    if not require_free_port("frontend", port):
        return None
    version = node_version()
    if version is None:
        log("err", "没有找到 node，无法启动前端")
        log("dev", f"安装符合 Vite 8 要求的 Node.js 版本（{NODE_ENGINE_RANGE}）后重试")
        return None
    if not node_version_supported(version):
        log("err", f"Node.js 版本不兼容（当前 {node_version_text(version)}），Vite 8 要求 {NODE_ENGINE_RANGE}")
        return None
    node = shutil.which("node")
    assert node is not None
    vite = FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"
    if not vite.exists():
        log("err", f"缺少 {vite.relative_to(ROOT)}，前端依赖尚未安装")
        log("dev", "先运行 python dev.py setup（或去掉 --no-setup 重新启动）")
        return None
    command = [node, str(vite), "--host", "127.0.0.1", "--port", str(port), "--strictPort"]
    env = {"VITE_API_BASE_URL": f"http://127.0.0.1:{backend_port}"}
    process = spawn(command, FRONTEND, env)
    return process if wait_for("前端", port, process) else None


def backend_healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3):
            return True
    except (urllib.error.URLError, OSError):
        return False


def shutdown(processes: dict[str, subprocess.Popen]) -> None:
    for process in processes.values():
        if process.poll() is not None:
            continue
        try:
            if IS_WINDOWS:
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGINT)
        except (OSError, ProcessLookupError):
            process.terminate()
    deadline = time.monotonic() + 10
    for process in processes.values():
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            log("err", f"进程 {process.pid} 不响应关闭请求，强制结束")
            process.kill()


def supervise(processes: dict[str, subprocess.Popen], open_url: str | None, backend_port: int) -> None:
    stopped = threading.Event()

    # A background job in a non-interactive shell inherits SIGINT as ignored, so install
    # the handler explicitly; otherwise Ctrl+C would never reach the children.
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(stop_signal, lambda *_: stopped.set())
        except (OSError, ValueError):
            pass

    if open_url and ("backend" not in processes or backend_healthy(backend_port)):
        log("dev", f"正在打开 {open_url}")
        webbrowser.open(open_url)
    print("\n按 Ctrl+C 同时停止前后端。\n", flush=True)
    try:
        while not stopped.is_set():
            stopped.wait(0.5)
            dead = [name for name, process in processes.items() if process.poll() is not None]
            if dead:
                log("err", f"{'、'.join(dead)} 意外退出，正在停止其余进程")
                break
    except KeyboardInterrupt:
        pass
    finally:
        if stopped.is_set():
            log("dev", "正在停止前后端……")
        shutdown(processes)


# ---------------------------------------------------------------- commands


def prepare_env() -> None:
    step(1, 4, "准备配置文件")
    if seed_env(BACKEND / ".env", BACKEND / ".env.example"):
        log("dev", "已从 .env.example 生成 backend/.env")
    if seed_env(FRONTEND / ".env.local", FRONTEND / ".env.example"):
        log("dev", "已从 .env.example 生成 life-circle-demo/.env.local")

    step(2, 4, "检查环境变量")
    setup_env_vars()

    step(3, 4, "运行模式确认")
    provider_guard()


def doctor(backend_port: int, frontend_port: int) -> int:
    problems: list[str] = []
    log("dev", f"Python {sys.version.split()[0]}" + ("" if sys.version_info >= MIN_PYTHON else "  ← 需要 3.11+"))
    if sys.version_info < MIN_PYTHON:
        problems.append(f"Python {sys.version.split()[0]} 太旧，需要 3.11 或更新")
    version = node_version()
    if version is None:
        problems.append("PATH 里没有可运行的 node，请安装 Node.js")
    else:
        version_text = node_version_text(version)
        log("dev", f"Node {version_text}")
        if not node_version_supported(version):
            problems.append(f"Node {version_text} 不兼容，Vite 8 要求 {NODE_ENGINE_RANGE}")
    if not venv_python().exists():
        problems.append(f"缺少 {VENV.relative_to(ROOT)}，运行 python dev.py setup")
    if not (BACKEND / ".env").exists():
        problems.append("缺少 backend/.env，运行 python dev.py setup")
    if not (FRONTEND / "node_modules").exists():
        problems.append("缺少 life-circle-demo/node_modules，运行 python dev.py setup")
    for var in ENV_VARS:
        if not read_env(var.path).get(var.key):
            log("warn", f"✗ {var.key}（{var.label}）未填写")
            problems.append(f"{var.key} 未填写")
        else:
            log("ok", f"✓ {var.key}（{var.label}）已填写")
    for label, port in (("backend", backend_port), ("frontend", frontend_port)):
        if port_open("127.0.0.1", port):
            problems.append(f"端口 {port} 已被占用，{label} 无法启动")
    for problem in problems:
        log("err", problem)
    if not problems:
        log("ok", "环境检查通过")
    return 1 if problems else 0


def main() -> int:
    if sys.version_info < MIN_PYTHON:
        log("err", f"需要 Python 3.11 或更新，当前是 {sys.version.split()[0]}")
        log("err", "请到 https://www.python.org/downloads/ 安装，并勾选 Add python.exe to PATH")
        return 1

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="start",
                        choices=["start", "setup", "backend", "frontend", "doctor"])
    parser.add_argument("--backend-port", type=int, default=DEFAULT_BACKEND_PORT)
    parser.add_argument("--frontend-port", type=int, default=DEFAULT_FRONTEND_PORT)
    parser.add_argument("--provider", choices=["synthetic", "baidu"], help="临时覆盖 .env 里的 ANALYSIS_PROVIDER")
    parser.add_argument("--no-setup", action="store_true", help="跳过依赖与环境变量的准备")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--index-url", metavar="URL", help="pip 换源，例如 https://pypi.tuna.tsinghua.edu.cn/simple")
    parser.add_argument("--no-fallback-index", action="store_true", help="只用 --index-url 指定的源，不自动换源")
    args = parser.parse_args()
    options = Options(index_url=args.index_url, no_fallback_index=args.no_fallback_index)

    if args.command == "doctor":
        return doctor(args.backend_port, args.frontend_port)

    if not args.no_setup:
        try:
            preflight()
            if args.command == "setup":
                step(1, 4, "准备配置文件")
                if seed_env(BACKEND / ".env", BACKEND / ".env.example"):
                    log("dev", "已从 .env.example 生成 backend/.env")
                if seed_env(FRONTEND / ".env.local", FRONTEND / ".env.example"):
                    log("dev", "已从 .env.example 生成 life-circle-demo/.env.local")
                step(2, 4, "检查环境变量")
                setup_env_vars()
                step(3, 4, "运行模式确认")
                provider_guard()
            else:
                prepare_env()
            step(4, 4, "安装依赖")
            ensure_backend_deps(options)
            ensure_frontend_deps()
        except InstallFailure as failure:
            failure.report()
            return 1
        if args.command == "setup":
            print()
            log("ok", "准备完成。运行 python dev.py（或双击 start.bat）即可启动。")
            return 0
    elif args.command == "start":
        log("warn", "已跳过准备步骤，直接启动")

    processes: dict[str, subprocess.Popen] = {}
    open_url: str | None = None

    if args.command in ("start", "backend"):
        print()
        backend = run_backend(args.backend_port, args.provider)
        if backend is None:
            return 1
        processes["backend"] = backend
        if args.command == "backend":
            supervise(processes, None, args.backend_port)
            return 0

    if args.command in ("start", "frontend"):
        print()
        frontend = run_frontend(args.frontend_port, args.backend_port)
        if frontend is None:
            shutdown(processes)
            return 1
        processes["frontend"] = frontend
        open_url = None if args.no_browser else f"http://127.0.0.1:{args.frontend_port}/"

    supervise(processes, open_url, args.backend_port)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
