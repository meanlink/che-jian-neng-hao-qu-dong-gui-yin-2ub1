"""电表批次接入/发布/补录/合并的数据库级集成测试（SQLite 内存库）。"""
import sys
import types
import pathlib

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker

BACKEND = pathlib.Path(__file__).resolve().parents[3] / 'backend'
sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def db_session(monkeypatch):
    engine_db = create_engine('sqlite:///:memory:')
    # 切到内存库后再导入模型，保证所有表建在同一引擎上。
    from src.config import database as db_module
    monkeypatch.setattr(db_module, 'engine', engine_db)
    monkeypatch.setattr(db_module, 'Session', sessionmaker(engine_db, expire_on_commit=False))
    from src.config.database import Base
    from src import models  # noqa: F401  注册全部 ORM 模型
    Base.metadata.create_all(engine_db)
    Session = sessionmaker(engine_db, expire_on_commit=False)
    with Session() as session:
        user = models.User(email='analyst@demo.local', name='测试分析员', role='analyst',
                           password='x')
        admin = models.User(email='admin@demo.local', name='测试负责人', role='admin', password='x')
        session.add_all([user, admin])
        session.flush()
        yield session, user, admin


def _ingest(session, user, name, rows, token=None):
    from src.schemas.meter import MeterIngestInput
    from src.services import meters
    data = MeterIngestInput(name=name, readings=rows, request_token=token)
    batch = meters.create_batch(session, data, user)
    session.commit()
    return batch


def _publish(session, user, batch_id):
    from src.services import meters
    batch = meters.publish_batch(session, batch_id, user)
    session.commit()
    return batch


def _readings(values, meter='M1', line='冲压P01', start_day=1, hour='08:00'):
    return [{'meter_id': meter, 'line': line,
             'read_time': f'2026-05-{start_day + i:02d}T{hour}', 'reading': value}
            for i, value in enumerate(values)]


def test_ingest_draft_preview_and_publish_materializes_daily(db_session):
    session, user, _ = db_session
    from src.models import MeterDaily, MeterSegment, MeterState
    batch = _ingest(session, user, '首批电表读数', _readings([100, 130, 180]))
    assert batch.status == 'draft'
    assert batch.preview['publishable'] is True
    _publish(session, user, batch.id)
    daily = list(session.scalars(select(MeterDaily).order_by(MeterDaily.day)))
    assert [d.day for d in daily] == ['2026-05-02', '2026-05-03']
    assert [d.kwh for d in daily] == [30, 50]
    # 每笔日耗都带读数依据，且严格等于读数差
    for d in daily:
        assert d.end_reading - d.start_reading == d.kwh
    state = session.scalar(select(MeterState).where(MeterState.meter_id == 'M1'))
    assert state.generation == 1 and state.batch_id == batch.id
    segments = session.scalars(select(MeterSegment)).all()
    assert all(s.status == 'complete' for s in segments)


def test_gap_days_have_no_daily_and_full_sum_equals_reading_difference(db_session):
    session, user, _ = db_session
    from src.models import MeterDaily, MeterSegment
    # 05-03 之后跳到 05-06，中间缺测
    rows = [{'meter_id': 'M1', 'line': '冲压P01', 'read_time': t, 'reading': v}
            for t, v in [('2026-05-01T08:00', 100), ('2026-05-02T08:00', 130),
                         ('2026-05-03T08:00', 180), ('2026-05-06T08:00', 300),
                         ('2026-05-07T08:00', 330)]]
    batch = _ingest(session, user, '含缺测批次', rows)
    _publish(session, user, batch.id)
    gap = session.scalars(select(MeterSegment).where(MeterSegment.status == 'gap')).all()
    assert len(gap) == 1 and gap[0].delta == 120
    days = {d.day: d.kwh for d in session.scalars(select(MeterDaily))}
    assert '2026-05-04' not in days and '2026-05-05' not in days and '2026-05-06' not in days
    assert days == {'2026-05-02': 30, '2026-05-03': 50, '2026-05-07': 30}
    # 不变量：日耗之和 == 有效（complete）读数差额
    assert sum(days.values()) == 110
    assert 330 - 100 - 120 == 110


def test_isolated_rollback_and_reset_produce_no_daily(db_session):
    session, user, _ = db_session
    from src.models import MeterDaily, MeterSegment
    rows = _readings([100, 130, 180])
    rows.append({'meter_id': 'M1', 'line': '冲压P01', 'read_time': '2026-05-04T08:00', 'reading': 5})
    rows.append({'meter_id': 'M1', 'line': '冲压P01', 'read_time': '2026-05-05T08:00',
                 'reading': 20, 'event': 'reset'})
    batch = _ingest(session, user, '回零隔离批次', rows)
    _publish(session, user, batch.id)
    isolated = session.scalars(select(MeterSegment).where(MeterSegment.status == 'isolated')).all()
    assert len(isolated) == 2
    days = {d.day for d in session.scalars(select(MeterDaily))}
    assert '2026-05-04' not in days and '2026-05-05' not in days
    # 正常日耗保留
    assert session.scalar(select(func.count()).select_from(MeterDaily)) == 2


def test_late_backfill_supersedes_old_batch_and_switches_whole_affected_days(db_session):
    session, user, _ = db_session
    from src.models import MeterBatch, MeterDaily, MeterState
    first_rows = [{'meter_id': 'M1', 'line': '冲压P01', 'read_time': t, 'reading': v}
                  for t, v in [('2026-05-01T08:00', 100), ('2026-05-02T08:00', 130),
                               ('2026-05-03T08:00', 180), ('2026-05-06T08:00', 300)]]
    first = _publish(session, user, _ingest(session, user, '初始批次', first_rows).id)
    assert session.scalar(select(func.count()).select_from(MeterDaily)) == 2  # 02、03
    # 迟到补录 04、05
    backfill_rows = first_rows[:3] + [
        {'meter_id': 'M1', 'line': '冲压P01', 'read_time': '2026-05-04T08:00', 'reading': 220},
        {'meter_id': 'M1', 'line': '冲压P01', 'read_time': '2026-05-05T08:00', 'reading': 260},
        first_rows[3]]
    second = _publish(session, user, _ingest(session, user, '补录批次', backfill_rows).id)
    session.refresh(first)
    assert first.status == 'superseded' and second.status == 'published'
    state = session.scalar(select(MeterState).where(MeterState.meter_id == 'M1'))
    assert state.generation == 2 and state.batch_id == second.id
    days = {d.day: d.kwh for d in session.scalars(select(MeterDaily).order_by(MeterDaily.day))}
    # 旧跨段关系撤销，相邻区间重建：40、40 补上，06 仍因 05->06 相邻而成立 40
    assert days == {'2026-05-02': 30, '2026-05-03': 50, '2026-05-04': 40,
                    '2026-05-05': 40, '2026-05-06': 40}
    assert sum(days.values()) == 200  # 300 - 100
    # 整组切换：库里不存在旧代次的残留日耗
    assert session.scalar(select(func.count()).select_from(MeterDaily)
                          .where(MeterDaily.generation == 1)) == 0


def test_idempotent_request_token_and_publish_retry(db_session):
    session, user, _ = db_session
    from src.models import MeterBatch, MeterDaily
    rows = _readings([100, 130, 180])
    first = _ingest(session, user, '幂等批次', rows, token='token-xyz')
    again = _ingest(session, user, '幂等批次', rows, token='token-xyz')
    assert first.id == again.id
    assert session.scalar(select(func.count()).select_from(MeterBatch)) == 1
    published = _publish(session, user, first.id)
    republished = _publish(session, user, first.id)  # 重试不报错
    assert republished.id == published.id
    assert session.scalar(select(func.count()).select_from(MeterDaily)) == 2


def test_rebuild_is_idempotent_and_does_not_duplicate(db_session):
    session, user, admin = db_session
    from src.models import MeterDaily
    _publish(session, user, _ingest(session, user, '重建批次', _readings([100, 130, 180])).id)
    from src.services import meters
    meters.rebuild_meter(session, 'M1')
    session.commit()
    meters.rebuild_meter(session, 'M1')  # 模拟中断后恢复重试
    session.commit()
    assert session.scalar(select(func.count()).select_from(MeterDaily)) == 2


def test_merge_only_uses_published_complete_days_and_keeps_import_path(db_session):
    session, user, _ = db_session
    from src.models import Dataset
    from src.schemas.meter import MeterMergeInput
    from src.services import meters
    # 生产特征批次（原有日样本导入路径不受影响）
    production = [
        {'day': '2026-05-02', 'line': '冲压P01', 'units': 800, 'run_hours': 12,
         'temp_c': 25, 'idle_hours': 1, 'kwh': 999},
        {'day': '2026-05-03', 'line': '冲压P01', 'units': 820, 'run_hours': 13,
         'temp_c': 26, 'idle_hours': 1, 'kwh': 999},
        {'day': '2026-05-04', 'line': '冲压P01', 'units': 830, 'run_hours': 12,
         'temp_c': 26, 'idle_hours': 1, 'kwh': 999},
    ]
    base = Dataset(name='生产特征批次', source='prod.json', records=production,
                   checksum='abc', created_at=1.0)
    session.add(base)
    session.flush()
    # 电表只有 02、03 完整，04 缺测
    rows = [{'meter_id': 'M1', 'line': '冲压P01', 'read_time': t, 'reading': v}
            for t, v in [('2026-05-01T08:00', 100), ('2026-05-02T08:00', 130),
                         ('2026-05-03T08:00', 180), ('2026-05-05T08:00', 260)]]
    _publish(session, user, _ingest(session, user, '发布用于合并', rows).id)
    merged, meta = meters.merge_dataset(
        session, MeterMergeInput(name='电表口径合并批次', dataset_id=base.id), user)
    session.commit()
    assert len(merged.records) == 2  # 04 被排除
    assert {r['day'] for r in merged.records} == {'2026-05-02', '2026-05-03'}
    by_day = {r['day']: r['kwh'] for r in merged.records}
    assert by_day == {'2026-05-02': 30, '2026-05-03': 50}
    assert all(r['kwh_source'] == 'meter-published' for r in merged.records)
    assert meta['skipped'][0]['day'] == '2026-05-04'
    # 原批次原样保留，原有导入仍可用
    refreshed = session.get(Dataset, base.id)
    assert all(r['kwh'] == 999 for r in refreshed.records)


def test_daily_basis_traceable_to_two_readings(db_session):
    session, user, _ = db_session
    from src.services import meters
    from src.models import MeterDaily
    _publish(session, user, _ingest(session, user, '追查批次', _readings([100, 130, 180])).id)
    daily = session.scalar(select(MeterDaily).where(MeterDaily.day == '2026-05-03'))
    detail = meters.daily_basis(session, daily.id)
    assert detail['identity_check'] is True
    assert detail['start_reading']['reading'] == 130
    assert detail['end_reading']['reading'] == 180
    assert detail['segments'][0]['status'] == 'complete'


def test_same_day_multiple_segments_aggregate_with_traceable_basis(db_session):
    """同一日历日多个读数：聚合为一笔日耗，依据追查覆盖全部底层区间且恒等式成立。"""
    session, user, _ = db_session
    from src.services import meters
    from src.models import MeterDaily, MeterSegment
    rows = [
        {'meter_id': 'M1', 'line': '冲压P01', 'read_time': '2026-05-01T08:00', 'reading': 100},
        {'meter_id': 'M1', 'line': '冲压P01', 'read_time': '2026-05-01T16:00', 'reading': 150},
        {'meter_id': 'M1', 'line': '冲压P01', 'read_time': '2026-05-01T23:00', 'reading': 180},
        {'meter_id': 'M1', 'line': '冲压P01', 'read_time': '2026-05-02T08:00', 'reading': 210},
    ]
    _publish(session, user, _ingest(session, user, '同日多段批次', rows).id)
    # 每表每日唯一：05-01 只生成一笔聚合日耗 80
    day1 = session.scalar(select(MeterDaily).where(MeterDaily.day == '2026-05-01'))
    assert day1.kwh == 80
    day2 = session.scalar(select(MeterDaily).where(MeterDaily.day == '2026-05-02'))
    assert day2.kwh == 30
    # 底层仍是两段完整区间，日耗等于末读数−首读数
    detail = meters.daily_basis(session, day1.id)
    assert detail['identity_check'] is True
    assert len(detail['segments']) == 2
    assert round(sum(s['delta'] for s in detail['segments']), 6) == 80
    assert detail['end_reading']['reading'] - detail['start_reading']['reading'] == 80
    assert session.scalar(select(func.count()).select_from(MeterSegment)
                          .where(MeterSegment.status == 'complete')) == 3


def test_optimistic_lock_blocks_concurrent_publish(db_session):
    """两个独立会话并发发布同表批次：第二个抢占锁失败并整体回滚，无残留日耗。"""
    from src.config.database import Session as SessionFactory
    from src.services import meters
    from src.models import MeterBatch, MeterDaily, MeterState
    session, user, _ = db_session
    first_batch = _publish(session, user, _ingest(
        session, user, '初始', _readings([100, 130, 180])).id)
    # 两个迟到补录批次
    bid_a = _ingest(session, user, '补录A', _readings([100, 130, 180, 220])).id
    bid_b = _ingest(session, user, '补录B', _readings([100, 130, 180, 220])).id

    session_a = SessionFactory()
    session_b = SessionFactory()
    try:
        # 两个事务先各自读取状态行（模拟并发进入发布临界区）
        state_a = session_a.scalar(select(MeterState).where(MeterState.meter_id == 'M1'))
        state_b = session_b.scalar(select(MeterState).where(MeterState.meter_id == 'M1'))
        assert state_a.lock_version == state_b.lock_version
        meters.publish_batch(session_a, bid_a, user)
        session_a.commit()  # A 先提交，版本号 +1
        with pytest.raises(meters.PublishConflict):
            # B 仍持抢占前的旧版本快照，条件更新 rowcount=0
            meters.publish_batch(session_b, bid_b, user)
        session_b.rollback()
    finally:
        session_a.close()
        session_b.close()

    session.refresh(first_batch)
    assert first_batch.status == 'superseded'
    batch_b = session.get(MeterBatch, bid_b)
    assert batch_b.status == 'draft'  # 失败方保持草稿，未产生半成品
    state = session.scalar(select(MeterState).where(MeterState.meter_id == 'M1'))
    assert state.batch_id == bid_a
    # 失败方回滚后重试可成功（中断恢复语义），且日耗不重复
    republished = meters.publish_batch(session, bid_b, user)
    session.commit()
    assert republished.status == 'published'
    daily = session.scalars(select(MeterDaily).order_by(MeterDaily.day)).all()
    assert [d.day for d in daily] == ['2026-05-02', '2026-05-03', '2026-05-04']
    assert round(sum(d.kwh for d in daily), 6) == 120  # 220 - 100
