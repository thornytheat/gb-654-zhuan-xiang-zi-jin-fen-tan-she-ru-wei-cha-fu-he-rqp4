"""M7 跨批次尾差结算 —— 确定性裁决核心（纯函数，不落库、不接支付）。

规则要点：
- 先复用 M4 的迭代封顶分摊取得每项目精确份额 raw，再向分截底 base。
- 尾分按 ``(本批截底零头 - 已领尾分 + 同专项年度历史欠额)`` 降序发放，
  同分按项目编号升序，避免连续小批把尾分长期偏向编码靠前项目。
- 项目一旦达到当批封顶即退出尾分竞争；全量封顶时未发出的分留作批次未分配，
  欠额按精确份额与实发金额的差额照常累计（封顶项目的欠额暂存，不突破当批上限）。
- 欠额为有符号台账：正为历史少发、负为历史超发，超发会降低后续批次的竞争序。
- 输入行顺序不改变结果。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, localcontext

from app.errors import DomainError
from app.manifest import canonical_digest
from app.modules.m4_allocations.core import allocate as m4_allocate

CENT = Decimal("0.01")
ZERO = Decimal("0")
# 欠额是分以下的次级台账，统一保留 1e-8 元，保证跨批次累计确定可复算。
DEFICIT_QUANT = Decimal("0.00000001")

REASON_OK = "OK"
REASON_CAP = "CAP_LIMIT"
LINE_WEIGHT = "WEIGHT"
LINE_CAP = "CAP"
LINE_CAP_PARKED = "CAP_PARKED"

SAMPLE = {
    "fund_year": "2026",
    "batch_id": "B-001",
    "total": "10.00",
    "rows": [
        {"project_id": "A", "weight": "1", "cap": "10.00"},
        {"project_id": "B", "weight": "1", "cap": "10.00"},
        {"project_id": "C", "weight": "1", "cap": "10.00"},
    ],
}


def _decimal(value: str, code: str) -> Decimal:
    try:
        out = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, AttributeError):
        raise DomainError(code, f"非法数值: {value!r}")
    if not out.is_finite() or out < 0:
        raise DomainError(code, f"非法数值: {value!r}")
    return out


def _validate(payload: dict) -> tuple[str, str, Decimal, list[dict]]:
    fund_year = str(payload.get("fund_year", "")).strip()
    if not (fund_year.isdigit() and len(fund_year) == 4):
        raise DomainError("INVALID_FUND_YEAR", "专项年度须为 4 位数字，欠额仅在同一年度内结转")
    batch_id = str(payload.get("batch_id", "")).strip()
    if not batch_id:
        raise DomainError("INVALID_BATCH", "缺少批次编号 batch_id")
    total = _decimal(payload.get("total"), "INVALID_TOTAL")
    rows = payload.get("rows") or []
    if not isinstance(rows, list) or not rows:
        raise DomainError("EMPTY_BATCH", "批次至少包含一个项目")
    seen: set[str] = set()
    clean: list[dict] = []
    for row in rows:
        pid = str(row.get("project_id", "")).strip()
        if not pid:
            raise DomainError("INVALID_PROJECT", "项目编号为空")
        if pid in seen:
            raise DomainError("DUPLICATE_PROJECT", f"同批项目重复: {pid}")
        seen.add(pid)
        clean.append({
            "project_id": pid,
            "weight": _decimal(row.get("weight"), "INVALID_WEIGHT"),
            "cap": _decimal(row.get("cap"), "INVALID_CAP").quantize(CENT),
        })
    return fund_year, batch_id, total, clean


def _rank_key(pid: str, frac: Decimal, received: int, carried: Decimal) -> tuple[Decimal, str]:
    # 零头 + 历史欠额 - 本批已领尾分；取负实现降序，元组第二项实现编号升序破平。
    return (-(frac - received * CENT + carried), pid)


def _fixed(value: Decimal) -> str:
    """统一定点字符串，避免 Decimal 科学计数法（如 0E-8）进入台账与接口。"""
    return format(value.quantize(DEFICIT_QUANT), "f")


def build(payload: dict, deficits: dict | None, version: int, committed: bool) -> dict:
    """对单一批次出具裁决。deficits 为 ``{project_id: Decimal}`` 的同年度历史欠额。"""
    d = SAMPLE | (payload or {})
    fund_year, batch_id, total, rows = _validate(d)
    carried = {pid: Decimal(str(v)) for pid, v in (deficits or {}).items()}

    with localcontext() as ctx:
        ctx.prec = 40
        # M4：先迭代封顶再按有效权重分配，输出精确（分以下）份额与封顶余量。
        items = [{"id": r["project_id"], "weight": str(r["weight"]), "cap": str(r["cap"])} for r in rows]
        raw_map, leftover_raw = m4_allocate(total, items)
        leftover = leftover_raw.quantize(CENT, rounding=ROUND_HALF_UP)

        pids = sorted(r["project_id"] for r in rows)
        cap_by = {r["project_id"]: r["cap"] for r in rows}
        raw = {pid: raw_map[pid] for pid in pids}
        base = {pid: raw[pid].quantize(CENT, rounding=ROUND_DOWN) for pid in pids}
        frac = {pid: raw[pid] - base[pid] for pid in pids}

        # 待发尾分 = 可分配总额的分以下零头合计；M4 截断残差远小于半分，HALF_UP 归整。
        units = int(((sum(raw.values()) - sum(base.values())) / CENT).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

        received = {pid: 0 for pid in pids}
        blocked = {pid: False for pid in pids}
        unallocated = ZERO
        for _ in range(units):
            candidates = [
                pid for pid in pids
                if base[pid] + (received[pid] + 1) * CENT <= cap_by[pid]
            ]
            if not candidates:
                # 全部触及当批上限：尾分无法发放，记未分配并给在限项目挂暂存标记。
                unallocated += CENT
                for pid in pids:
                    blocked[pid] = True
                continue
            blocked_now = [pid for pid in pids if pid not in candidates]
            pick = min(candidates, key=lambda pid: _rank_key(pid, frac[pid], received[pid], carried.get(pid, ZERO)))
            received[pick] += 1
            for pid in blocked_now:
                blocked[pid] = True

        settled = {pid: base[pid] + received[pid] * CENT for pid in pids}
        delta = {pid: (raw[pid] - settled[pid]).quantize(DEFICIT_QUANT, rounding=ROUND_HALF_UP) for pid in pids}
        deficit_before = {pid: carried.get(pid, ZERO).quantize(DEFICIT_QUANT) for pid in pids}
        deficit_after = {pid: (deficit_before[pid] + delta[pid]).quantize(DEFICIT_QUANT) for pid in pids}

    lines = []
    for pid in pids:
        capped = settled[pid] >= cap_by[pid]
        if blocked[pid]:
            reason = LINE_CAP_PARKED
        elif capped:
            reason = LINE_CAP
        else:
            reason = LINE_WEIGHT
        lines.append({
            "project_id": pid,
            "raw_amount": str(raw[pid]),
            "base_amount": str(base[pid]),
            "tail_cents": received[pid],
            "amount": str(settled[pid]),
            "cap": str(cap_by[pid]),
            "capped": capped,
            "parked": blocked[pid],
            "reason": reason,
        })

    allocated_total = sum(settled.values())
    unallocated_total = leftover + unallocated
    digest = canonical_digest({
        "fund_year": fund_year,
        "batch_id": batch_id,
        "total": str(total),
        "rows": sorted(
            ({"project_id": r["project_id"], "weight": str(r["weight"]), "cap": str(r["cap"])} for r in rows),
            key=lambda r: r["project_id"],
        ),
    })
    return {
        "status": "READY",
        "committed": committed,
        "replayed": False,
        "fund_year": fund_year,
        "batch_id": batch_id,
        "input_count": len(rows),
        "valid_count": len(rows),
        "quarantine_count": 0,
        "digest": digest,
        "echo": {
            "fund_year": fund_year,
            "batch_id": batch_id,
            "total": str(total),
            "rows": [
                {"project_id": r["project_id"], "weight": str(r["weight"]), "cap": str(r["cap"])}
                for r in sorted(rows, key=lambda r: r["project_id"])
            ],
        },
        "version_before": int(version),
        "expected_version": int(version),
        "version_after": int(version) + 1 if committed else int(version),
        "amounts": {pid: str(settled[pid]) for pid in pids},
        "allocated_total": str(allocated_total),
        "approved_total": str(total),
        "unallocated": str(unallocated_total),
        "balanced": allocated_total + unallocated_total == total,
        "deficit_before": {pid: _fixed(deficit_before[pid]) for pid in pids},
        "deficit_delta": {pid: _fixed(delta[pid]) for pid in pids},
        "deficit_after": {pid: _fixed(deficit_after[pid]) for pid in pids},
        "lines": lines,
        "reason": REASON_OK if unallocated_total == 0 else REASON_CAP,
    }
