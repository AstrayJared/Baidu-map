# 设施检索的预算、缓存与补查衔接：修复与验证报告

**分支**：`distribution-boost`　**基准提交**：`decce8784e0505fb9601c1d493c522d919c1e348`（本轮未提交、未推送、未改动任何部署配置）
**验证方式**：合成 provider、临时 SQLite、受控时钟；全程未使用真实 AK、未请求百度服务、未消耗线上额度，`conftest.py` 的全会话外连防护记录为 **0 次尝试**。
**配套材料**：完整证据文档 [backend/docs/设施检索预算与缓存修复-2026-10-08.md](backend/docs/设施检索预算与缓存修复-2026-10-08.md)；代码见下文"交付物清单"。

- 当前版本：`poi-categories-v2.2`
- 唯一分类来源：`backend/app/categories.json`
- 后台保留 31 个细分类，用于检索、归类、审计和接口兼容。
- 用户界面统一展示 5 个展示组，展示组变化必须同步目录接口、前端契约和文档。
- 分类规则变化必须升级版本号，不得覆盖历史版本的报告。
---

## 摘要（结论）

1. **已有结果不再被新额度检查挡住。** 预检改用规划器自己的首轮页键核对缓存：完全命中缓存的检索在网络额度为 0 时也能执行，一次都不联网；部分命中的检索按缺页放行，不做"完整冷启动页数"的错误拒绝。
2. **缓存重放不再耗尽新增调用预算。** 网络额度（服务池 + 日账本，唯一扣减入口）与本地处理上限（4096 步，停止原因 `processing_limit_reached`）分成两把尺子。超过 60 页的缓存前缀可以全部重放，额度只花在缺页上。
3. **补查把额度用于新增检索。** 幂等先于预算：同一请求标识取回原补查（不排队、不发请求、不扣额度，日余额为 0 也一样），参数不同返回 409 且优先于余额判断；原请求的合法重试绑定它自己的 `baseRevision`。首次 `partial` 后用新标识补查，能在缓存之后真正取到缺页（成功页 60 → 112）。
4. **限流、独立补查、部分结果与评分口径未被削弱。** 所有真实派发仍受任务预算、应用日预算与 `ServicePool.attempt()` 的派发前原子检查约束；额度用尽时保留缓存证据、缺页具名、结果报 `partial`；原任务的修订、报告、评分、灰区与预算桶均不被补查改动。
5. **需要如实说明的一点**："同一个地理位置重新体检"默认**不会**复用上一次的页面（页面缓存按任务归属，未配置跨任务窗口），所以它仍会重新花一次额度，密集数据下一次 60 次预算装不下整轮 112 页。这不是本轮修复的退步，而是部署取舍；要把这一路径也变成复用，需运营者决定是否开启 `CACHE_FRESHNESS_SECONDS`。详见 §4.2。

---

## 1. 审阅判断的复现结果

静态审阅的 8 项判断逐条复现（第 7 项被推翻，据实说明）：

| # | 审阅判断 | 复现 | 证据 |
| --- | --- | --- | --- |
| 1 | 补查先查余额再查幂等，重复提交可能拿到 429 | **复现**：补查成功后余额清零，重放同标识 → `429`，原补查取不回 | `test_a_replay_after_the_days_allowance_is_gone_returns_the_original` |
| 2 | 余额为 0 时参数冲突被余额覆盖 | **复现**：同标识换类别 → `429` 而非 `409` | `test_a_conflict_beats_an_exhausted_allowance` |
| 3 | 请求指纹含 `baseRevision`，父任务发新修订后合法重试变 409 | **复现**：发布新修订后重放同标识 → `409 checkup_extension_request_id_conflict` | `test_a_replay_keeps_its_original_revision_after_the_parent_publishes_another` |
| 4 | 身份含"新算出的运行时默认预算" | **复现**：`requested` 进入指纹，省略预算的重试随默认值/几何漂移 | `test_an_omitted_budget_replay_survives_a_changed_default` |
| 5 | 前置预算检查不认缓存，全缓存检索被任务桶挡住 | **复现**：56 页全在缓存、任务桶 0 → 检索根本没开始，零网络调用 | `test_a_fully_cached_retrieval_runs_with_an_exhausted_task_bucket` |
| 6 | 规划器把缓存重放计入同一份预算，第二轮停在旧边界 | **复现**：60 页缓存前缀 + 同预算 → 处理 60 页、新增调用 0、`budget_exhausted`；同一份输入换成两把尺子后立刻在缓存前缀之外取到缺页（该用例放开 3 次额度就发 3 次），证明后续页面确实存在 | `test_more_than_sixty_cached_pages_do_not_stop_the_run_before_the_missing_ones` |
| 7 | 前端仍用 `4 × 小类数 > 当日余额` 硬性禁用补查 | **未复现**：补查按钮唯一禁用条件是 `extensionRunning`，全前端没有把预算与该算术比较的代码。修正的是"至少/下界"这一误导文案 | `CheckupApp.tsx` 补查按钮；本轮未新增任何此类禁用 |
| 8 | 补查没有独立的本地处理上限 | **确认并修复**：新增 `processing_limit_reached`，与 `network_budget_exhausted` 分开 | `test_the_local_processing_ceiling_has_its_own_stop_reason` |

---

## 2. 修改内容

### 2.1 幂等先于预算（`checkups/manager.py`、`checkups/store.py`）

顺序改为：认领资源（任务 404 / 圈面 409）→ 按 `(taskId, clientRequestId)` 查找已有请求 → 命中且等价则立即返回原记录 → 参数不同则 409 → 只有确认为新请求才计算首轮计划与预算。

请求身份只认客户端声明的两件事（类别集合 + 是否显式给出预算），冻结在新列 `intent`；等价规则：

* 两次都省略 → 同一请求，默认值/几何/余额变化都不影响；
* 省略 ↔ 显式写出**当时解析出的那个默认值** → 同一请求（保留基准兼容语义）；
* 显式写出别的数字 → 冲突；
* 旧版本写下的行（`intent` 为 NULL）按它被创建时的规则判定（用本次解析出的预算与冻结的 `budget` 比较）。

数据库 `UNIQUE(task_id, client_request_id)` + `BEGIN IMMEDIATE` + `created` 标志仍是唯一创建点：并发落败方只取回那一行，不入队、不重复执行已做过的预算判断。

### 2.2 预检看得见缓存（新增 `poi/plan.py`，复用自 `facilities.py` / `manager.py`）

新增的首轮计划辅助函数直接复用**规划器自己的**第一轮（同一份 `coarse_blocks`、同一套类别轮转、同一批主关键词）与 `poi/cache.page_key` 的真实页键，产出 `initialPageCount`、`reusableInitialPageCount`、`estimatedNewInitialCalls`、`remainingTaskBudget`、`remainingDailyBudget`；`reusable_pages()` 只用 `KeyedCache.get()` 做只读查询（不 build、不刷新 `obtainedAt`、不加入 in-flight）。

判定规则（补查与设施阶段共用）：

* 预计新增调用为 0 → 不拒绝（零余额也放行）；
* 部分可复用 → 不拒绝，按缺页执行；
* 完全冷启动 → 保留必要拒绝（任务桶不足 422、日额度不足 429）并带全部数字；
* 预检不预留额度；派发前仍由 `ServicePool.attempt()` 做最终检查与原子扣减。

### 2.3 两把尺子（`poi/online.py`、`poi/cache.py`）

`OnlinePlanner` 接受显式 `RunLimits`：`processing_steps`（v2 为 `PROCESSING_STEP_LIMIT = 4096`）管本地处理（缓存重放、翻页、细分、重试），到顶报 `processing_limit_reached`；网络预算仍只由任务桶与日账本管。只有指名 `budget=` 的旧调用方保持原语义（页数上限 + 预算拒绝即停），由 `legacy_limits()` 固定，命令行与旧接口不受影响。

计量改为显式事实：`CachedPages` 每页返回 `PageResponse(payload, reason, manner)`，`manner ∈ {live, cache, shared, refused}` 由真正解决这次请求的那一层写出；规划器据此把 `network_calls` 只加在 `live` 上。**派发后失败（超时/网络错误）计入新增调用**，派发前的拒绝不计。契约里 `requests` / `networkRequests` 的含义未改，另加 `statistics.processedPages` / `networkCalls` / `processingLimit` / `allowanceRefusedPages` 便于核对。

额度用尽时的行为：`task_budget_exhausted` / `daily_budget_exhausted` 不再是整轮致命错误，而是那一页的结果；运行继续读完缓存里能答的页面，缺页逐页记名，整轮报 `network_budget_exhausted`。上游 `quota`、持续 `rate_limit`、`permission` 仍是有界致命停止并保留已取页面；纯缓存流程也检查取消与 deadline。

### 2.4 补查边界（未改变的口径）

补查复用冻结的原圈面、只查本次明确选择的类别、不重新成圈、不请求步行路线、不动原任务预算桶；自己的网络请求进入同一日账本；不修改原任务修订、报告、综合分与灰区；`partial` 仍是终态，同标识是取回原操作，要接着查必须用新标识。

### 2.5 诊断与文案（`checkups/models.py`、`checkups/router.py`、前端）

* `facilities.initialPlan`；补查状态/结果新增 `stopReason` 与 `initialPlan`（定稿冻结，读状态即可见）；
* `/api/v2/capabilities` 新增 `poiPlanning`（处理上限、两本账、首轮是估算）与 `cache.processLocal`；`budgets.poiRequestsIsLowerBound` 改为 **false**，新增 `poiFirstRoundIsAnEstimate: true`；
* 前端补查面板显示预算 / 页面处理 / 新增网络 / 首轮估算 / 停止原因；`requiredRequests` 更名 `estimatedFirstRoundPages`，文案改为"按最多 4 个查询分块估计的冷启动首轮页数……首轮够用不代表查完，命中缓存的页面不消耗网络额度"；**没有**新增按余额禁用补查的逻辑。

### 2.6 实现自查中发现并修掉的两处问题

1. **等价规则写反了一半**："省略"应当只在解析结果正好等于该行显式写的那个数时命中；初版条件对显式行恒为真，等于把省略变成通配符（160 的补查会被当成 60 的重复提交）。已修并补双向断言。
2. **派发后失败被记成"没发出去"**：超时/网络错误只可能发生在扣减之后，初版归为 `refused` 会让 `networkCalls` 比账本少算一次。已归为 `live` 并补断言。

---

## 3. 计量方式（一句话版）

* **网络额度** = 经 `ServicePool.attempt()` 真正派发的调用数，任务桶与应用日账本在派发前原子扣减，失败/超时/重试都计入、不退款；
* **本地处理上限** = 规划器调度的页面数（含缓存重放），独立、有限、可解释；
* **页面缓存** = 进程内存，同任务始终可用，跨任务仅在配置 `CACHE_FRESHNESS_SECONDS` 时按窗口可用；命中缓存不进配额池。

---

## 4. 实测记录

固定合成数据、临时 SQLite、受控时钟；`页面处理` = 规划器处理的页面数，`新增网络` = 真正派发的调用数，`账本` = 本应用 place 日账本增量。

### 4.1 五类流程（核心三类，每次预算 60）

| 流程 | 页面处理 | 新增网络 | 账本增量 | 结果 |
| --- | ---: | ---: | ---: | --- |
| 冷查询（4 分块） | 56 | 56 | +56 | 56 页全走网络，`completed` |
| 纯缓存（任务桶 0、日余额 0） | 56 | 0 | 0 | 全部重放，结果 `complete` |
| 部分缓存（父任务查 dining，补查 dining+leisure 只给缺页预算 12） | 20 | 12 | +12 | 可复用 8 页，按缺页执行 |
| 幂等重试（补查成功后清零余额，重放同标识） | 0 | 0 | 0 | 返回同一 `extensionId` 与同一 `baseRevision` |
| 首轮 `partial` → 新标识同类别补查 | 112 → 112 | 60 → 52 | +60 → +52 | 成功页 60 → **112**，`completed` |

### 4.2 同一地理位置、每次 60 次预算（追加验证）

固定坐标 `121.513925, 31.313079`，核心三类，密集合成数据整轮需 **112 页**（56 首轮页 + 56 第二页）：

| 场景 | 本次新增调用 | 成功取得页面 | 结果 |
| --- | ---: | ---: | --- |
| 默认档位（fallback，日额度 80）首次体检 | 60 | 60 | `partial`，`stopReason=network_budget_exhausted` |
| 默认档位，**同位置新建体检**（新标识） | 0 | 0 | 设施阶段被拒：`首轮需要 56 页…缓存可复用 0 页…今天还剩 20 次`；任务本身仍 `completed` |
| 默认档位，**原任务新补查** | 20 | 80 | `partial`，日额度归零 |
| 默认档位，同日余额 0 再补查 | 0 | 80 | `partial`，预检放行但不超额 |
| 默认档位，**跨日**后再补查 | 32 | **112** | `completed`（北京时间跨日） |
| 日额度充足，同位置新建体检 | 60 | 60 | `partial`（默认不跨任务复用） |
| 日额度充足 **且开跨任务窗口**，同位置新建体检 | 52 | **112** | `completed` |
| 首轮页面全在有效缓存，原任务补查 | 0 | 56 | `completed`，账本不变 |
| 同一请求标识重复提交（首次 `partial`） | 0 | 60 | 取回原任务，报告逐字不变 |

判读规则：**任务 `completed` 不等于设施查完**（读 `facilitiesStatus` / `facilities.queryStatus` / `stopReason`）；**同位置 ≠ 同任务**；**112 个未缓存页面确实超过一次 60 次预算**，这是正常限制，未通过放大额度或把 `partial` 当完成来消除。

---

## 5. 验证执行与结果

| 命令 | 改动前 | 本轮 |
| --- | --- | --- |
| `cd backend && .venv/bin/python -m pytest -q` | 42 failed / 968 passed / 10 skipped | **34 failed / 1004 passed / 10 skipped** |
| `pytest -q tests/test_checkup_facilities.py tests/test_cache.py tests/test_poi_online.py tests/test_quota.py` | — | **104 passed**（收集数 33 + 22 + 27 + 22） |
| `cd life-circle-demo && npm test` | 40 files / 446 tests | 41 files / **458 tests passed**（前端本轮未改动，沿用同一份前端代码上一轮的实际运行） |
| `cd life-circle-demo && npm run build` | 通过 | 通过（`tsc -b && vite build`） |
| Playwright（`test:e2e` / `test:checkup-ui` / `test:integration`） | 不可运行 | **未运行**：各 config 指定 `channel: 'msedge'`，本机无 Edge |

### 5.1 失败归因（同一环境前后逐条对照）

* **本次引入：0 项。** 两份失败清单逐条比对，没有出现基线里没有的失败。
* **基线已存在、本轮修好：8 项。** 7 项是 `test_checkup_facilities.py` 里仍在用旧类别数字（6 小类 × 4 块 = 24 页年代）的用例——它们与 3→10 类别重构脱节，本轮按词典推导期望值（断言未删未放宽）：`test_both_engines_close_the_loop_over_their_own_boundary`、`test_the_boundary_decides_what_counts_and_the_search_range_only_reaches`、`test_the_facility_layer_draws_findings_and_stays_tied_to_its_revision`、`test_the_verification_layer_draws_what_was_asked_and_what_came_back`、`test_a_cached_page_costs_no_attempt_and_keeps_its_data_time`、`test_a_facility_run_that_fails_every_page_is_a_failed_query`、`test_a_route_detail_is_only_offered_for_a_facility_of_this_checkup`；另 1 项 `test_facility_stage.py::test_the_daily_allowance_is_checked_before_the_request` 是夹具缺陷（未固定档位切换时间，切换日后生效的是 fallback 档从而真的发了请求），修的是夹具而非断言。
* **基线已存在、范围外：34 项。** 分布：`test_facilities` 10、`test_poi_service` 5、`test_poi_acceptance` 4、`test_catalog` 4、`test_business_api` 3、`test_poi_tool` 2、`test_checkup_progress` 2、`test_poi_evidence` 1、`test_place_safety` 1、`test_checkup_report` 1、`test_analysis` 1。集中在：①3→10 类别重构后的词典/分类旧用例；②旧 POI 运行时网格与离线 CLI；③报告/进度/业务 API 的既有偏差；④生成物漂移（旧 `api-contract.ts` 等在基准版本即已过期，重跑导出会一次性改写约 12k 行，与本轮无关，已回退）。
* 两次全量运行的外连尝试均为 **0 次**。

### 5.2 未验证范围

真实百度服务、真实 AK、真实额度、重启后的断点续查、跨进程缓存、分布式限流、浏览器端到端（无 Edge）均未验证；本轮结论全部来自合成 provider 与受控时钟。

---

## 6. 运营者需要核实的配置与诊断步骤

名称和业务标签使用同一套主类选择规则。目录的 `nameShadows` 只允许具体词在相同文本位置遮蔽其包含的泛化词；其他位置的独立命中仍须检查冲突。无声明关系的名称和标签冲突返回 `needs_review`，不进入正式数量、覆盖或密度统计，旧名称接口也不接纳该记录。

严格 POI 流程要求业务标签证据，旧接口仍允许名称判定。`教育培训` 等父标签不作为业务证据；独立培训证据仍排除。`美术馆` 内包含的 `美术` 不作为独立培训命中。

## 4. 接口约束
1. **先看生效档位。** `/api/v2/capabilities` 的 `quota.tier` 报出 `current` 还是 `fallback`；切换时间（默认 `2026-09-30T00:00:00+08:00`）之后只提高 `BAIDU_PLACE_DAILY_BUDGET` **不会有任何效果**，要调的是 `BAIDU_FALLBACK_PLACE_DAILY_BUDGET`。本轮未改动任何默认值或切换时间。
2. **四种限制互相独立**：QPS/并发、单任务预算（`facilities.maxPoiRequests`，默认 60、上限 160）、应用日预算（本应用账本，按北京时间跨日、重启不清零）、百度账号真实配额（不在本账本里，须按控制台授权核实）。
3. **缓存口径**：`cache.processLocal=true`，`freshnessSeconds` 默认为 null（仅同任务内复用）。开启跨任务窗口是数据使用决定；本轮没有默认开启，也没有把缓存写成"永久有效"或"重启后仍在"。
4. **同一位置想接着查**：用原任务的补查接口（新补查标识），无需改配置；要让**重新体检**也复用上一次页面，才需要开启跨任务窗口。
5. **诊断路径**：capabilities 看档位/余额/缓存/处理上限 → `facility-extensions/{id}` 看 `requests` / `networkRequests` / `stopReason` / `initialPlan` → 任务结果看 `facilities.statistics`（`processedPages`、`networkCalls`、`allowanceRefusedPages`）与 `facilities.initialPlan`。预算拒绝的报文同时给出首轮页数、可复用页数、预计新增调用数与当前预算。

---

## 7. 仍不支持的能力

* **没有持久化断点续查**：页面缓存是进程内存，重启即失效；`partial` 是终态，不自动跨日恢复、不自动续跑。后续若要做，需持久化"成功页面 + 未完成队列"，并同时处理数据使用许可、有效期、查询计划/规则版本、圈面修订、页码与细分状态、幂等与重复扣费。
* **没有跨进程共享缓存、分布式限流、多 AK 轮换**：并发与账本仍以单进程 + SQLite 账本为准。
* **没有真实浏览器端到端与真实百度验收**。
* 补查仍只查本次明确选择的类别、不重新成圈、不请求步行路线、不修改原任务修订/报告/评分/灰区。
* 一次 60 次预算装不下密集数据整轮 112 页：这是真实限制，需由运营者按核实到的授权决定是否提高部署额度。

---

- 展示组统计只按设施主类映射计算，次类不能再次加入总数。
- 地图可以按展示组着色，但聚合只改变绘制方式，不删除或合并业务记录。
- `in_circle` 为 `null` 的设施不能计入圈内数量或热力密度。
- 检索词、名称词和标签词必须来自分类字典，业务模块不得维护私有分类表。
- 总分要求十个后台大类都有可用空间支持，每类权重为 `1/10`；三类数据只能形成分类结果。
## 8. 交付物清单

**新增（5）**

| 文件 | 说明 |
| --- | --- |
| `backend/app/poi/plan.py` | 首轮计划与成本估算：真实页键、缓存可复用页数、预计新增调用数 |
| `life-circle-demo/src/checkup/extensions.ts` | 补查面板的纯渲染辅助（预算/首轮估算/停止原因） |
| `life-circle-demo/src/checkup/extensions.test.ts` | 上述辅助的 8 项单测 |
| `backend/docs/设施检索预算与缓存修复-2026-10-08.md` | 完整证据文档 |
| `report.md` | 本报告 |

**修改（27）**：`backend/app/`：`cache.py`、`poi/cache.py`、`poi/online.py`、`checkups/{facilities,manager,models,places,router,store}.py`；`backend/tests/`：`test_cache.py`、`test_checkup_facilities.py`、`test_checkup_v2.py`、`test_facility_stage.py`、`test_poi_online.py`；说明：`CURRENT_STATE.md`、`backend/README.md`、`backend/.env.example`、`life-circle-demo/README.md`；前端：`src/checkup/{CheckupApp.tsx,capabilities.ts,capabilities.test.ts,categories.ts,categories.test.ts,checkup.css,contract.ts,fixtures.ts,validate.ts}`。

> 原先根目录的 `report.md`（2026-10-05 的《设施分类规则约束报告》，内容与本任务无关）已原样保存为 `backend/docs/设施分类规则约束报告-2026-10-05.md`，可用 `git show c030bca:report.md` 复核。

**新增回归测试（本轮验证相关）**：`test_the_same_coordinates_in_a_new_task_do_not_reuse_the_first_tasks_pages`、`test_each_extension_advances_only_as_far_as_the_days_balance_allows`、`test_a_fully_cached_retrieval_at_the_same_budget_costs_no_new_calls`、`test_replaying_a_checkup_request_id_never_starts_a_second_retrieval`、`test_cross_task_reuse_is_the_deployment_option_that_lets_a_new_task_continue`（+ 上一轮的 `test_a_cache_hit_is_not_a_new_network_call`、`test_the_local_processing_ceiling_has_its_own_stop_reason`、`test_a_spent_allowance_stops_the_pages_that_need_it_not_the_run`、`test_a_cache_only_run_still_checks_cancellation_and_the_deadline`、`test_more_than_sixty_cached_pages_do_not_stop_the_run_before_the_missing_ones`、`test_a_missing_page_after_a_fully_cached_first_round_is_partial_not_completed`、`test_an_allowance_that_runs_out_after_the_precheck_still_bounds_every_dispatch`、`test_an_in_flight_page_shared_with_another_caller_is_not_a_new_call` 等）。

---

## 9. 验收对照

| 验收要求 | 结论 |
| --- | --- |
| 已有结果不再被新额度检查挡住 | ✅ 全缓存零余额可执行；部分缓存按缺页执行 |
| 缓存重放不耗尽新增调用预算 | ✅ 处理 112 页、新增调用仅 52（对照旧口径：重放 60 页即停、缺页 0） |
| 补查能把额度用于缺页 | ✅ 部分缓存补查 20 页处理 / 12 次新增，全部花在缺页 |
| 真实派发严格遵守任务预算、日预算与限流 | ✅ 派发前原子扣减；缺额不超额、失败计入、不退款 |
| 原报告及评分保持不变 | ✅ 补查前后结果文件逐字相同；修订数不变 |
| 幂等不被余额覆盖、冲突优先 | ✅ 零余额重放取回原记录；改参数 409 |
| 部分结果与核心评分口径 | ✅ `partial` 保留证据并具名，绝不解释为零设施 |

**日期**：2026-10-08
