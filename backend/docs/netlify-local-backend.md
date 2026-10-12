# Netlify 前端 + 本机后端

前端发布到 Netlify，本机运行 Python 后端，通过 Cloudflare Quick Tunnel 提供临时 HTTPS 地址。适用于演示与小范围试用，不需要购买云服务器或域名。

## 本次发布状态

正式站点：https://linyu-life-circle.netlify.app 。已解除站点登录保护，匿名访问正常。公网 `/health`、`/api/facility-catalog`、`/api/v2/capabilities` 与精确来源 CORS 已验证。

2026-10-09 更新浏览器端 AK 后重新构建、发布，百度授权校验返回 `error: 0`，底图正常绘制，浏览器无未捕获错误，后端能力接口返回 200。此前线上构建仍使用旧 AK；修改本地 `.env.local` 不会自动更新已发布的静态文件，必须重新构建和发布。本次未提交真实体检任务。

## 本次本地运行文件

所有运行状态保存在仓库忽略的 `.tmp/public-demo/`，不提交密钥或临时进程信息：

- `session.json`：后端和隧道的 PID、启动时间、临时地址，以及配置完成后的 Netlify 站点信息。
- `backend.stderr.log`、`tunnel.stderr.log`：服务与隧道日志。
- `checkups/`、`ledgers/`：此次公网演示的任务与报告，不暴露原有本地任务目录。

项目专用工具安装在 `.tmp/deploy-tools/`。Netlify 账号授权由官方 CLI 保存，勿将其令牌写入源码。

## 启动与发布顺序

1. 保留 `backend/.env` 中现有服务端百度密钥及 OSM 数据路径。
2. 用单进程启动后端，监听 `127.0.0.1:8000`，任务目录指向 `.tmp/public-demo/checkups`。不要同时启动第二个使用相同百度账号的后端，避免多个进程各自进行请求节流。
3. 启动隧道：

   ```powershell
   .\.tmp\deploy-tools\cloudflared.exe tunnel --no-autoupdate --protocol http2 --url http://127.0.0.1:8000
   ```

4. 记录命令打印的 `https://….trycloudflare.com`。检查该地址的 `/health` 和 `/api/facility-catalog`。
5. 创建或复用自己的 Netlify 站点，记下正式的 `https://….netlify.app` 地址。
6. 后端的 `CORS_ORIGINS` 加入该 Netlify 精确来源，保留本地来源后重启后端；不要使用 `*`。
7. 前端以 `VITE_ANALYSIS_MODE=api`、`VITE_API_BASE_URL=临时隧道HTTPS地址` 构建。`VITE_BAIDU_MAP_AK` 仅使用浏览器密钥，并在百度控制台为正式站点配置来源白名单。
8. 将 `life-circle-demo/dist` 发布到同一个 Netlify 站点，只上传构建产物。

每次重新创建 Quick Tunnel，地址通常都会变化。需要更新前端 API 地址并重新构建、发布；仅重启后端但保留隧道进程时，隧道地址不变。

## 停止与验证

演示结束时停止本次创建的后端和隧道进程。进程号可能被系统复用，结束进程前必须同时核对 `session.json` 的启动时间，不能只复制旧 PID 后执行停止命令。

电脑休眠、断网或隧道退出后，Netlify 页面仍能打开，但实时计算不可用。历史报告保留在本机演示目录中。免费托管与隧道不代表百度 API 额度无限，真实体检仍使用原账号的额度。

验证健康检查、分类目录与 CORS 不会发起百度检索；提交体检会。先确认地图白名单与页面连通，再按需要执行小预算真实体检。

参考：[Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)、[Netlify CLI](https://docs.netlify.com/api-and-cli-guides/cli-guides/get-started-with-cli/)。
