"""§5 B2 决策 1 的重试：同一次体检的新修订。

这个文件钉住三件事：

1. **它是同一次体检** —— 重试发布的是这个任务的**新修订**，圈面和类别都取自被冻结的
   那一版，所以"这次查完了没有"的答案就在这一份报告里，而不是旁边多一个文件。
2. **它只花缺页的钱** —— 首轮页面已经在这个任务的缓存里，重放不计新调用；网络额度只
   花在真正没取到的页面上，而且账本对得上。
3. **旧修订一个字不改** —— 已发布的修订是不可变证据：重试不许回头改写它。

外加两条与补查共用的规则：同一个请求标识回来取回原记录、换了预算才是 409（而这条必须
**先于**"已经查完、无需重试"生效，否则一次成功的重试会把那个标识变成冲突）；以及
"改类别"在这个接口上根本表达不出来 —— 类别不在重试请求里给。
"""
import time

from fastapi.testclient import TestClient

from test_checkup_facilities import (CORE_MAJORS, SyntheticPlaces, at_origin, body, document,
                                     make_app, run, two_page_rows)

RETRIES = "/api/v2/checkups/{task}/retries"
FINAL = ("completed", "partial", "failed", "cancelled")


def start_partial_checkup(client, budget=60):
    """一次**没查完**的体检：密集两页数据，预算只够首轮与 4 个第二页。"""
    task_id, _view = run(client, body(facilities={"categories": CORE_MAJORS,
                                                 "maxPoiRequests": budget}))
    first = document(client, task_id)
    assert first["facilities"]["queryStatus"] == "partial", first["facilities"]["queryStatus"]
    assert first["facilities"]["queryAreaCoverage"]["status"] == "unmet"
    return task_id, first


def post_retry(client, task_id, client_request_id, **overrides):
    payload = {"schemaVersion": "checkup-v1", "clientRequestId": client_request_id}
    payload.update(overrides)
    return client.post(RETRIES.format(task=task_id), json=payload)


def submit_retry(client, task_id, client_request_id, **overrides):
    created = post_retry(client, task_id, client_request_id, **overrides)
    assert created.status_code == 202, created.text
    return created.json()


def finish_retry(client, task_id, retry_id, timeout=180.0):
    deadline = time.monotonic() + timeout
    view = client.get(f"{RETRIES.format(task=task_id)}/{retry_id}").json()
    while time.monotonic() < deadline:
        if view["status"] in FINAL:
            return view
        time.sleep(0.02)
        view = client.get(f"{RETRIES.format(task=task_id)}/{retry_id}").json()
    raise AssertionError(f"重试没有在 {timeout}s 内结束：{view}")


def test_a_retry_publishes_a_new_revision_of_the_same_checkup(tmp_path):
    places = SyntheticPlaces(two_page_rows)
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, first = start_partial_checkup(client)
        original_revision, original_hash = first["revision"], first["trace"]["resultHash"]
        spent_before, dispatched_before = (app.state.quota.ledger.spent("place"), len(places.sent))

        view = submit_retry(client, task_id, "retry-1", maxPoiRequests=120)
        done = finish_retry(client, task_id, view["retryId"])
        assert done["status"] == "completed", done
        # 这一轮真的发了请求（不是全缓存重放），而且账本与派发数对得上。
        assert done["networkRequests"] > 0
        assert len(places.sent) - dispatched_before == done["networkRequests"]
        assert app.state.quota.ledger.spent("place") == spent_before + done["networkRequests"]

        after = document(client, task_id)
        assert after["revision"] > original_revision
        assert after["stage"] == "reporting"
        assert after["completion"]["roundPoiLimit"] == 120
        assert after["completion"]["roundNumber"] == 2
        assert after["completion"]["roundPoiRequests"] == done["networkRequests"]
        assert after["completion"]["cumulativePoiRequests"] == 60 + done["networkRequests"]
        assert after["completion"]["routeRequests"] == first["completion"]["routeRequests"]
        # 这一版是"重试发布的"，不是"离线重算发布的"：后者不花钱，前者花了。
        assert after["trace"]["retried"]["fromRevision"] == original_revision
        assert after["trace"]["retried"]["networkRequests"] == done["networkRequests"]
        assert after["trace"]["recomputed"] is None
        # 缺口补上了，而且写在**这一份报告**里。
        assert after["facilities"]["queryStatus"] == "completed"
        assert after["facilities"]["queryAreaCoverage"]["status"] == "met"

        # 原修订按它的内容原样留着：重试不许回头改写已经发布的证据。
        kept = app.state.checkups.store.revision(task_id, original_revision)
        assert kept["result_hash"] == original_hash
        assert kept["snapshot"]["facilities"]["queryStatus"] == "partial"
        assert kept["snapshot"]["facilities"]["queryAreaCoverage"]["status"] == "unmet"


def test_a_retry_replays_on_the_same_id_and_conflicts_on_another_budget(tmp_path):
    places = SyntheticPlaces(two_page_rows)
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _first = start_partial_checkup(client)
        view = submit_retry(client, task_id, "retry-1", maxPoiRequests=120)
        finish_retry(client, task_id, view["retryId"])
        # 重试之后这次体检已经查完，所以接下来的幂等断言正是在证明：
        # "取回原记录"发生在"无需重试"之前。
        dispatched = len(places.sent)
        spent = app.state.quota.ledger.spent("place")

        again = submit_retry(client, task_id, "retry-1", maxPoiRequests=120)
        assert again["retryId"] == view["retryId"]
        assert len(places.sent) == dispatched
        assert app.state.quota.ledger.spent("place") == spent
        assert [item["retryId"] for item in
                client.get(RETRIES.format(task=task_id)).json()] == [view["retryId"]]

        conflict = post_retry(client, task_id, "retry-1", maxPoiRequests=240)
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["code"] == "checkup_retry_request_id_conflict"


def test_a_finished_checkup_has_nothing_to_retry(tmp_path):
    """稀疏数据一次就查完：重试的前提不成立，就要具名拒绝，而不是白花一轮额度。"""
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = run(client, body(facilities={"categories": CORE_MAJORS,
                                                     "maxPoiRequests": 60}))
        group = document(client, task_id)["facilities"]
        assert group["queryStatus"] == "completed"
        assert group["queryAreaCoverage"]["status"] == "met"
        refused = post_retry(client, task_id, "retry-finished")
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "checkup_retry_not_needed"
        assert app.state.checkups.retries(task_id) == []


def test_a_requested_revision_is_the_one_served(tmp_path):
    """显式要哪一版就给哪一版，缺那一版就具名拒绝。

    客户端在重试结束后要按**指定的**修订读取结果与图层（否则图上画的是上一版的灰区、
    面板里写着这一版的面积），而结果接口曾经根本不看 `?revision=`：想要旧版的人会拿到
    最新版，然后因为响应里的版本号与请求不符被判成"契约异常"。
    """
    places = SyntheticPlaces(two_page_rows)
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, first = start_partial_checkup(client)
        original = first["revision"]
        view = submit_retry(client, task_id, "retry-1", maxPoiRequests=120)
        finish_retry(client, task_id, view["retryId"])
        assert document(client, task_id)["revision"] > original

        kept = client.get(f"/api/v2/checkups/{task_id}/result?revision={original}")
        assert kept.status_code == 200, kept.text
        assert kept.json()["revision"] == original
        assert kept.json()["facilities"]["queryStatus"] == "partial"
        layer = client.get(f"/api/v2/checkups/{task_id}/layers/facilities?revision={original}")
        assert layer.status_code == 200, layer.text
        assert layer.json()["revision"] == original

        missing = client.get(f"/api/v2/checkups/{task_id}/result?revision={original + 99}")
        assert missing.status_code == 409, missing.text
        assert missing.json()["code"] == "checkup_revision_not_found"


def test_a_retry_cannot_change_the_categories_it_is_continuing(tmp_path):
    """类别不在重试请求里给。

    ``CheckupModel`` 是 ``extra="forbid"``，所以"顺手换个类别"在这个接口上**根本表达
    不出来** —— 这比在服务端判一句"类别不一致就拒绝"更早一步，也更难绕过。
    """
    places = SyntheticPlaces(two_page_rows)
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _first = start_partial_checkup(client)
        refused = post_retry(client, task_id, "retry-1", categories=["finance"])
        assert refused.status_code == 422, refused.text
        # 被拒绝的请求不占额度、不排队、不留行：标识随后照样可以用。
        assert app.state.checkups.retries(task_id) == []
        view = submit_retry(client, task_id, "retry-1", maxPoiRequests=120)
        done = finish_retry(client, task_id, view["retryId"])
        assert done["status"] == "completed"
        after = document(client, task_id)
        assert sorted(after["facilities"]["countsByCategory"]) == sorted(CORE_MAJORS)
