"""API 与 RFC 1982 算术的测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.rfc1982 import SERIAL_HALF, MAX_SERIAL, classify
from app.suggest import suggest_next_serial, SuggestionError
from datetime import date

TODAY = date(2026, 9, 18)
BASE = 2026091800  # YYYYMMDD00


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


# ---------- RFC 1982 分类 ----------

@pytest.mark.parametrize(
    "prev,cur,expected",
    [
        (1, 2, "advance"),
        (5, 5, "equal"),
        (2, 1, "regression"),
        (0, SERIAL_HALF, "incomparable"),
        (0, SERIAL_HALF - 1, "advance"),
        (0, SERIAL_HALF + 1, "regression"),
        (MAX_SERIAL, 0, "advance"),          # 回绕后仍算前进
        (MAX_SERIAL - 9, 10, "advance"),     # 回绕前进 20
        (10, MAX_SERIAL - 9, "regression"),  # 反向回绕即倒退
    ],
)
def test_classify(prev, cur, expected):
    assert classify(prev, cur) == expected


# ---------- 审计接口 ----------

def test_create_and_get_audit(client):
    serials = [BASE, BASE + 1, BASE + 1, BASE - 5]
    resp = client.post("/api/audits", json={"zone": "example.com", "serials": serials})
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == 1
    assert body["zone"] == "example.com"
    assert body["serials"] == serials

    relations = [item["relation"] for item in body["items"]]
    assert relations == ["initial", "advance", "equal", "regression"]
    risks = [item["slave_reject_risk"] for item in body["items"]]
    assert risks == [False, False, False, True]
    assert body["summary"]["risk_indexes"] == [3]
    assert body["summary"]["regression"] == 1

    got = client.get(f"/api/audits/{body['id']}")
    assert got.status_code == 200
    assert got.json() == body


def test_incomparable_flagged_as_risk(client):
    serials = [0, SERIAL_HALF]
    resp = client.post("/api/audits", json={"zone": "example.com", "serials": serials})
    assert resp.status_code == 201
    item = resp.json()["items"][1]
    assert item["relation"] == "incomparable"
    assert item["slave_reject_risk"] is True
    assert item["delta"] is None


def test_wraparound_sequence(client):
    serials = [MAX_SERIAL - 1, MAX_SERIAL, 0, 1]
    resp = client.post("/api/audits", json={"zone": "example.com.", "serials": serials})
    assert resp.status_code == 201
    relations = [i["relation"] for i in resp.json()["items"]]
    assert relations == ["initial", "advance", "advance", "advance"]


def test_get_missing_audit_404(client):
    assert client.get("/api/audits/999").status_code == 404


# ---------- 修复预演 ----------

def test_repair_plan_keeps_and_replaces(client):
    serials = [BASE, BASE + 1, BASE + 1, BASE - 5]
    created = client.post("/api/audits", json={"zone": "example.com", "serials": serials})
    audit_id = created.json()["id"]

    resp = client.get(f"/api/audits/{audit_id}/repair-plan")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == audit_id
    assert body["zone"] == "example.com"

    items = body["items"]
    assert [(i["index"], i["original"], i["effective"], i["action"]) for i in items] == [
        (0, BASE, BASE, "keep"),        # 首项不变
        (1, BASE + 1, BASE + 1, "keep"),  # 有效前进
        (2, BASE + 1, BASE + 2, "replace"),  # 相等 → 上一修复值 +1
        (3, BASE - 5, BASE + 3, "replace"),  # 相对修复后值倒退 → +1
    ]
    assert body["summary"] == {"total": 4, "replaced": 2}
    # 替换原因逐项返回
    assert "相等" in items[2]["reason"]
    assert "倒退" in items[3]["reason"]
    # 修复后序列相邻项均为有效前进
    effectives = [i["effective"] for i in items]
    assert all(classify(a, b) == "advance" for a, b in zip(effectives, effectives[1:]))


def test_repair_plan_wraparound(client):
    # 0 相对 MAX_SERIAL 是回绕前进（保留）；MAX_SERIAL 相对 0 是倒退（替换为 1）
    serials = [MAX_SERIAL, 0, MAX_SERIAL]
    audit_id = client.post(
        "/api/audits", json={"zone": "example.com", "serials": serials}
    ).json()["id"]

    body = client.get(f"/api/audits/{audit_id}/repair-plan").json()
    assert [(i["original"], i["effective"], i["action"]) for i in body["items"]] == [
        (MAX_SERIAL, MAX_SERIAL, "keep"),
        (0, 0, "keep"),
        (MAX_SERIAL, 1, "replace"),
    ]
    assert body["summary"]["replaced"] == 1


def test_repair_plan_replace_wraps_to_zero(client):
    # 上一修复后值为 2^32-1 且原值相等时，替换值按 32 位无符号回绕为 0
    serials = [MAX_SERIAL, MAX_SERIAL]
    audit_id = client.post(
        "/api/audits", json={"zone": "example.com", "serials": serials}
    ).json()["id"]

    items = client.get(f"/api/audits/{audit_id}/repair-plan").json()["items"]
    assert items[1]["effective"] == 0
    assert items[1]["action"] == "replace"
    assert classify(items[0]["effective"], items[1]["effective"]) == "advance"


def test_repair_plan_incomparable_replaced(client):
    serials = [0, SERIAL_HALF]
    audit_id = client.post(
        "/api/audits", json={"zone": "example.com", "serials": serials}
    ).json()["id"]

    items = client.get(f"/api/audits/{audit_id}/repair-plan").json()["items"]
    assert items[1]["action"] == "replace"
    assert items[1]["effective"] == 1
    assert "不可比较" in items[1]["reason"]


def test_repair_plan_all_advance_kept(client):
    serials = [MAX_SERIAL - 1, MAX_SERIAL, 0, 1]
    audit_id = client.post(
        "/api/audits", json={"zone": "example.com.", "serials": serials}
    ).json()["id"]

    body = client.get(f"/api/audits/{audit_id}/repair-plan").json()
    assert all(i["action"] == "keep" for i in body["items"])
    assert [i["effective"] for i in body["items"]] == serials
    assert body["summary"] == {"total": 4, "replaced": 0}


def test_repair_plan_single_serial(client):
    audit_id = client.post(
        "/api/audits", json={"zone": "example.com", "serials": [42]}
    ).json()["id"]

    body = client.get(f"/api/audits/{audit_id}/repair-plan").json()
    assert body["items"] == [
        {"index": 0, "original": 42, "effective": 42, "action": "keep",
         "reason": body["items"][0]["reason"]}
    ]
    assert body["summary"] == {"total": 1, "replaced": 0}


def test_repair_plan_not_persisted(client):
    serials = [BASE, BASE - 5]
    audit_id = client.post(
        "/api/audits", json={"zone": "example.com", "serials": serials}
    ).json()["id"]

    client.get(f"/api/audits/{audit_id}/repair-plan")
    # 原审计记录未被修改
    stored = client.get(f"/api/audits/{audit_id}").json()
    assert stored["serials"] == serials


def test_repair_plan_missing_audit_404(client):
    assert client.get("/api/audits/999/repair-plan").status_code == 404


# ---------- 下一序列号建议 ----------


def test_list_audits(client):
    client.post("/api/audits", json={"zone": "a.example", "serials": [1, 2]})
    client.post("/api/audits", json={"zone": "b.example", "serials": [2, 1]})
    resp = client.get("/api/audits")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    assert rows[0]["id"] == 2  # 倒序
    assert rows[0]["risk_count"] == 1
    assert rows[1]["risk_count"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"zone": "example.com", "serials": []},                    # 空列表
        {"zone": "example.com", "serials": [-1]},                  # 越界
        {"zone": "example.com", "serials": [MAX_SERIAL + 1]},      # 越界
        {"zone": "bad_zone!", "serials": [1]},                     # 非法区域名
        {"zone": "", "serials": [1]},                              # 空区域名
        {"zone": "example.com"},                                   # 缺字段
    ],
)
def test_invalid_payload_422(client, payload):
    assert client.post("/api/audits", json=payload).status_code == 422


# ---------- 下一序列号建议 ----------

def test_suggest_when_behind_base():
    assert suggest_next_serial(2026010100, TODAY) == BASE
    assert suggest_next_serial(0, TODAY) == BASE
    # 回绕场景：接近 2^32 上限时，今日基准仍是有效前进
    assert suggest_next_serial(MAX_SERIAL, TODAY) == BASE


def test_suggest_within_today():
    assert suggest_next_serial(BASE, TODAY) == BASE + 1
    assert suggest_next_serial(BASE + 50, TODAY) == BASE + 51
    assert suggest_next_serial(BASE + 98, TODAY) == BASE + 99


def test_suggest_exhausted():
    with pytest.raises(SuggestionError):
        suggest_next_serial(BASE + 99, TODAY)


def test_suggest_ahead_of_today():
    with pytest.raises(SuggestionError):
        suggest_next_serial(BASE + 100, TODAY)
    with pytest.raises(SuggestionError):
        suggest_next_serial(2099010100, TODAY)  # 未来日期


def test_suggest_half_interval_ambiguity():
    ambiguous = (BASE + SERIAL_HALF) % (MAX_SERIAL + 1)
    with pytest.raises(SuggestionError):
        suggest_next_serial(ambiguous, TODAY)


def test_next_serial_endpoint(client):
    resp = client.get("/api/next-serial", params={"current": BASE + 7, "today": "2026-09-18"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["next"] == BASE + 8
    assert body["date"] == "20260918"
    # 建议值确为有效前进
    assert classify(body["current"], body["next"]) == "advance"


def test_next_serial_endpoint_409(client):
    resp = client.get("/api/next-serial", params={"current": BASE + 99, "today": "2026-09-18"})
    assert resp.status_code == 409
    ambiguous = (BASE + SERIAL_HALF) % (MAX_SERIAL + 1)
    resp = client.get("/api/next-serial", params={"current": ambiguous, "today": "2026-09-18"})
    assert resp.status_code == 409


def test_next_serial_default_date(client):
    resp = client.get("/api/next-serial", params={"current": 1})
    assert resp.status_code == 200
    assert classify(1, resp.json()["next"]) == "advance"


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
