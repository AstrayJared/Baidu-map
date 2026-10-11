# 15 分钟生活圈

以 900 秒步行预算重建生活圈范围：E8.2 用百度边界搜索做真实步行端点证据的径向搜索，Hybrid v1.5 用 OSM 路网提供参考、百度详细步行路线核验。前端是 React + Vite 的百度地图工作台。

## 先决条件

只需要两样东西，版本要求如下：

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.11 或更新 | 装的时候勾选 **Add python.exe to PATH** |
| Node.js | `^20.19.0` 或 `>=22.12.0` | 与前端锁定的 Vite 8 版本要求一致 |

除此之外（虚拟环境、pip 依赖、npm 依赖、`backend/.env`）都由启动脚本自动准备，不需要手动执行任何 `pip install` 或 `npm install`。

## 四步启动

**第 1 步** 克隆仓库：

```bash
git clone https://github.com/PennEwan/Baidu-map.git
cd Baidu-map
```

**第 2 步** 双击启动器：

- Windows：双击 `start.bat`
- macOS：双击 `start.command`
- Linux：终端里执行 `./start.command`

**第 3 步** 按提示回答问题。

启动器会依次完成四件事，并在每一步打印当前状态：

```
[ok] 环境预检通过：Python 3.12，Node 24

== 1/4 准备配置文件 ==
[dev] 已从 .env.example 生成 backend/.env
[dev] 已从 .env.example 生成 life-circle-demo/.env.local

== 2/4 检查环境变量 ==

  backend/.env
[warn]     ✗ BAIDU_MAP_AK（百度地图服务端 AK）为空，需要你决定
  life-circle-demo/.env.local
[warn]     ✗ VITE_BAIDU_MAP_AK（百度地图浏览器端 AK）为空，需要你决定

需要你处理：百度地图服务端 AK
  写在哪：backend/.env 的 BAIDU_MAP_AK
  为什么：真实步行路线、边界搜索和设施检索都依赖它；留空时所有在线查询都会失败。
  怎么拿：百度地图开放平台 → 应用管理 → 我的应用 → 创建应用，类型勾选"服务端"

  [回车] 现在填写    [s] 跳过    >
```

对每一个空的变量，脚本都会告诉你**它写在哪、为什么需要、从哪拿**，然后让你选择回车填写或按 `s` 跳过。填写的值直接落盘到对应的 `.env`，已有的注释和其他配置都不会被破坏。

两个变量都可以跳过：

| 跳过后果 | 影响 |
| --- | --- |
| 跳过 `BAIDU_MAP_AK` | 可将 E8.2 后端切到 `synthetic` 测试替身（约 1080 米正圆，不是实际分析结果）；此设置不覆盖独立的 Hybrid 路径 |
| 跳过 `VITE_BAIDU_MAP_AK` | 默认 API 工作台显示“地图不可用”，仍可手动输入坐标；只有显式设置 `VITE_ANALYSIS_MODE=demo` 才回退到本地示意地图 |

**第 4 步** 前后端就绪后，浏览器会自动打开 <http://127.0.0.1:5173>。后端在 <http://127.0.0.1:8000>，`/health` 可查状态。按 **Ctrl+C** 一次同时停止前后端。

## 关于百度地图 AK

两个 AK 用途不同，按需要分别配置：

- **服务端 AK**（`backend/.env` 的 `BAIDU_MAP_AK`）：真实步行分析、Hybrid 核验和在线设施查询使用；只放在后端，绝不能出现在前端代码或浏览器里。
- **浏览器端 AK**（`life-circle-demo/.env.local` 的 `VITE_BAIDU_MAP_AK`）：浏览器端底图、地点搜索与定位使用；按域名做 Referer 限制。

两者都在[百度地图开放平台](https://lbsyun.baidu.com/apiconsole/key)申请，需要分别创建应用并勾选对应类型。

E8.2 的 `synthetic` 仅替代后端步行 Provider，用于离线测试，不产生有意义的圈面；它不会把 Hybrid 切成离线模式。若设置了浏览器端 AK，地图 SDK、地点搜索和定位仍会访问百度服务。默认 API 工作台缺少浏览器端 AK 时可手动输入坐标；本地示意图仅属于显式启用的 `demo` 模式。

## 命令行用法

启动器只是薄薄一层封装，需要更细的控制时可以直接调用 `dev.py`：

```bash
python dev.py                 # 完整引导流程（start.bat 跑的就是这个）
python dev.py setup           # 只做环境准备，不启动服务
python dev.py doctor          # 只体检：版本、依赖、变量、端口，缺什么报什么
python dev.py backend         # 只启动后端
python dev.py frontend        # 只启动前端
```

常用参数：

| 参数 | 作用 |
| --- | --- |
| `--provider synthetic\|baidu` | 临时覆盖 `.env` 里的 `ANALYSIS_PROVIDER`，不改文件 |
| `--backend-port 8000` | 换后端端口（默认 8000），被占用时会提示 |
| `--frontend-port 5173` | 换前端端口（默认 5173） |
| `--no-browser` | 不自动打开浏览器 |
| `--no-setup` | 跳过准备步骤直接启动 |
| `--index-url URL` | pip 换源，指定后仍会在失败时自动回退 |
| `--no-fallback-index` | 只用 `--index-url` 指定的源，不自动换源 |

非交互环境（CI、管道输入）下不会追问，会自动降级到 `synthetic` 并把结果打印出来。

## 目录结构

| 路径 | 内容 |
| --- | --- |
| `backend/` | FastAPI 服务，算法编排、百度与 OSM 访问、配额账本 |
| `life-circle-algorithm/` | 可复用的步行等时圈算法包（`pip install -e` 装进后端） |
| `life-circle-demo/` | React + Vite 前端 |
| `data/osm/` | OSM 数据与图缓存，需自行下载，见 [数据说明](data/osm/README.md) |
| `dev.py` | 一键启动器，零依赖，仅用标准库 |

`start.bat` 与 `dev.py` 需要一起保留：前者负责找到系统 Python 并转交参数，后者承载全部逻辑。两者都需要提交进版本库。

`backend/.env`、`life-circle-demo/.env.local`、`backend/.venv/`、`life-circle-demo/node_modules/` 都已在 `.gitignore` 中，由启动脚本按需生成。

## 单独启动（可选）

调试单个服务时：

```powershell
# 后端
cd backend
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt -e ../life-circle-algorithm
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log

# 前端
cd life-circle-demo
npm install
npm run dev
```

## 常见问题

**端口被占用。** 脚本会明确报出是哪个端口、建议换哪个：

```
[err] 端口 8000 已被占用，backend 无法启动；关掉占用它的程序，或用 --backend-port换一个端口
```

**后端报 `Invalid backend configuration; check .env format.`。** `backend/.env` 里有格式非法的值。删除该文件后运行 `python dev.py setup` 重新生成，再按提示填写。

**依赖装不上。** 脚本会自动换源重试，不会把 Python 堆栈甩给你。默认依次尝试你配置的源、清华镜像、阿里云镜像、官方源，并在每次尝试时把 pip 的输出实时打出来（不会有几分钟的静默）。全部失败才会给建议：

```
[warn] 换用 清华镜像 重试（https://pypi.tuna.tsinghua.edu.cn/simple）
[warn] 换用 阿里云镜像 重试（https://mirrors.aliyun.com/pypi/simple/）
[warn] 换用 官方源 重试（https://pypi.org/simple）

[err] 后端依赖安装失败：4 个源都试过了（当前源, 清华镜像, 阿里云镜像, 官方源）
    pip 缓存里有损坏条目，先清理：...\pip cache purge
    锁文件里某个包在 Python 3.14 上没有可用轮子，说明当前源没有该版本：
      换 清华镜像 重试：python dev.py setup --index-url https://pypi.tuna.tsinghua.edu.cn/simple
      ...
```

整个后端依赖约 30MB。也可以手动指定源（公司网络通常需要这一步）：

```bash
python dev.py setup --index-url https://pypi.tuna.tsinghua.edu.cn/simple
python dev.py setup --index-url https://pypi.tuna.tsinghua.edu.cn/simple --no-fallback-index  # 只用这一个源
```

**换过 Python 版本。** 脚本会比对虚拟环境的 Python 版本，不一致时自动删除并重建，不需要手动清理 `backend/.venv`。

**装到一半断了。** 已下载的包留在 pip 缓存里，重新运行 `python dev.py` 会接着装，不用从头来。

**npm 报网络错。** 换国内镜像后重试：

```bash
npm config set registry https://registry.npmmirror.com
```

**依赖版本冲突（ERESOLVE）。** 删除 `life-circle-demo/node_modules` 和 `package-lock.json` 后重新运行 `python dev.py`。

**OSM 图相关提示 degraded。** `data/osm/` 里的数据不在版本库中，需要按 [数据说明](data/osm/README.md) 自行下载。E8.2 百度模式不依赖它，Hybrid 模式才需要。
