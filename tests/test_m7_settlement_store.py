"""M7 跨批次尾差结算：台账、并发与接口测试。"""
import threading

import pytest

from app.errors import DomainError
from app.settlement_store import SettlementStore

PAYLOAD_1 = {
    "fund_year": "2026", "batch_id": "B-001", "total": "10.00",
    "rows": [
        {"project_id": "A", "weight": "1", "cap": "10.00"},
        {"project_id": "B", "weight": "1", "cap": "10.00"},
        {"project_id": "C", "weight": "1", "cap": "10.00"},
    ],
}


def _payload(batch_id, fund_year="2026", total="10.00"):
    d = dict(PAYLOAD_1)
    d["batch_id"] = batch_id
    d["fund_year"] = fund_year
    d["total"] = total
    d["rows"] = [dict(r) for r in PAYLOAD_1["rows"]]
    return d


@pytest.fixture
def store(tmp_path):
    return SettlementStore(str(tmp_path / "m7.db"))


def test_preview_does_not_consume_deficit(store):
    before = store.preview(_payload("B-001"))
    again = store.preview(_payload("B-001"))
    assert before == again
    assert store.get_version(store.connect(), "2026") == 0
    assert store.get_deficits(store.connect(), "2026") == {}


def test_commit_persists_verdict_and_deficit_together(store):
    verdict = store.commit(_payload("B-001"), 0)
    assert verdict["committed"] is True
    with store.connect() as db:
        assert store.get_version(db, "2026") == 1
        deficits = store.get_deficits(db, "2026")
    assert set(deficits) == {"A", "B", "C"}
    read = store.readback("2026", "B-001")
    assert read["amounts"] == verdict["amounts"]
    assert read["lines"][0]["deficit_before"] == "0.00000000"
    assert read["lines"][0]["deficit_after"] == verdict["deficit_after"]["A"]


def test_concurrent_commit_rejects_stale_version(store):
    barrier = threading.Barrier(2)
    results: list[dict | Exception] = []

    def worker(seq, version):
        try:
            barrier.wait(timeout=10)
            results.append(store.commit(_payload(f"B-C{seq}"), version))
        except Exception as exc:  # noqa: BLE001
            results.append(exc)

    t1 = threading.Thread(target=worker, args=(1, 0))
    t2 = threading.Thread(target=worker, args=(2, 0))
    t1.start(); t2.start(); t1.join(); t2.join()
    ok = [r for r in results if isinstance(r, dict)]
    stale = [r for r in results if isinstance(r, DomainError)]
    assert len(ok) == 1 and len(stale) == 1
    assert stale[0].code == "STALE_VERSION"
    with store.connect() as db:
        assert store.get_version(db, "2026") == 1  # 仅一笔生效，无半笔


def test_retry_after_interruption_replays_without_double_carry(store):
    first = store.commit(_payload("B-001"), 0)
    replay = store.commit(_payload("B-001"), 0)  # 中断后重试：版本号已过期也按已存裁决恢复
    assert replay["replayed"] is True
    assert replay["amounts"] == first["amounts"]
    with store.connect() as db:
        assert store.get_version(db, "2026") == 1  # 不重复结转
        journal = db.execute("SELECT COUNT(*) FROM m7_journal").fetchone()[0]
    assert journal == 3  # 每项目一条流水，重试不追加


def test_failed_commit_rolls_back_everything(store):
    bad = _payload("B-001")
    bad["rows"][1]["project_id"] = "A"  # 重复项目，裁决阶段即失败
    with pytest.raises(DomainError):
        store.commit(bad, 0)
    with store.connect() as db:
        assert store.get_version(db, "2026") == 0
        assert db.execute("SELECT COUNT(*) FROM m7_batch").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM m7_deficit").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM m7_journal").fetchone()[0] == 0


def test_deficit_never_crosses_fund_year(store):
    store.commit(_payload("B-001", fund_year="2026"), 0)
    next_year = store.preview(_payload("B-001", fund_year="2027"))
    assert next_year["deficit_before"] == {"A": "0.00000000", "B": "0.00000000", "C": "0.00000000"}
    assert next_year["amounts"]["A"] == "3.34"


def test_settled_batch_recompute_restores_original_verdict(store):
    first = store.commit(_payload("B-001"), 0)
    store.commit(_payload("B-002"), 1)  # 后续批次改变欠额
    read = store.readback("2026", "B-001")
    # 套最新欠额会改旧账；回读必须还原原裁决与原结转前后欠额。
    assert read["amounts"] == first["amounts"]
    assert read["lines"][0]["deficit_after"] == first["deficit_after"]["A"]
    assert read["version_before"] == 0 and read["version_after"] == 1


def test_version_advances_per_commit_and_preview_sees_it(store):
    store.commit(_payload("B-001"), 0)
    preview = store.preview(_payload("B-002"))
    assert preview["version_before"] == 1
    store.commit(_payload("B-002"), 1)
    with store.connect() as db:
        assert store.get_version(db, "2026") == 2
    with pytest.raises(DomainError) as exc:
        store.commit(_payload("B-003"), 1)  # 过期版本
    assert exc.value.code == "STALE_VERSION"


def test_list_batches_and_ledger_readback(store):
    store.commit(_payload("B-001"), 0)
    batches = store.list_batches("2026")
    assert [b["batch_id"] for b in batches] == ["B-001"]
    assert batches[0]["reason"] == "OK"
    with store.connect() as db:
        ledger = {"version": store.get_version(db, "2026"), "deficits": store.get_deficits(db, "2026")}
    assert ledger["version"] == 1 and "A" in ledger["deficits"]


def test_readback_missing_batch(store):
    with pytest.raises(DomainError) as exc:
        store.readback("2026", "NOPE")
    assert exc.value.code == "BATCH_NOT_FOUND"


def test_full_flow_compensates_bias_across_batches(store):
    store.commit(_payload("B-001"), 0)
    second = store.commit(_payload("B-002"), 1)
    assert second["amounts"]["B"] == "3.34"  # 尾分补偿给上批少分者，不再固定 A


# ---------------- HTTP 接口 ----------------
def test_api_preview_commit_readback_flow(client):
    pv = client.post("/api/settlement/preview", json=_payload("B-001"))
    assert pv.status_code == 200
    assert pv.json()["committed"] is False

    missing = client.post("/api/settlement/commit", json=_payload("B-001"))
    assert missing.status_code == 422 and missing.json()["error"] == "MISSING_VERSION"

    commit = client.post("/api/settlement/commit", json={**_payload("B-001"), "expected_version": 0})
    assert commit.status_code == 200 and commit.json()["version_after"] == 1

    stale = client.post("/api/settlement/commit", json={**_payload("B-002"), "expected_version": 0})
    assert stale.status_code == 422 and stale.json()["error"] == "STALE_VERSION"

    read = client.get("/api/settlement/batch/2026/B-001")
    assert read.status_code == 200
    assert read.json()["amounts"] == commit.json()["amounts"]

    ledger = client.get("/api/settlement/ledger/2026").json()
    assert ledger["version"] == 1 and set(ledger["deficits"]) == {"A", "B", "C"}

    batches = client.get("/api/settlement/batches/2026").json()
    assert len(batches["batches"]) == 1


def test_api_concurrent_commits_one_wins(client):
    barrier = threading.Barrier(2)
    outcomes = []

    def call(batch):
        barrier.wait(timeout=10)
        resp = client.post("/api/settlement/commit", json={**_payload(batch), "expected_version": 0})
        outcomes.append(resp.status_code)

    t1 = threading.Thread(target=call, args=("B-X1",))
    t2 = threading.Thread(target=call, args=("B-X2",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(outcomes) == [200, 422]


def test_api_replay_is_idempotent(client):
    client.post("/api/settlement/commit", json={**_payload("B-001"), "expected_version": 0})
    again = client.post("/api/settlement/commit", json={**_payload("B-001"), "expected_version": 0})
    assert again.status_code == 200
    assert again.json()["replayed"] is True
    assert client.get("/api/settlement/ledger/2026").json()["version"] == 1


def test_settlement_page_renders(client):
    resp = client.get("/settlement")
    assert resp.status_code == 200 and "panel" in resp.text
