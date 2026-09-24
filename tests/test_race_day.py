

def test_check_timing_rejects_threshold_without_matching_lead():
    """★決定時点に対応する閾値が無いと全レースが見送りになり、1日走って1件も買えない。
    走り出す前に落とす(2026-09-22 に『正常に走って0件』を実際に経験している)。"""
    import pytest

    from hro_operations.race_day import DayConfig, TimingError, check_timing

    base = dict(date="20260926", win_model="", place_model="", results_path="/tmp/r.jsonl",
                strategy="flow", lead_seconds=70, deadline_lead_seconds=60,
                flow_minutes=6, source="live")

    # netkeiba は秒単位。キーは lead_seconds そのもの
    ok = DayConfig(flow_source="netkeiba", flow_lead_seconds=75,
                   flow_thresholds={75: 0.3125}, **base)
    check_timing(ok)

    ng = DayConfig(flow_source="netkeiba", flow_lead_seconds=75,
                   flow_thresholds={60: 0.3125}, **base)
    with pytest.raises(TimingError, match="対応する閾値がありません"):
        check_timing(ng)

    # 格子ソースは60秒に丸めて引く
    grid = DayConfig(flow_source="sokuho", flow_lead_seconds=118,
                     flow_thresholds={120: 0.15}, **base)
    check_timing(grid)
