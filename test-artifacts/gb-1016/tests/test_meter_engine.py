"""电表计量引擎口径单元测试（纯函数，不依赖数据库与第三方库）。"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / 'backend'))

import pytest

from src.services import meter_engine as engine


def reading(meter, line, moment, value, event=None, handoff=None):
    row = {'meter_id': meter, 'line': line, 'read_time': moment, 'reading': value}
    if event:
        row['event'] = event
    if handoff is not None:
        row['handoff'] = handoff
    return row


def base_daily_series():
    """每日 08:00 一个累计读数：100,130,180,缺测,缺测,300,330,360。"""
    return [
        reading('M1', 'L1', '2026-05-01T08:00', 100),
        reading('M1', 'L1', '2026-05-02T08:00', 130),
        reading('M1', 'L1', '2026-05-03T08:00', 180),
        reading('M1', 'L1', '2026-05-06T08:00', 300),
        reading('M1', 'L1', '2026-05-07T08:00', 330),
        reading('M1', 'L1', '2026-05-08T08:00', 360),
    ]


def test_daily_consumption_is_reading_difference():
    records = engine.validate_readings(base_daily_series())
    preview = engine.build_preview(records)
    view = preview['daily_view']['M1']
    assert view['2026-05-02']['kwh'] == 30
    assert view['2026-05-03']['kwh'] == 50
    assert view['2026-05-07']['kwh'] == 30
    assert view['2026-05-08']['kwh'] == 30
    assert view['2026-05-02']['status'] == 'complete'


def test_cross_day_gap_is_never_apportioned():
    records = engine.validate_readings(base_daily_series())
    segments = engine.build_segments(records)
    gap = [s for s in segments if s['status'] == 'gap']
    assert len(gap) == 1
    assert gap[0]['start_day'] == '2026-05-03' and gap[0]['end_day'] == '2026-05-06'
    assert gap[0]['delta'] == 120
    view = engine.build_daily_view(records, segments)['M1']
    for day in ('2026-05-04', '2026-05-05'):
        assert view[day]['status'] == 'gap'
        assert view[day]['kwh'] is None  # 120 度绝不摊到缺测日


def test_reconciliation_daily_sum_equals_valid_difference():
    records = engine.validate_readings(base_daily_series())
    preview = engine.build_preview(records)
    recon = preview['reconciliation']['M1']
    assert recon['balanced'] is True
    # 完整日耗：30+50+30+30=140；gap 排除 120；首尾读数差 260=140+120
    assert recon['daily_kwh'] == 140
    assert recon['complete_kwh'] == 140
    assert recon['excluded_gap_kwh'] == 120
    assert recon['last_reading'] - recon['first_reading'] == 260


def test_rollback_without_handoff_is_isolated():
    rows = base_daily_series()
    rows.append(reading('M1', 'L1', '2026-05-09T08:00', 5))  # 表底回零
    records = engine.validate_readings(rows)
    segments = engine.build_segments(records)
    isolated = [s for s in segments if s['status'] == 'isolated']
    assert len(isolated) == 1
    assert isolated[0]['end_time'] == '2026-05-09T08:00:00'
    view = engine.build_daily_view(records, segments)['M1']
    assert view['2026-05-09']['status'] == 'isolated'
    assert view['2026-05-09']['kwh'] is None
    # 隔离不影响其他日耗与对账
    assert engine.reconciliation(records, segments)['M1']['balanced'] is True


def test_reset_event_without_handoff_is_isolated():
    rows = base_daily_series()
    rows.append(reading('M1', 'L1', '2026-05-09T08:00', 20, event='reset'))
    records = engine.validate_readings(rows)
    segments = engine.build_segments(records)
    assert any(s['status'] == 'isolated' and '交接读数' in s['reason'] for s in segments)


def test_replace_with_handoff_keeps_segment_valid():
    rows = base_daily_series()
    # 旧表末值交接读数 380：新表从 0 起，但交接读数让旧区间差额可算
    rows.append(reading('M1', 'L1', '2026-05-09T08:00', 0, event='replace', handoff=380))
    records = engine.validate_readings(rows)
    segments = engine.build_segments(records)
    last = [s for s in segments if s['end_time'] == '2026-05-09T08:00:00'][0]
    assert last['status'] == 'complete'
    assert last['delta'] == 20  # 380 - 360
    view = engine.build_daily_view(records, segments)['M1']
    assert view['2026-05-09']['kwh'] == 20


def test_late_arrival_rebuilds_adjacent_segments_and_conserves_total():
    # 原始：05-03 -> 05-06 是 gap（120 度被排除）
    records = engine.validate_readings(base_daily_series())
    before = engine.build_preview(records)
    assert before['daily_view']['M1']['2026-05-04']['status'] == 'gap'
    assert before['reconciliation']['M1']['complete_kwh'] == 140
    # 迟到补录 05-04、05-05 两个读数
    backfilled = base_daily_series()
    backfilled.insert(3, reading('M1', 'L1', '2026-05-04T08:00', 220))
    backfilled.insert(4, reading('M1', 'L1', '2026-05-05T08:00', 260))
    rebuilt = engine.build_preview(engine.validate_readings(backfilled))
    view = rebuilt['daily_view']['M1']
    assert view['2026-05-04']['status'] == 'complete'
    assert view['2026-05-04']['kwh'] == 40
    assert view['2026-05-05']['kwh'] == 40
    assert rebuilt['segment_counts']['gap'] == 0
    # 补录后完整日耗之和 = 全部相邻有效差额 = 首尾读数差
    recon = rebuilt['reconciliation']['M1']
    assert recon['complete_kwh'] == 260
    assert recon['complete_kwh'] == recon['last_reading'] - recon['first_reading']
    assert recon['balanced'] is True


def test_same_day_multiple_segments_accumulate():
    rows = [
        reading('M1', 'L1', '2026-05-01T08:00', 100),
        reading('M1', 'L1', '2026-05-01T16:00', 150),
        reading('M1', 'L1', '2026-05-01T23:00', 180),
        reading('M1', 'L1', '2026-05-02T08:00', 210),
    ]
    records = engine.validate_readings(rows)
    view = engine.build_daily_view(records, engine.build_segments(records))['M1']
    assert view['2026-05-01']['kwh'] == 80  # 50 + 30
    assert view['2026-05-02']['kwh'] == 30


def test_multi_meter_lines_are_separated():
    rows = base_daily_series() + [
        reading('M2', 'L2', '2026-05-01T08:00', 0),
        reading('M2', 'L2', '2026-05-02T08:00', 45),
    ]
    records = engine.validate_readings(rows)
    preview = engine.build_preview(records)
    assert preview['meters'] == ['M1', 'M2']
    assert preview['lines'] == ['L1', 'L2']
    assert preview['reconciliation']['M2']['daily_kwh'] == 45
    assert all(item['balanced'] for item in preview['reconciliation'].values())


def test_validation_rejects_bad_rows():
    with pytest.raises(engine.MeterValidationError):
        engine.validate_readings([{'meter_id': 'M1', 'line': 'L1', 'read_time': 'bad', 'reading': 1}])
    with pytest.raises(engine.MeterValidationError):
        engine.validate_readings([{'meter_id': 'M1', 'line': 'L1', 'read_time': '2026-05-01T08:00', 'reading': -1}])
    with pytest.raises(engine.MeterValidationError):
        engine.validate_readings([
            reading('M1', 'L1', '2026-05-01T08:00', 1),
            reading('M1', 'L1', '2026-05-01T08:00', 2),
        ])
    with pytest.raises(engine.MeterValidationError):
        engine.validate_readings([{'meter_id': 'M1', 'line': 'L1',
                                   'read_time': '2026-05-01T08:00', 'reading': 1, 'event': 'boom'}])


def test_first_reading_day_has_no_consumption():
    records = engine.validate_readings(base_daily_series())
    view = engine.build_daily_view(records, engine.build_segments(records))['M1']
    assert '2026-05-01' not in view  # 首读数日无前序读数，不生成日耗
