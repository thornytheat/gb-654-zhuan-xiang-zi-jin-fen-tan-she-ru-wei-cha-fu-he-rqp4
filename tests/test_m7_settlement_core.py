"""M7 跨批次尾差结算：确定性裁决核心测试。"""
from decimal import Decimal

import pytest

from app.errors import DomainError
from app.modules.m7_settlement.core import CENT, build


def _rows(n, cap="1000.00", weight="1"):
    return [{"project_id": chr(65 + i), "weight": weight, "cap": cap} for i in range(n)]


def test_conserves_total():
    out = build({"fund_year": "2026", "batch_id": "b1", "total": "10.00", "rows": _rows(3)}, None, 0, False)
    assert Decimal(out["allocated_total"]) + Decimal(out["unallocated"]) == Decimal("10.00")
    assert out["balanced"] is True


def test_first_batch_matches_largest_remainder_bias():
    # 无历史欠额时，首批评判与最大余额法一致：平票尾分落到编码靠前项目。
    out = build({"fund_year": "2026", "batch_id": "b1", "total": "10.00", "rows": _rows(3)}, None, 0, False)
    assert out["amounts"] == {"A": "3.34", "B": "3.33", "C": "3.33"}
    assert out["deficit_delta"]["A"] == "-0.00666667"


def test_second_batch_compensates_rounding_debt():
    first = build({"fund_year": "2026", "batch_id": "b1", "total": "10.00", "rows": _rows(3)}, None, 0, True)
    second = build({"fund_year": "2026", "batch_id": "b2", "total": "10.00", "rows": _rows(3)},
                   first["deficit_after"], 1, True)
    # A 上一批已拿尾分且欠额为负，本批尾分补偿给 B，不再长期偏向编码靠前项目。
    assert second["amounts"] == {"A": "3.33", "B": "3.34", "C": "3.33"}


def test_tail_rotates_across_batches_no_permanent_bias():
    deficits = None
    winners = []
    for i in range(6):
        out = build({"fund_year": "2026", "batch_id": f"b{i}", "total": "10.00", "rows": _rows(3)},
                    deficits, i, True)
        winners.append(next(pid for pid, line in enumerate(out["lines"]) if line["tail_cents"] == 1))
        deficits = out["deficit_after"]
    counts = {pid: winners.count(pid) for pid in set(winners)}
    assert max(counts.values()) - min(counts.values()) <= 1  # 六批各拿两次，无固化偏向


def test_deficit_is_conserved_and_signed():
    out = build({"fund_year": "2026", "batch_id": "b1", "total": "10.00", "rows": _rows(3)}, None, 0, False)
    # 截底零头合计守恒，欠额按 1e-8 次级精度归整，残差不超过一个量化单位。
    assert abs(sum(Decimal(v) for v in out["deficit_delta"].values())) <= Decimal("0.00000001")
    # 有符号台账：拿尾分者为负（超发），其余为正（待补偿）。
    assert Decimal(out["deficit_after"]["A"]) < 0
    assert Decimal(out["deficit_after"]["B"]) > 0


def test_capped_project_parks_and_never_breaches_cap():
    rows = [
        {"project_id": "A", "weight": "1", "cap": "3.33"},
        {"project_id": "B", "weight": "1", "cap": "3.33"},
        {"project_id": "C", "weight": "1", "cap": "3.33"},
    ]
    out = build({"fund_year": "2026", "batch_id": "b1", "total": "10.00", "rows": rows}, None, 0, False)
    assert all(Decimal(out["amounts"][p]) <= Decimal("3.33") for p in "ABC")
    assert out["unallocated"] == "0.01"          # M4 封顶余量留作批次未分配，不突破任何上限
    assert out["reason"] == "CAP_LIMIT"
    assert all(line["reason"] == "CAP" for line in out["lines"])
    # 封顶项目仍保留欠额台账行（本批差额为 0，后续批次照常结转）。
    assert "deficit_after" in out and all(pid in out["deficit_after"] for pid in "ABC")


def test_tail_cents_skip_capped_candidate():
    # 原始截底恰好把 A 顶到 cap，尾分只能发给未封顶的 B。
    rows = [
        {"project_id": "A", "weight": "1", "cap": "3.33"},
        {"project_id": "B", "weight": "1", "cap": "10.00"},
        {"project_id": "C", "weight": "1", "cap": "10.00"},
    ]
    out = build({"fund_year": "2026", "batch_id": "b1", "total": "10.00", "rows": rows}, None, 0, False)
    assert out["amounts"]["A"] == "3.33"
    assert out["lines"][0]["parked"] is True
    assert out["lines"][0]["reason"] == "CAP_PARKED"
    assert out["amounts"]["B"] == "3.34"


def test_carry_over_only_within_same_fund_year():
    first = build({"fund_year": "2026", "batch_id": "b1", "total": "10.00", "rows": _rows(3)}, None, 0, True)
    next_year = build({"fund_year": "2027", "batch_id": "b1", "total": "10.00", "rows": _rows(3)}, None, 0, True)
    same_year = build({"fund_year": "2026", "batch_id": "b2", "total": "10.00", "rows": _rows(3)},
                      first["deficit_after"], 1, True)
    assert next_year["amounts"]["A"] == "3.34"          # 新年度从零起步
    assert same_year["amounts"]["A"] == "3.33"          # 同年度欠额生效


def test_preview_does_not_mutate_ledger_shape():
    out = build({"fund_year": "2026", "batch_id": "b1", "total": "10.00", "rows": _rows(3)}, None, 0, False)
    assert out["committed"] is False
    assert out["version_after"] == out["version_before"] == 0


def test_commit_bumps_version_in_response():
    out = build({"fund_year": "2026", "batch_id": "b1", "total": "10.00", "rows": _rows(3)}, None, 0, True)
    assert out["committed"] is True
    assert (out["version_before"], out["version_after"]) == (0, 1)


def test_order_independence():
    rows = _rows(3)
    forward = build({"fund_year": "2026", "batch_id": "b1", "total": "10.00", "rows": rows}, None, 0, False)
    reverse = build({"fund_year": "2026", "batch_id": "b1", "total": "10.00", "rows": list(reversed(rows))}, None, 0, False)
    assert forward["amounts"] == reverse["amounts"]
    assert forward["digest"] == reverse["digest"]


def test_zero_total_settles_zero():
    out = build({"fund_year": "2026", "batch_id": "b0", "total": "0.00", "rows": _rows(2)}, None, 0, False)
    assert out["allocated_total"] == "0.00"
    assert set(out["amounts"].values()) == {"0.00"}


def test_invalid_fund_year_rejected():
    with pytest.raises(DomainError) as exc:
        build({"fund_year": "26", "batch_id": "b1", "total": "10.00", "rows": _rows(2)}, None, 0, False)
    assert exc.value.code == "INVALID_FUND_YEAR"


def test_duplicate_project_rejected():
    rows = [{"project_id": "A", "weight": "1", "cap": "9"}, {"project_id": "A", "weight": "1", "cap": "9"}]
    with pytest.raises(DomainError) as exc:
        build({"fund_year": "2026", "batch_id": "b1", "total": "10.00", "rows": rows}, None, 0, False)
    assert exc.value.code == "DUPLICATE_PROJECT"


def test_quantized_to_cent():
    out = build({"fund_year": "2026", "batch_id": "b1", "total": "7.77", "rows": _rows(3)}, None, 0, False)
    assert all(Decimal(v).as_tuple().exponent == -2 for v in out["amounts"].values())
    assert Decimal(out["allocated_total"]) == Decimal("7.77")
