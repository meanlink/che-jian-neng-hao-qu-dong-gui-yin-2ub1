"""电表批次接入、发布与合并的数据库服务。

发布在单事务内完成：锁定每块电表的状态行 -> 标记旧批次 superseded ->
代次 +1 -> 删除旧派生行 -> 用新批次重建区间与日耗 -> 写入 MeterState。
任何一步失败整体回滚，重试不会产生重复日耗；请求令牌保证接入幂等。
"""
import time
from fastapi import HTTPException
from sqlalchemy import select, delete
from src.models import (Dataset, MeterBatch, MeterReading, MeterState,
                        MeterSegment, MeterDaily)
from src.services import meter_engine as engine
from src.utils.serialization import checksum


def _batch_dict(db, batch):
    reading_count = db.scalar(
        select(MeterReading.id).where(MeterReading.batch_id == batch.id).limit(1))
    return {
        'id': batch.id, 'name': batch.name, 'source': batch.source,
        'status': batch.status, 'meters': batch.meters, 'lines': batch.lines,
        'affected_days': batch.affected_days, 'preview': batch.preview,
        'checksum': batch.checksum, 'request_token': batch.request_token,
        'created_at': batch.created_at, 'published_at': batch.published_at,
        'has_readings': reading_count is not None,
    }


def create_batch(db, data, user):
    name = data.name.strip()
    if len(name) < 2:
        raise HTTPException(422, '批次名称去除空格后至少需要两个字符')
    if data.request_token:
        existing = db.scalar(select(MeterBatch).where(MeterBatch.request_token == data.request_token))
        if existing:
            return existing  # 同令牌重试：返回首次结果，不重复接入
    if db.scalar(select(MeterBatch).where(MeterBatch.name == name)):
        raise HTTPException(409, '该电表批次名已存在，请修改名称')
    try:
        records = engine.validate_readings(data.readings)
        preview = engine.build_preview(records)
    except engine.MeterValidationError as exc:
        raise HTTPException(422, str(exc)) from None
    fingerprint = checksum([{k: r[k] for k in ('meter_id', 'line', 'read_time_str', 'reading', 'event', 'handoff')}
                            for r in records])
    batch = MeterBatch(
        name=name, source=data.source, status='draft', meters=preview['meters'],
        lines=preview['lines'], affected_days=preview['affected_days'], preview=preview,
        checksum=fingerprint, request_token=data.request_token or None,
        created_by=user.id, created_at=time.time(),
    )
    db.add(batch)
    db.flush()
    for record in records:
        db.add(MeterReading(
            batch_id=batch.id, meter_id=record['meter_id'], line=record['line'],
            read_time=record['read_time_str'], reading=record['reading'],
            event=record['event'], handoff=record['handoff'],
        ))
    db.flush()
    return batch


def _lock_meter_states(db, meter_ids):
    """按固定顺序锁定电表状态行，避免并发发布互相死锁；缺行则插入。"""
    states = {}
    for meter_id in sorted(meter_ids):
        state = db.scalar(select(MeterState).where(MeterState.meter_id == meter_id).with_for_update())
        if state is None:
            state = MeterState(meter_id=meter_id, line='', generation=0, batch_id=None,
                               first_time=None, last_time=None, first_reading=None, last_reading=None)
            db.add(state)
            db.flush()
            state = db.scalar(select(MeterState).where(MeterState.meter_id == meter_id).with_for_update())
        states[meter_id] = state
    return states


def _materialize(db, batch, records, segments, states):
    """把一个批次的区间与日耗物化为当前代次的派生行（调用方持锁、处于事务内）。"""
    reading_rows = list(db.scalars(select(MeterReading).where(MeterReading.batch_id == batch.id)
                                   .order_by(MeterReading.meter_id, MeterReading.read_time)))
    reading_key = {(r.meter_id, r.read_time): r for r in reading_rows}
    daily_groups = {}
    for segment in segments:
        start_row = reading_key[(segment['meter_id'], segment['start_time'])]
        end_row = reading_key[(segment['meter_id'], segment['end_time'])]
        db.add(MeterSegment(
            batch_id=batch.id, meter_id=segment['meter_id'], line=segment['line'],
            generation=states[segment['meter_id']].generation,
            start_time=segment['start_time'], end_time=segment['end_time'],
            start_reading=segment['start_reading'],
            end_reading=segment['end_reading'],
            delta=segment['delta'] if segment['delta'] is not None else 0.0,
            status=segment['status'], reason=segment['reason'],
            start_reading_id=start_row.id, end_reading_id=end_row.id,
        ))
        if segment['status'] == engine.SEGMENT_COMPLETE:
            daily_groups.setdefault(
                (segment['meter_id'], segment['end_day']), []).append((segment, start_row, end_row))
    # 同一日历日多段先聚合为一笔日耗，保证每表每日唯一且日耗等于各有效差额之和。
    for (meter_id, day), group in daily_groups.items():
        total_kwh = round(sum(item[0]['delta'] for item in group), 6)
        first_segment, first_start, _ = group[0]
        last_segment, _, last_end = group[-1]
        db.add(MeterDaily(
            batch_id=batch.id, meter_id=meter_id, line=last_segment['line'],
            day=day, kwh=total_kwh,
            generation=states[meter_id].generation,
            start_reading_id=first_start.id, end_reading_id=last_end.id,
            start_time=first_segment['start_time'], end_time=last_segment['end_time'],
            start_reading=first_segment['start_reading'], end_reading=last_segment['end_reading'],
            created_at=time.time(),
        ))
    for meter_id, state in states.items():
        meter_rows = [r for r in reading_rows if r.meter_id == meter_id]
        meter_rows.sort(key=lambda r: r.read_time)
        state.line = meter_rows[-1].line
        state.batch_id = batch.id
        state.first_time = meter_rows[0].read_time
        state.last_time = meter_rows[-1].read_time
        state.first_reading = meter_rows[0].reading
        state.last_reading = meter_rows[-1].reading


class PublishConflict(Exception):
    """乐观锁检测到并发发布，调用方应回滚后重试。"""


def _acquire_meter_locks(db, meter_ids):
    """按固定顺序对每块电表做条件版本更新，抢占发布权。

    MySQL 上同时有 with_for_update 行锁串行化；无行锁引擎（SQLite）上，
    条件 UPDATE 的 rowcount=0 即表示已被并发事务抢先，调用方必须回滚重试。
    """
    states = _lock_meter_states(db, meter_ids)
    expected = {}
    for meter_id, state in states.items():
        version = state.lock_version
        result = db.execute(
            MeterState.__table__.update()
            .where(MeterState.meter_id == meter_id, MeterState.lock_version == version)
            .values(lock_version=version + 1)
        )
        if result.rowcount != 1:
            raise PublishConflict(meter_id)
        db.refresh(state)
        expected[meter_id] = version
    return states, expected


def publish_batch(db, batch_id, user):
    batch = db.get(MeterBatch, batch_id)
    if batch is None:
        raise HTTPException(404, '电表批次不存在')
    if batch.status == 'published':
        return batch  # 发布重试：已发布即幂等返回
    if batch.status == 'superseded':
        raise HTTPException(409, '该批次已被后续批次替代，不能发布')
    records = engine.validate_readings([
        {'meter_id': r.meter_id, 'line': r.line, 'read_time': r.read_time,
         'reading': r.reading, 'event': r.event, 'handoff': r.handoff}
        for r in db.scalars(select(MeterReading).where(MeterReading.batch_id == batch.id))
    ])
    preview = engine.build_preview(records)
    if not preview['publishable']:
        raise HTTPException(422, '对账不平衡，拒绝发布；请隔离异常区间后重新接入')
    segments = engine.build_segments(records)
    states, _expected = _acquire_meter_locks(db, preview['meters'])
    # 旧批次一次性整体失效，保证归因永远只读到完整发布代次。
    for meter_id, state in states.items():
        if state.batch_id is not None:
            previous = db.get(MeterBatch, state.batch_id)
            if previous and previous.status == 'published':
                previous.status = 'superseded'
            db.execute(delete(MeterDaily).where(MeterDaily.meter_id == meter_id))
            db.execute(delete(MeterSegment).where(MeterSegment.meter_id == meter_id))
        state.generation += 1
    batch.status = 'published'
    batch.published_at = time.time()
    batch.preview = preview
    _materialize(db, batch, records, segments, states)
    db.flush()
    return batch


def rebuild_meter(db, meter_id):
    """中断恢复入口：用该电表当前发布批次幂等重建派生行（先删后建，同事务）。"""
    state = db.scalar(select(MeterState).where(MeterState.meter_id == meter_id).with_for_update())
    if state is None or state.batch_id is None:
        raise HTTPException(404, '该电表尚无发布批次')
    batch = db.get(MeterBatch, state.batch_id)
    records = engine.validate_readings([
        {'meter_id': r.meter_id, 'line': r.line, 'read_time': r.read_time,
         'reading': r.reading, 'event': r.event, 'handoff': r.handoff}
        for r in db.scalars(select(MeterReading).where(MeterReading.batch_id == batch.id))
    ])
    records = [r for r in records if r['meter_id'] == meter_id]
    segments = [s for s in engine.build_segments(records) if s['meter_id'] == meter_id]
    db.execute(delete(MeterDaily).where(MeterDaily.meter_id == meter_id))
    db.execute(delete(MeterSegment).where(MeterSegment.meter_id == meter_id))
    _materialize(db, batch, records, segments, {meter_id: state})
    db.flush()
    return state


def verify_published(db):
    """启动期体检：核对每个已发布批次的日耗守恒，返回需要重建的电表清单。

    进程在发布事务提交前崩溃时，数据库不会留下半成品（事务原子性）；
    若外部干预或存储异常导致派生行缺失，这里逐表核对 日耗和 == 有效差额，
    不一致即交由 rebuild_meter 幂等重建，重试绝不重复产生日耗。
    """
    inconsistent = []
    for state in db.scalars(select(MeterState).where(MeterState.batch_id.isnot(None))):
        batch = db.get(MeterBatch, state.batch_id)
        if batch is None or batch.status != 'published':
            inconsistent.append(state.meter_id)
            continue
        readings = [r for r in db.scalars(
            select(MeterReading).where(MeterReading.batch_id == batch.id))
            if r.meter_id == state.meter_id]
        if len(readings) < 2:
            continue
        records = engine.validate_readings([
            {'meter_id': r.meter_id, 'line': r.line, 'read_time': r.read_time,
             'reading': r.reading, 'event': r.event, 'handoff': r.handoff} for r in readings])
        segments = engine.build_segments(records)
        expected = round(sum(s['delta'] for s in segments if s['status'] == engine.SEGMENT_COMPLETE), 6)
        actual = round(sum(d.kwh for d in db.scalars(select(MeterDaily).where(MeterDaily.meter_id == state.meter_id))), 6)
        if abs(expected - actual) > 1e-6:
            inconsistent.append(state.meter_id)
    return inconsistent


def recover_on_startup(db):
    """worker/backend 启动调用：体检并幂等重建所有不一致电表，返回处理摘要。"""
    repaired = []
    for meter_id in verify_published(db):
        try:
            rebuild_meter(db, meter_id)
            repaired.append(meter_id)
        except HTTPException:
            continue
    db.flush()
    return repaired


def merge_dataset(db, data, user):
    """把已发布完整日耗合并进生产特征批次，生成可归因的标准 Dataset。"""
    base = db.get(Dataset, data.dataset_id)
    if base is None:
        raise HTTPException(404, '生产特征数据批次不存在')
    daily_rows = list(db.scalars(select(MeterDaily).order_by(MeterDaily.line, MeterDaily.day)))
    if not daily_rows:
        raise HTTPException(422, '尚无已发布日耗，无法合并；请先接入并发布电表批次')
    line_day_kwh = {}
    for row in daily_rows:
        line_day_kwh.setdefault((row.line, row.day), 0.0)
        line_day_kwh[(row.line, row.day)] = round(line_day_kwh[(row.line, row.day)] + row.kwh, 6)
    merged, skipped_gap, missing = [], [], []
    for record in base.records:
        key = (str(record.get('line')), str(record.get('day')))
        if key not in line_day_kwh:
            skipped_gap.append({'day': key[1], 'line': key[0], 'reason': '该线体当日无已发布完整日耗（缺测/隔离/待补齐）'})
            continue
        item = dict(record)
        item['kwh'] = line_day_kwh[key]
        item['kwh_source'] = 'meter-published'
        merged.append(item)
    covered = {(r.line, r.day) for r in daily_rows}
    for key in line_day_kwh:
        if not any(str(r.get('line')) == key[0] and str(r.get('day')) == key[1] for r in base.records):
            missing.append({'day': key[1], 'line': key[0]})
    if not merged:
        raise HTTPException(422, '生产特征与已发布日耗没有可对齐的线体/日期，拒绝生成空批次')
    if db.scalar(select(Dataset).where(Dataset.name == data.name.strip())):
        raise HTTPException(409, '该合并批次名已存在，请修改名称')
    dataset = Dataset(name=data.name.strip(), source=data.source, records=merged,
                      checksum=checksum(merged), created_at=time.time())
    db.add(dataset)
    db.flush()
    meta = {'skipped': skipped_gap, 'missing_meter_only': missing,
            'meter_daily_rows': len(daily_rows), 'covered_pairs': len(covered),
            'merged_rows': len(merged), 'base_dataset_id': base.id}
    return dataset, meta


def daily_basis(db, daily_id):
    """单笔日耗的读数依据：起止原始读数与当日全部底层区间，供页面追查。

    同日多段聚合时，日耗 = 末读数 − 首读数 = 各相邻区间差额之和（读数首尾相接）。
    """
    daily = db.get(MeterDaily, daily_id)
    if daily is None:
        raise HTTPException(404, '日耗记录不存在')
    start = db.get(MeterReading, daily.start_reading_id)
    end = db.get(MeterReading, daily.end_reading_id)
    day_segments = [s for s in db.scalars(select(MeterSegment).where(
        MeterSegment.meter_id == daily.meter_id,
        MeterSegment.batch_id == daily.batch_id))
        if s.end_time.startswith(daily.day)]
    return {
        'daily': {'id': daily.id, 'meter_id': daily.meter_id, 'line': daily.line,
                  'day': daily.day, 'kwh': daily.kwh, 'generation': daily.generation,
                  'batch_id': daily.batch_id},
        'start_reading': _reading_dict(start), 'end_reading': _reading_dict(end),
        'segments': [{
            'id': segment.id, 'status': segment.status, 'reason': segment.reason,
            'delta': segment.delta, 'start_time': segment.start_time,
            'end_time': segment.end_time, 'start_reading': segment.start_reading,
            'end_reading': segment.end_reading} for segment in day_segments],
        'identity_check': round(end.reading - start.reading, 6) == round(daily.kwh, 6),
    }


def _reading_dict(row):
    if row is None:
        return None
    return {'id': row.id, 'meter_id': row.meter_id, 'line': row.line,
            'read_time': row.read_time, 'reading': row.reading,
            'event': row.event, 'handoff': row.handoff, 'batch_id': row.batch_id}
