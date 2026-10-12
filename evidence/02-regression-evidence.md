# A3｜针对性复现与端到端数据流回归

**任务**：`scheme.md` A3
**执行时间**：2026-10-08；**2026-10-11 追加 §5（本轮交付的验收复核）**
**口径**：全部使用 mock/synthetic provider、临时 SQLite、受控时钟、外连阻断（`conftest.py` 的全会话防护记录为 **0 次尝试**）。测试穿过真实链路 `manager → store → facilities → poi/plan → OnlinePlanner → poi/cache → KeyedCache → ServicePool`，不只断言新 helper。

---

## 1. 计量记录（scheme.md §10.2 格式）

来源：`/tmp/baidu-verify/a3_evidence.py`（可重复执行的离线探针，不读密钥、不联网）。字段原样保留，下面为可读汇总。

> **2026-10-09 溯源性说明**：该探针脚本放在临时目录，**现已不存在**，因此上表是那一次运行的记录，
> 不能凭本文件重新执行得到同样的字节。等价计量的可复现入口在 C 阶段补齐为
> `backend/tools/poi_query_benchmark`（`--suite baseline`），但它用的是自己的一套合称夹具，
> 覆盖的是"冷启动 / 全缓存 / 上游故障 / 紧预算"这些**同一类分支**，数值与上表并不逐项相等
> （例：上表冷启动为 112 页/60 派发，基准夹具的同类场景为 116 页/60 派发）。
> 也就是说：**A3 的结论有记录、有断言、有对应回归用例，但其原始探针数字不可重放**；
> 这一点按 `scheme.md` 的证据要求如实标注，而不是把 `07` 的数字说成是它的重跑。


| 场景 | 页面处理 | 缓存/共享 | 新增网络 | 任务/日账本增量 | 成功页 | 状态 / stopReason |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 冷查询（核心三类、4 分块、预算 60） | 112 | 0 / 0 | 60 | +60 | 60 | `partial` / `network_budget_exhausted` |
| 全缓存（首轮 56 页全在缓存，任务桶 0、日余额 0） | 56 | 56 / 0 | **0** | **+0** | 56 | `complete` / — |
| 部分缓存（可复用 8/20，预算 11＝缺页−1） | 20 | 8 / 0 | 11 | +11 | 19 | `partial` / `network_budget_exhausted` |
| 部分缓存（上一轮已缓存 19/20，预算 12） | 20 | 19 / 0 | **1** | +1 | 20 | `completed` / — |
| 幂等重试（补查成功、日余额清零，同 ID） | 0 | — | **0** | **+0** | — | 取回原补查（同 `extensionId`、原 `baseRevision=5`），未入队 |
| partial 后新标识补查（整轮需 112 页） | 112 | 56 / 0 | 52 | +52 | **60 → 112** | `completed` / — |
| 共享 in-flight 失败（派发方超时） | 2 | 0 / 1 | 1 | +1 | 0 | `failed` / `timeout`；派发方 `manner=live(dispatched=True)`，等待方 `manner=shared(dispatched=False)` |
| 纯缓存运行中途取消 | 0 | — | 0 | +0 | 0 | `cancelled` / `cancelled`（第一个调度步即响应） |

原始行（节选）：

```json
{"Task ID / scenario": "cold-core-3-categories", "Pages processed": 112, "Network calls dispatched": 60, "Task budget delta / Daily ledger delta": 60, "Status / stopReason": {"status": "partial", "stopReason": "network_budget_exhausted"}, "Facility evidence (successful pages)": 60, ...}
{"Task ID / scenario": "all-cached-zero-allowance", "Pages processed": 56, "Network calls dispatched": 0, "Task budget delta / Daily ledger delta": 0, "Status / stopReason": {"status": "complete", "stopReason": null}, "Facility evidence (successful pages)": 56, ...}
{"Task ID / scenario": "partly-cached-short-budget", "Pages processed": 20, "Network calls dispatched": 11, "Task budget delta / Daily ledger delta": 11, "Facility evidence (successful pages)": 19, ...}
{"Task ID / scenario": "partly-cached-exact-budget", "Pages processed": 20, "Network calls dispatched": 1, "Task budget delta / Daily ledger delta": 1, "Facility evidence (successful pages)": 20, ...}
{"Task ID / scenario": "partial-then-new-extension", "Pages processed": 112, "Network calls dispatched": 52, "Task budget delta / Daily ledger delta": 52, "Facility evidence (successful pages)": 112, ...}
{"Task ID / scenario": "shared-in-flight-failure", "Network calls dispatched": 1, "Task budget delta / Daily ledger delta": 1, ...}
{"Task ID / scenario": "cache-only-run-cancelled", "Pages processed": 0, "Network calls dispatched": 0, "Status / stopReason": {"status": "cancelled", "stopReason": "cancelled"}, ...}
{"external connection attempts refused": 0}
```

**核对**：每一行的"新增网络"与"账本增量"相等；页面处理数可以远大于新增网络（重放），这正是本轮修复点。

---

## 2. 场景覆盖（scheme.md A3 表 → 用例）

| scheme 场景 | 用例 | 关键断言 |
| --- | --- | --- |
| 冷查询、4 分块、核心三类首轮 | `test_both_engines_close_the_loop_over_their_own_boundary`、探针 §1 | 首轮 56 页派出、不虚报整轮完成 |
| 全缓存、任务与日余额都为 0 | `test_a_fully_cached_retrieval_runs_with_an_exhausted_task_bucket`、`test_a_fully_cached_retrieval_at_the_same_budget_costs_no_new_calls` | 网络 0、账本 +0、状态准确 |
| 部分缓存、新增额度恰好覆盖缺页 | `test_a_partly_cached_extension_spends_its_allowance_on_the_missing_pages` | 20 页处理 / 11 次新增 / partial；随后只差 1 页 |
| 先 60 页 partial，新标识补查 | 同上 `partial-then-new-extension`、`test_each_extension_advances_only_as_far_as_the_days_balance_allows` | 成功页 60 → 112，额度只花在缺页 |
| 原请求同 ID 重试、余额为 0 | `test_a_replay_after_the_days_allowance_is_gone_returns_the_original` | 同 `extensionId`/`baseRevision`，不发请求、不扣额度 |
| 同 ID 改参数 | `test_a_conflict_beats_an_exhausted_allowance` | 409 优先于余额判断 |
| 父任务新修订 / 默认预算变化 | `test_a_replay_keeps_its_original_revision_after_the_parent_publishes_another`、`test_an_omitted_budget_replay_survives_a_changed_default`、`test_a_replay_survives_a_latest_revision_whose_geometry_is_unusable` | 重试绑原修订；新标识才绑新修订；圈面不可用不影响重试 |
| 并发相同 ID / 相同页面共享 | `test_two_concurrent_extension_submissions_create_exactly_one_row`、`test_an_in_flight_page_shared_with_another_caller_is_not_a_new_call`、`test_a_failed_shared_call_is_not_a_dispatch_for_the_waiter` | 只创建一次；只有派发方计费；失败不写成功缓存 |
| 缓存过期 / 预检后额度被他人消耗 | `test_an_allowance_that_runs_out_after_the_precheck_still_bounds_every_dispatch` | 派发前最终校验、不超额、不伪成功 |
| 首轮已缓存但下一页缺失且预算 0 | `test_a_missing_page_after_a_fully_cached_first_round_is_partial_not_completed` | 保留证据、`partial`、缺页具名 |
| 持续 rate-limit / quota / 超时 | `test_a_dispatched_but_failed_call_is_still_a_new_network_call`、`test_a_refusal_before_dispatch_costs_nothing`、`test_a_facility_run_that_fails_every_page_is_a_failed_query` | 有界停止、已派发计费、不重试风暴、不写成零设施 |
| 取消 / deadline / 本地步数上限 | `test_a_cache_only_run_still_checks_cancellation_and_the_deadline`、`test_a_pure_cache_run_observes_a_cancellation_that_already_arrived`、`test_the_local_processing_ceiling_has_its_own_stop_reason` | 有限退出、原因分开 |
| 补查成功/失败/取消后父任务不变 | `test_a_facility_extension_reuses_the_boundary_and_leaves_the_report_alone`、`test_a_cancelled_extension_keeps_what_it_retrieved_and_never_touches_the_report`、`test_shutting_down_while_an_extension_runs_still_returns` | 修订数、报告逐字不变；关停不再卡死 |
| 北京时间跨日 / SQLite 重启 | `test_each_extension_advances_only_as_far_as_the_days_balance_allows`、`test_an_old_extension_table_gains_the_new_columns_without_touching_rows` | 跨日账本按新日期；旧库升级后旧行照读 |
| 60 个以上缓存页 + 少量新网络页 | `test_more_than_sixty_cached_pages_do_not_stop_the_run_before_the_missing_ones` | 处理 112 页、新增 3，旧口径只重放 60 页 |
| 前端/契约向后兼容 | `capabilities.test.ts`（旧后端缺 `poiPlanning`/`cache`）、`extensions.test.ts`（旧记录缺 `initialPlan`/`stopReason`） | 缺项省略、不写 0、不编造 |

---

## 3. 复现命令

```bash
cd backend
.venv/bin/python -m pytest -q tests/test_checkup_facilities.py tests/test_checkup_v2.py tests/test_cache.py \
    tests/test_poi_online.py tests/test_quota.py          # 126 passed
.venv/bin/python /tmp/baidu-verify/a3_evidence.py          # §1 的计量记录（离线探针）
```

> 第二行的探针路径**已不存在**（见 §1 的溯源性说明）。第一行的定向套件仍可随时重跑；
> 计量类结论可复现的入口请看 `07-baseline-benchmark.md`（`tools.poi_query_benchmark --suite baseline`）。

被修复缺陷的"先复现、后修复"证据（同一环境，改动前后对照）：

| 缺陷 | 复现证据（改动前） | 修复后 |
| --- | --- | --- |
| 共享失败归属 | 探针 `shared_timeout`：等待方 `dispatched=true` 但预算未动、账本仅 +1 | 等待方 `manner=shared`，`dispatched=false` |
| 纯缓存取消 | 探针 `cancel_during_cache_drain`：`status=completed, stopReason=null, processed=4` | `status=cancelled`，第一个调度步即停 |
| 关停卡死 | 去掉循环守卫后 `TestClient.__exit__` 阻塞（15 秒看门狗），默认预算亦可复现 | 退出立即返回 |
| 准入不一致 | 部分缓存 + 预算不足：manager `422`，设施阶段放行 | 两处都放行并按缺页执行 |
| 幂等晚于修订读取 | 最新修订几何不可用时，合法重试得 409 | 重试正常取回（新增用例固定） |

新增/更新的回归用例共 9 个（5 个本轮新增：共享失败归属、纯缓存取消、关停返回×2、几何不可用的重试；另更新 1 个准入期望、新增 2 个迁移/惰性读取用例）。

---

## 4. 结论

- 13 类 A3 场景全部有穿过真实链路的用例，且都带页数/派发/账本三类数字。
- 三个高严重度缺陷（共享失败归属、纯缓存取消、关停卡死）都先复现再修，并留下失败→通过的对照。
- 所有运行的外连尝试为 0。

---

## 5. 2026-10-11 验收复核（本轮交付）

**口径与 §1 相同**：无真实调用、外连 0 次、临时 SQLite 与受控时钟；两个环境各用自己的虚拟环境。

### 5.1 执行与结果

| 检查 | 环境 | 结果 |
| --- | --- | --- |
| 后端定向套件（额度 / 缓存 / 补查幂等 / 无边界报告） | WSL，Python 3.12.3 | **54 passed**（71.8 s） |
| 后端定向套件（同一批文件） | Windows，Python 3.14.5 | **54 passed**（81.4 s） |
| 后端全量套件 | WSL，Python 3.12.3 | **29 failed / 1103 passed / 10 skipped**（18 min 19 s）→ 归因见 §5.2 |
| 前端 `npx tsc -b` | WSL | 干净，无诊断输出 |
| 前端 `npm test` | WSL | **523 passed**（46 个文件） |
| 前端 `npm run build` | WSL | 成功（3739 模块；`built in 1.10s`） |
| 前端 `npm run test:checkup-ui` | WSL，Playwright | **20 passed**（1.2 min；`reuseExistingServer: false` 未改动） |
| 契约只读检查 `python -m tools.export_contract --check` | WSL | 9 份生成物与仓库一致 |
| 配置核验 `tools.verify_deployment_config` | WSL 与 Windows | 两份都"未发现问题"，外连各 **0** 次（`05` §1.3） |
| 全新 `git clone --shared` 后的 `git status` | WSL | **干净**（`start.bat` 行尾规范化后不再显示为已修改） |

### 5.2 全量套件 29 项失败的归因（同环境对照，不靠旧提交说明）

方法：把涉及失败的 9 个文件在**同一台机器、同一虚拟环境**下跑两次 ——
一次在 `origin/main`（`9cbca57`）的全新共享克隆里，一次在当前 HEAD（`2624704`）的主树里。

| 运行 | 结果 |
| --- | --- |
| 基线 `9cbca57` | **30 failed / 137 passed**（187.6 s） |
| 当前 `2624704` | **29 failed / 147 passed**（183.3 s） |

逐条比对 node id：

- **共同失败 29 条** → 基准已存在，**不是本次引入**；
- **只在基准失败 1 条** → 本次修复：
  `test_poi_acceptance.py::test_v3_parent_education_tag_is_not_a_training_business[合成小学-tags0-accepted]`
  （即 `教育培训` 容器标签那条，见 `CURRENT_STATE.md` 2026-10-10）；
- **只在当前失败 0 条** → 本次**没有引入任何失败**。

抽样看到的形态是**测试期望与已重构的分类词典 / 报告结构不一致**：`test_analysis.py` 仍要求
mock 的 major 只落在 `("shopping","medical","education")`，而词典早已扩展出
`care`/`dining`/`finance`；`test_catalog.py` 仍要求 `EXCLUSIONS` 含 `培训`，而按现在的口径
它属于 `NON_BUSINESS_PARENT_TAGS`。**这不是本次改动的回归，也不是依赖/导入失败** ——
这 29 条在 `origin/main` 上同样失败，本轮按要求不弱化、不跳过、也不顺手改它们，
留作独立的一次核对（它们可能与某个已合并的分类重构有关，需要单独判断）。
