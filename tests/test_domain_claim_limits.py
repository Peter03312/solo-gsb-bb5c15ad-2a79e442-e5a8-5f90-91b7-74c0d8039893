"""按终点邮箱域名的同时在途领取数上限（/deliveries/claim-limits + 领取联动）：

管理员可设置、删除并查询域名的正整数上限；域名大小写归一，非法域名或非正上限
拒绝且配置不变，未配置域不受限。领取仍按既有到期顺序和请求上限进行；某域满额时
跳过其任务继续领取其他域；只有未完成、未过期且未被更新回执取代的租约占用名额。
租约完成、到期或被既有回执抢先写入后释放占用；调低上限不撤销已发令牌，但占用
降到新上限以下前不再领取。并发领取与配置变更经 BEGIN IMMEDIATE 串行化，不超额。
既有回执、令牌完成、判定与报表响应保持兼容。
"""
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

# 必须在导入 app 之前设置，config 在导入时设置
os.environ.setdefault("ROUTER_DB", os.path.join(tempfile.mkdtemp(prefix="router-test-"), "test.db"))

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture()
def client():
    db.init_db()
    conn = db.connect()
    conn.executescript(
        "DELETE FROM endpoint_leases;"
        "DELETE FROM delivery_attempts;"
        "DELETE FROM domain_claim_limits;"
        "DELETE FROM draft_rules;"
        "DELETE FROM delivery_policy;"
        "DELETE FROM delivery_decisions;"
        "DELETE FROM circuit_breaker_settings;"
        "DELETE FROM circuit_breaks;"
        "DELETE FROM version_rules;"
        "DELETE FROM versions;"
        "DELETE FROM sqlite_sequence WHERE name = 'versions';"
        "DELETE FROM draft_revisions;"
        "INSERT INTO draft_revisions (revision, rules, rule_count, created_at)"
        " VALUES (0, '[]', 0, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));"
        "UPDATE draft_meta SET revision = 0 WHERE id = 1;"
    )
    conn.close()
    with TestClient(app) as c:
        yield c


RULES = [
    {
        "match_type": "exact",
        "match_key": "multi@corp.example.com",
        "action": "route",
        "targets": [
            {"kind": "mailbox", "value": "alice@mail.example.com"},
            {"kind": "mailbox", "value": "bob@mail.example.com"},
        ],
    },
    {
        "match_type": "exact",
        "match_key": "single@corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "carol@mail.example.com"}],
    },
    {
        "match_type": "exact",
        "match_key": "extra@corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "erin@mail.example.com"}],
    },
    {
        "match_type": "exact",
        "match_key": "peer@corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "dana@other.example.net"}],
    },
    {
        "match_type": "exact",
        "match_key": "blocked@corp.example.com",
        "action": "reject",
        "targets": [],
    },
]


def setup(client):
    assert client.put("/draft", json={"revision": 0, "rules": RULES}).status_code == 200
    assert client.post("/publish", json={"revision": 1}).status_code == 200
    assert client.put("/policy", json={
        "revision": 0, "baseline_version": 1, "candidate_version": 1, "candidate_percent": 100,
    }).status_code == 200


def submit(client, delivery_id, address="multi@corp.example.com"):
    resp = client.post("/deliveries", json={"delivery_id": delivery_id, "address": address})
    assert resp.status_code == 200, resp.text
    return resp.json()


def claim(client, max_claims=100, lease_seconds=300):
    return client.post("/deliveries/claims", json={
        "max_claims": max_claims, "lease_seconds": lease_seconds,
    })


def complete(client, token, result):
    return client.post(f"/deliveries/claims/{token}/complete", json={"result": result})


def put_limit(client, domain, max_claims):
    return client.put(f"/deliveries/claim-limits/{domain}", json={"max_claims": max_claims})


def task_pairs(client, **kw):
    return {(t["delivery_id"], t["mailbox"]) for t in claim(client, **kw).json()["tasks"]}


def active_count_by_domain():
    """当前占用名额的有效租约（未完成、未过期且未被更新回执取代）按域名计数。"""
    conn = db.connect()
    rows = conn.execute(
        """
        SELECT domain_of(l.mailbox) AS domain, COUNT(*) AS n
        FROM endpoint_leases l
        WHERE l.completed_at IS NULL
          AND l.lease_expires_at > strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
          AND NOT EXISTS (
                SELECT 1 FROM delivery_attempts a
                WHERE a.delivery_id = l.delivery_id AND a.mailbox = l.mailbox
                  AND a.attempt_no >= l.attempt_no)
        GROUP BY domain_of(l.mailbox)
        """
    ).fetchall()
    conn.close()
    return {r["domain"]: r["n"] for r in rows}


def expire_all_leases():
    conn = db.connect()
    conn.execute(
        "UPDATE endpoint_leases SET lease_expires_at = "
        "strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', '-1 second')"
    )
    conn.close()


# ---------- 管理员配置：设置 / 查询 / 删除 / 归一 / 非法入参 ----------

def test_list_limits_empty_by_default(client):
    setup(client)
    resp = client.get("/deliveries/claim-limits")
    assert resp.status_code == 200
    assert resp.json() == {"limits": []}


def test_put_get_list_limit_shape(client):
    setup(client)
    resp = put_limit(client, "mail.example.com", 2)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"domain", "max_claims", "updated_at"}
    assert body["domain"] == "mail.example.com" and body["max_claims"] == 2 and body["updated_at"]

    got = client.get("/deliveries/claim-limits/mail.example.com")
    assert got.status_code == 200 and got.json() == body

    assert client.get("/deliveries/claim-limits").json() == {"limits": [body]}


def test_domain_is_case_insensitive_and_trimmed(client):
    setup(client)
    resp = put_limit(client, "MAIL.Example.COM ", 1)
    assert resp.status_code == 200
    assert resp.json()["domain"] == "mail.example.com"
    # 任意大小写 / 带空白查询都命中同一份配置
    got = client.get("/deliveries/claim-limits/%20Mail.Example.COM%20")
    assert got.status_code == 200 and got.json()["domain"] == "mail.example.com"
    assert client.get("/deliveries/claim-limits/MAIL.EXAMPLE.COM").json()["max_claims"] == 1
    listed = client.get("/deliveries/claim-limits").json()["limits"]
    assert [r["domain"] for r in listed] == ["mail.example.com"]


def test_put_overwrites_existing_limit(client):
    setup(client)
    assert put_limit(client, "mail.example.com", 5).json()["max_claims"] == 5
    resp = put_limit(client, "mail.example.com", 1)
    assert resp.status_code == 200 and resp.json()["max_claims"] == 1
    assert client.get("/deliveries/claim-limits/mail.example.com").json()["max_claims"] == 1
    assert len(client.get("/deliveries/claim-limits").json()["limits"]) == 1


@pytest.mark.parametrize("bad_domain", [
    "not-a-domain", "example..com", "-bad.example.com", "bad-.example.com",
    "a@b.example.com", "ex*ample.com", ".com", "foo@bar@example.com",
])
def test_invalid_domain_rejected_on_put_get_delete(client, bad_domain):
    setup(client)
    assert put_limit(client, bad_domain, 1).status_code == 422
    assert client.get(f"/deliveries/claim-limits/{bad_domain}").status_code == 422
    assert client.delete(f"/deliveries/claim-limits/{bad_domain}").status_code == 422
    # 非法请求不改变配置
    assert client.get("/deliveries/claim-limits").json() == {"limits": []}
    got = client.put(
        f"/deliveries/claim-limits/{bad_domain}", json={"max_claims": 1}
    ).json()["detail"]
    assert got["error"] == "invalid_domain"


@pytest.mark.parametrize("bad_limit", [0, -1, -100])
def test_non_positive_limit_rejected_and_unchanged(client, bad_limit):
    setup(client)
    assert put_limit(client, "mail.example.com", 2).status_code == 200
    resp = client.put(
        "/deliveries/claim-limits/mail.example.com", json={"max_claims": bad_limit}
    )
    assert resp.status_code == 422
    # 原配置保持不变
    assert client.get("/deliveries/claim-limits/mail.example.com").json()["max_claims"] == 2


def test_get_unconfigured_domain_returns_404(client):
    setup(client)
    resp = client.get("/deliveries/claim-limits/mail.example.com")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "domain_claim_limit_not_found"
    assert resp.json()["detail"]["domain"] == "mail.example.com"


def test_delete_limit_is_idempotent_and_unrestricts(client):
    setup(client)
    assert put_limit(client, "mail.example.com", 1).status_code == 200
    resp = client.delete("/deliveries/claim-limits/MAIL.EXAMPLE.COM")
    assert resp.status_code == 200 and resp.json() == {"domain": "mail.example.com", "deleted": True}
    assert client.get("/deliveries/claim-limits/mail.example.com").status_code == 404
    # 删除未配置的域同样 200（幂等）
    again = client.delete("/deliveries/claim-limits/mail.example.com")
    assert again.status_code == 200 and again.json() == {"domain": "mail.example.com", "deleted": True}


# ---------- 领取联动：未配置域不受限 / 满额跳过 / 不超请求上限 ----------

def test_unconfigured_domains_are_unlimited(client):
    setup(client)
    submit(client, "msg-1")                                   # alice, bob @mail
    submit(client, "msg-2", "single@corp.example.com")       # carol @mail
    submit(client, "msg-3", "peer@corp.example.com")         # dana @other
    pairs = task_pairs(client)
    assert pairs == {
        ("msg-1", "alice@mail.example.com"),
        ("msg-1", "bob@mail.example.com"),
        ("msg-2", "carol@mail.example.com"),
        ("msg-3", "dana@other.example.net"),
    }


def test_full_domain_skipped_but_other_domains_continue(client):
    setup(client)
    assert put_limit(client, "mail.example.com", 1).status_code == 200
    # other.example.net 未配置：不受限
    submit(client, "msg-1")                                   # alice, bob @mail
    submit(client, "msg-2", "single@corp.example.com")       # carol @mail
    submit(client, "msg-net", "peer@corp.example.com")       # dana @other
    # 稳定顺序：msg-1/alice（mail，取，占用 1）→ msg-1/bob（mail，满，跳过）
    # → msg-2/carol（mail，满，跳过）→ msg-net/dana（other，未配置，取）
    pairs = task_pairs(client, max_claims=10)
    assert pairs == {
        ("msg-1", "alice@mail.example.com"),
        ("msg-net", "dana@other.example.net"),
    }
    # 第二次领取：mail 仍满额，other 也没有剩余任务 → 空
    assert claim(client).json()["tasks"] == []
    assert active_count_by_domain() == {"mail.example.com": 1, "other.example.net": 1}


def test_skipped_full_domain_does_not_consume_global_max_claims(client):
    # 请求上限只统计实际领取项：满额域被跳过不应挤掉其他可领项
    setup(client)
    assert put_limit(client, "mail.example.com", 1).status_code == 200
    submit(client, "msg-1")                            # alice, bob @mail
    submit(client, "msg-net", "peer@corp.example.com")  # dana @other
    tasks = claim(client, max_claims=2).json()["tasks"]
    assert {t["mailbox"] for t in tasks} == {"alice@mail.example.com", "dana@other.example.net"}


def test_global_request_cap_still_applies(client):
    setup(client)
    assert put_limit(client, "mail.example.com", 10).status_code == 200
    submit(client, "msg-1")                            # alice, bob
    submit(client, "msg-2", "single@corp.example.com")  # carol
    tasks = claim(client, max_claims=2).json()["tasks"]
    assert len(tasks) == 2  # 域名上限很大，但请求上限只有 2


# ---------- 名额释放：完成 / 到期 / 被既有回执抢先 ----------

def test_completing_lease_releases_domain_slot(client):
    setup(client)
    assert put_limit(client, "mail.example.com", 1).status_code == 200
    submit(client, "msg-1")  # alice, bob
    first = claim(client).json()["tasks"]
    assert [(t["delivery_id"], t["mailbox"]) for t in first] == [
        ("msg-1", "alice@mail.example.com")
    ]
    token = first[0]["token"]
    assert complete(client, token, "delivered").status_code == 200
    # alice 终结且不再入列；bob 的名额已释放 → 可领取
    pairs = task_pairs(client)
    assert pairs == {("msg-1", "bob@mail.example.com")}


def test_expired_lease_releases_domain_slot_and_token_rotates(client):
    setup(client)
    assert put_limit(client, "mail.example.com", 1).status_code == 200
    submit(client, "msg-1")
    old_token = claim(client).json()["tasks"][0]["token"]
    expire_all_leases()
    # 到期后占用归零：同一终点可重领（新令牌），不超额
    tasks = claim(client).json()["tasks"]
    assert len(tasks) == 1 and tasks[0]["token"] != old_token
    assert active_count_by_domain() == {"mail.example.com": 1}
    assert complete(client, old_token, "delivered").status_code == 409
    assert complete(client, tasks[0]["token"], "delivered").status_code == 200


def test_superseded_lease_does_not_occupy_slot(client):
    setup(client)
    assert put_limit(client, "mail.example.com", 1).status_code == 200
    submit(client, "msg-1")  # alice, bob
    token = claim(client).json()["tasks"][0]["token"]  # alice，占用 mail 域唯一名额
    # 既有回执接口抢先为 alice 记录了不早于租约序号的回执
    resp = client.post("/deliveries/msg-1/receipts", json={
        "mailbox": "alice@mail.example.com", "attempt_no": 1, "result": "delivered",
    })
    assert resp.status_code == 200
    # 旧令牌提交 → superseded，不覆盖
    conflict = complete(client, token, "temporary_failure")
    assert conflict.status_code == 409 and conflict.json()["detail"]["reason"] == "superseded"
    # 被抢先的租约不再占名额：bob 可领取（若仍计入占用，cap=1 会阻止 bob）
    pairs = task_pairs(client)
    assert pairs == {("msg-1", "bob@mail.example.com")}
    assert active_count_by_domain() == {"mail.example.com": 1}  # 只有 bob 的新租约占用


# ---------- 调低 / 调高 / 删除上限的语义 ----------

def test_lowering_limit_keeps_tokens_but_blocks_new_claims(client):
    setup(client)
    assert put_limit(client, "mail.example.com", 3).status_code == 200
    submit(client, "msg-1")                            # alice, bob
    submit(client, "msg-2", "single@corp.example.com")  # carol
    submit(client, "msg-3", "extra@corp.example.com")   # erin
    first = claim(client, max_claims=10).json()["tasks"]
    assert len(first) == 3  # cap 3：erin 被挡在外面
    by_mailbox = {t["mailbox"]: t["token"] for t in first}

    # 调低到 1：不撤销已发令牌，三个令牌仍然有效
    assert put_limit(client, "mail.example.com", 1).status_code == 200
    assert claim(client).json()["tasks"] == []  # 占用 3 ≥ 1：不再领取（erin 也不行）
    for result, mailbox in [
        ("delivered", "alice@mail.example.com"),
        ("temporary_failure", "bob@mail.example.com"),
        ("delivered", "carol@mail.example.com"),
    ]:
        assert complete(client, by_mailbox[mailbox], result).status_code == 200
    # 占用降到 0（alice/carol 终结，bob 租约已结束并在退避）→ erin 可领取
    pairs = task_pairs(client)
    assert pairs == {("msg-3", "erin@mail.example.com")}


def test_raising_limit_allows_more_claims(client):
    setup(client)
    assert put_limit(client, "mail.example.com", 1).status_code == 200
    submit(client, "msg-1")  # alice, bob
    assert len(claim(client).json()["tasks"]) == 1
    assert put_limit(client, "mail.example.com", 2).status_code == 200
    pairs = task_pairs(client)
    assert pairs == {("msg-1", "bob@mail.example.com")}


def test_deleting_limit_unrestricts(client):
    setup(client)
    assert put_limit(client, "mail.example.com", 1).status_code == 200
    submit(client, "msg-1")
    assert len(claim(client).json()["tasks"]) == 1
    assert client.delete("/deliveries/claim-limits/mail.example.com").status_code == 200
    pairs = task_pairs(client)
    assert pairs == {("msg-1", "bob@mail.example.com")}


# ---------- 并发：领取与配置变更原子核对，不超额 ----------

def test_concurrent_claims_never_exceed_domain_cap(client):
    setup(client)
    assert put_limit(client, "mail.example.com", 1).status_code == 200
    for i in range(4):
        submit(client, f"msg-{i}", "multi@corp.example.com")  # 8 个 mail 域任务
    assigned: list[tuple[str, str]] = []

    def claim_worker(_):
        resp = claim(client, max_claims=1)
        assigned.extend((t["delivery_id"], t["mailbox"]) for t in resp.json()["tasks"])
        return resp.status_code

    def rewrite_limit(_):  # 并发反复重设同一上限，只制造写锁竞争
        return put_limit(client, "mail.example.com", 1).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        claim_codes = list(pool.map(claim_worker, range(12)))
        limit_codes = list(pool.map(rewrite_limit, range(8)))
    assert set(claim_codes) == {200}
    assert set(limit_codes) == {200}
    # cap 恒为 1：全部并发领取加起来恰好分配 1 个，且不重复
    assert len(assigned) == 1
    assert active_count_by_domain() == {"mail.example.com": 1}


def test_concurrent_claims_and_limit_change_never_over_allocate(client):
    setup(client)
    assert put_limit(client, "mail.example.com", 1).status_code == 200
    submit(client, "msg-1")  # alice, bob 两个同域任务

    def claim_worker(_):
        return [(t["delivery_id"], t["mailbox"]) for t in claim(client, max_claims=1).json()["tasks"]]

    with ThreadPoolExecutor(max_workers=4) as pool:
        claim_futs = [pool.submit(claim_worker, i) for i in range(6)]
        # 并发放宽到 2：任何提交时刻核对到的上限要么是 1 要么是 2
        put_fut = pool.submit(put_limit, client, "mail.example.com", 2)
        results = [f.result() for f in claim_futs]
        assert put_fut.result().status_code == 200
    assigned = [p for seq in results for p in seq]
    assert len(assigned) <= 2  # 绝不超额
    assert len(set(assigned)) == len(assigned)  # 不重复分配
    assert active_count_by_domain()["mail.example.com"] <= 2


# ---------- 兼容性：回执 / 判定 / 报表不受域名上限影响 ----------

def test_limits_and_claims_keep_receipt_decision_report_compatible(client):
    setup(client)
    assert put_limit(client, "mail.example.com", 1).status_code == 200
    decision = submit(client, "msg-1")
    token = claim(client).json()["tasks"][0]["token"]
    assert complete(client, token, "temporary_failure").status_code == 200
    # 既有回执查询：形状 / 汇总规则不变
    summary = client.get("/deliveries/msg-1/receipts").json()
    assert summary["status"] == "pending"
    assert summary["endpoints"][0]["attempts"][0]["result"] == "temporary_failure"
    # 判定查询与同编号重放不变
    stored = client.get("/deliveries/msg-1").json()
    assert stored["endpoints"] == decision["endpoints"]
    replay = client.post("/deliveries", json={
        "delivery_id": "msg-1", "address": " MULTI@CORP.EXAMPLE.COM ",
    })
    assert replay.status_code == 200 and replay.json() == decision
    # 报表仍只统计判定，不受租约 / 上限影响
    report = client.get(
        "/deliveries/report",
        params={"start": "2026-09-01T00:00:00+00:00", "end": "2026-11-01T00:00:00+00:00"},
    )
    assert report.status_code == 200
    assert report.json()["total"] == 1
