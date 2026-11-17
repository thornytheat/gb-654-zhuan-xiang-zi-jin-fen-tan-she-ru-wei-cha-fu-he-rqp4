"""M7 跨批次尾差结算台账。

与 M1-M6 的 runs 表共库但表独立；所有写入在单个 ``BEGIN IMMEDIATE`` 事务内完成，
裁决与欠额同生共效：提交中断则整体回滚，不暴露半笔结算。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.errors import DomainError
from app.modules.m7_settlement import core

SCHEMA = """
CREATE TABLE IF NOT EXISTS m7_scope (
    fund_year TEXT NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (fund_year)
);
CREATE TABLE IF NOT EXISTS m7_deficit (
    fund_year TEXT NOT NULL,
    project_id TEXT NOT NULL,
    balance TEXT NOT NULL,
    updated_batch TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (fund_year, project_id)
);
CREATE TABLE IF NOT EXISTS m7_batch (
    fund_year TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    input_payload TEXT NOT NULL,
    verdict TEXT NOT NULL,
    version_before INTEGER NOT NULL,
    version_after INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (fund_year, batch_id)
);
CREATE TABLE IF NOT EXISTS m7_batch_line (
    fund_year TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    amount TEXT NOT NULL,
    cap TEXT NOT NULL,
    deficit_before TEXT NOT NULL,
    deficit_delta TEXT NOT NULL,
    deficit_after TEXT NOT NULL,
    reason TEXT NOT NULL,
    parked INTEGER NOT NULL,
    PRIMARY KEY (fund_year, batch_id, project_id)
);
CREATE TABLE IF NOT EXISTS m7_journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_year TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    balance_before TEXT NOT NULL,
    delta TEXT NOT NULL,
    balance_after TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class SettlementStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    # ---------- 只读 ----------
    def get_version(self, db: sqlite3.Connection, fund_year: str) -> int:
        row = db.execute("SELECT version FROM m7_scope WHERE fund_year=?", (fund_year,)).fetchone()
        return int(row[0]) if row else 0

    def get_deficits(self, db: sqlite3.Connection, fund_year: str) -> dict[str, str]:
        rows = db.execute(
            "SELECT project_id, balance FROM m7_deficit WHERE fund_year=?", (fund_year,)
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def get_batch(self, fund_year: str, batch_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT verdict FROM m7_batch WHERE fund_year=? AND batch_id=?",
                (fund_year, batch_id),
            ).fetchone()
        if not row:
            return None
        return json.loads(row[0])

    def list_batches(self, fund_year: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT batch_id, digest, version_before, version_after, created_at, verdict "
                "FROM m7_batch WHERE fund_year=? ORDER BY rowid",
                (fund_year,),
            ).fetchall()
        out = []
        for batch_id, digest, vb, va, created_at, verdict in rows:
            v = json.loads(verdict)
            out.append({
                "fund_year": fund_year,
                "batch_id": batch_id,
                "digest": digest,
                "version_before": vb,
                "version_after": va,
                "created_at": created_at,
                "allocated_total": v["allocated_total"],
                "unallocated": v["unallocated"],
                "reason": v["reason"],
            })
        return out

    def readback(self, fund_year: str, batch_id: str) -> dict:
        """回读原批次金额与结转前后欠额；已结算批次永远还原原裁决。"""
        with self.connect() as db:
            batch = db.execute(
                "SELECT digest, version_before, version_after, created_at, verdict "
                "FROM m7_batch WHERE fund_year=? AND batch_id=?",
                (fund_year, batch_id),
            ).fetchone()
            if not batch:
                raise DomainError("BATCH_NOT_FOUND", f"批次不存在: {fund_year}/{batch_id}")
            lines = db.execute(
                "SELECT project_id, amount, cap, deficit_before, deficit_delta, deficit_after, reason, parked "
                "FROM m7_batch_line WHERE fund_year=? AND batch_id=? ORDER BY project_id",
                (fund_year, batch_id),
            ).fetchall()
        digest, vb, va, created_at, verdict_raw = batch
        verdict = json.loads(verdict_raw)
        return {
            "status": "READY",
            "replayed": True,
            "fund_year": fund_year,
            "batch_id": batch_id,
            "digest": digest,
            "version_before": vb,
            "version_after": va,
            "created_at": created_at,
            "amounts": verdict["amounts"],
            "allocated_total": verdict["allocated_total"],
            "approved_total": verdict["approved_total"],
            "unallocated": verdict["unallocated"],
            "lines": [
                {
                    "project_id": pid,
                    "amount": amount,
                    "cap": cap,
                    "deficit_before": before,
                    "deficit_delta": delta,
                    "deficit_after": after,
                    "reason": reason,
                    "parked": bool(parked),
                }
                for pid, amount, cap, before, delta, after, reason, parked in lines
            ],
            "reason": verdict["reason"],
        }

    # ---------- 预览（不占欠额、不写库）----------
    def preview(self, payload: dict) -> dict:
        fund_year = str(payload.get("fund_year", "")).strip()
        with self.connect() as db:
            version = self.get_version(db, fund_year)
            deficits = self.get_deficits(db, fund_year)
            existing = db.execute(
                "SELECT verdict FROM m7_batch WHERE fund_year=? AND batch_id=?",
                (fund_year, str(payload.get("batch_id", "")).strip()),
            ).fetchone()
        if existing:
            verdict = json.loads(existing[0])
            verdict["replayed"] = True
            return verdict
        return core.build(payload, deficits, version, committed=False)

    # ---------- 结算（乐观版本 + 排他锁 + 幂等恢复）----------
    def commit(self, payload: dict, expected_version: int) -> dict:
        fund_year = str(payload.get("fund_year", "")).strip()
        batch_id = str(payload.get("batch_id", "")).strip()
        db = self.connect()
        try:
            # 排他锁先行：并发结算串行化，后者看到新版本即拒绝过期版本。
            db.execute("BEGIN IMMEDIATE")

            stored = db.execute(
                "SELECT verdict FROM m7_batch WHERE fund_year=? AND batch_id=?",
                (fund_year, batch_id),
            ).fetchone()
            if stored:
                # 中断恢复 / 重试：按已存裁决原样返回，不重复结转。
                db.commit()
                verdict = json.loads(stored[0])
                verdict["replayed"] = True
                return verdict

            version = self.get_version(db, fund_year)
            if int(expected_version) != version:
                db.rollback()
                raise DomainError(
                    "STALE_VERSION",
                    f"期望版本 {expected_version} 与当前版本 {version} 不一致",
                )

            deficits = self.get_deficits(db, fund_year)
            verdict = core.build(payload, deficits, version, committed=True)
            now = datetime.now(timezone.utc).isoformat()
            new_version = version + 1

            db.execute(
                "INSERT INTO m7_scope(fund_year, version) VALUES(?, ?) "
                "ON CONFLICT(fund_year) DO UPDATE SET version=excluded.version",
                (fund_year, new_version),
            )
            db.execute(
                "INSERT INTO m7_batch(fund_year, batch_id, digest, input_payload, verdict, "
                "version_before, version_after, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    fund_year, batch_id, verdict["digest"],
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(verdict, ensure_ascii=False, sort_keys=True),
                    version, new_version, now,
                ),
            )
            for line in verdict["lines"]:
                pid = line["project_id"]
                db.execute(
                    "INSERT INTO m7_batch_line(fund_year, batch_id, project_id, amount, cap, "
                    "deficit_before, deficit_delta, deficit_after, reason, parked) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        fund_year, batch_id, pid, line["amount"], line["cap"],
                        verdict["deficit_before"][pid], verdict["deficit_delta"][pid],
                        verdict["deficit_after"][pid], line["reason"], 1 if line["parked"] else 0,
                    ),
                )
                db.execute(
                    "INSERT INTO m7_deficit(fund_year, project_id, balance, updated_batch, updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(fund_year, project_id) DO UPDATE SET "
                    "balance=excluded.balance, updated_batch=excluded.updated_batch, updated_at=excluded.updated_at",
                    (fund_year, pid, verdict["deficit_after"][pid], batch_id, now),
                )
                db.execute(
                    "INSERT INTO m7_journal(fund_year, batch_id, project_id, balance_before, delta, "
                    "balance_after, reason, created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        fund_year, batch_id, pid,
                        verdict["deficit_before"][pid], verdict["deficit_delta"][pid],
                        verdict["deficit_after"][pid], line["reason"], now,
                    ),
                )
            db.commit()  # 裁决与欠额一起生效；此前任何异常都整体回滚。
            return verdict
        except DomainError:
            raise
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
