from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from src.config.database import get_db
from src.models import MeterBatch, MeterReading, MeterDaily, MeterSegment, MeterState
from src.schemas.meter import MeterIngestInput, MeterMergeInput
from src.middlewares.auth import current_user, can_write, admin_only
from src.services import meters
from src.services.audit import record

router = APIRouter(prefix='/meters')


@router.get('/batches')
def list_batches(db=Depends(get_db), u=Depends(current_user)):
    batches = db.scalars(select(MeterBatch).order_by(MeterBatch.id.desc()).limit(100)).all()
    return [meters._batch_dict(db, b) for b in batches]


@router.post('/batches', status_code=201)
def ingest(data: MeterIngestInput, db=Depends(get_db), u=Depends(can_write)):
    batch = meters.create_batch(db, data, u)
    record(db, u.email, 'meter.ingest', {'id': batch.id, 'name': batch.name})
    db.commit()
    return meters._batch_dict(db, batch)


@router.get('/batches/{batch_id}')
def batch_detail(batch_id: int, db=Depends(get_db), u=Depends(current_user)):
    batch = db.get(MeterBatch, batch_id)
    if not batch:
        from fastapi import HTTPException
        raise HTTPException(404, '电表批次不存在')
    readings = [{'id': r.id, 'meter_id': r.meter_id, 'line': r.line, 'read_time': r.read_time,
                 'reading': r.reading, 'event': r.event, 'handoff': r.handoff}
                for r in db.scalars(select(MeterReading).where(MeterReading.batch_id == batch_id)
                                     .order_by(MeterReading.meter_id, MeterReading.read_time))]
    segments = [{'id': s.id, 'meter_id': s.meter_id, 'line': s.line, 'status': s.status,
                 'reason': s.reason, 'delta': s.delta, 'start_time': s.start_time,
                 'end_time': s.end_time, 'start_reading': s.start_reading,
                 'end_reading': s.end_reading, 'generation': s.generation}
                for s in db.scalars(select(MeterSegment).where(MeterSegment.batch_id == batch_id)
                                     .order_by(MeterSegment.meter_id, MeterSegment.start_time))]
    return {**meters._batch_dict(db, batch), 'readings': readings, 'segments': segments}


@router.post('/batches/{batch_id}/publish')
def publish(batch_id: int, db=Depends(get_db), u=Depends(can_write)):
    import time as _time
    from src.services.meters import PublishConflict
    last_conflict = None
    for attempt in range(3):
        try:
            batch = meters.publish_batch(db, batch_id, u)
            record(db, u.email, 'meter.publish', {'id': batch.id, 'meters': batch.meters,
                                                  'affected_days': len(batch.affected_days),
                                                  'attempt': attempt + 1})
            db.commit()
            return meters._batch_dict(db, batch)
        except PublishConflict as conflict:
            # 并发发布抢占失败：整体回滚后重试，已写入的派生行随回滚撤销，绝不重复。
            db.rollback()
            last_conflict = conflict
            _time.sleep(0.05 * (attempt + 1))
    record(db, u.email, 'meter.publish.conflict', {'id': batch_id, 'meter': str(last_conflict)})
    db.commit()
    from fastapi import HTTPException
    raise HTTPException(409, '电表正被另一个发布任务占用，请重试；本次未产生任何日耗')


@router.post('/states/{meter_id}/rebuild')
def rebuild(meter_id: str, db=Depends(get_db), u=Depends(admin_only)):
    state = meters.rebuild_meter(db, meter_id)
    record(db, u.email, 'meter.rebuild', {'meter_id': meter_id, 'generation': state.generation})
    db.commit()
    return {'meter_id': meter_id, 'generation': state.generation, 'batch_id': state.batch_id}


@router.post('/merge', status_code=201)
def merge(data: MeterMergeInput, db=Depends(get_db), u=Depends(can_write)):
    dataset, meta = meters.merge_dataset(db, data, u)
    record(db, u.email, 'meter.merge', {'dataset_id': dataset.id, **{k: v for k, v in meta.items()
                                                                      if k in ('merged_rows', 'meter_daily_rows', 'base_dataset_id')}})
    db.commit()
    return {'id': dataset.id, 'name': dataset.name, 'source': dataset.source,
            'rows': len(dataset.records), 'checksum': dataset.checksum, 'merge_meta': meta}


@router.get('/daily')
def daily(line: str | None = None, meter_id: str | None = None, day: str | None = None,
          db=Depends(get_db), u=Depends(current_user)):
    statement = select(MeterDaily).order_by(MeterDaily.day.desc(), MeterDaily.meter_id).limit(2000)
    rows = db.scalars(statement).all()
    rows = [r for r in rows if (line is None or r.line == line)
            and (meter_id is None or r.meter_id == meter_id) and (day is None or r.day == day)]
    return [{'id': r.id, 'meter_id': r.meter_id, 'line': r.line, 'day': r.day, 'kwh': r.kwh,
             'generation': r.generation, 'batch_id': r.batch_id,
             'start_time': r.start_time, 'end_time': r.end_time,
             'start_reading': r.start_reading, 'end_reading': r.end_reading,
             'start_reading_id': r.start_reading_id, 'end_reading_id': r.end_reading_id} for r in rows]


@router.get('/daily/{daily_id}/basis')
def basis(daily_id: int, db=Depends(get_db), u=Depends(current_user)):
    return meters.daily_basis(db, daily_id)


@router.get('/status')
def status(db=Depends(get_db), u=Depends(current_user)):
    """页面追查：待补齐/隔离/可发布状态汇总（草稿预览 + 已发布日耗）。"""
    batches = db.scalars(select(MeterBatch).order_by(MeterBatch.id.desc())).all()
    states = {s.meter_id: {'meter_id': s.meter_id, 'line': s.line, 'generation': s.generation,
                           'batch_id': s.batch_id, 'first_time': s.first_time, 'last_time': s.last_time}
              for s in db.scalars(select(MeterState))}
    published_days = db.scalar(select(func.count()).select_from(MeterDaily))
    isolated = db.scalar(select(func.count()).select_from(MeterSegment)
                         .where(MeterSegment.status == 'isolated'))
    gaps = db.scalar(select(func.count()).select_from(MeterSegment)
                     .where(MeterSegment.status == 'gap'))
    draft_views = []
    for batch in batches:
        if batch.status != 'draft':
            continue
        preview = batch.preview or {}
        draft_views.append({'batch_id': batch.id, 'name': batch.name,
                            'day_counts': preview.get('day_counts', {}),
                            'segment_counts': preview.get('segment_counts', {}),
                            'publishable': preview.get('publishable', False),
                            'meter_summary': preview.get('meter_summary', [])})
    return {'published_daily_rows': published_days, 'isolated_segments': isolated,
            'gap_segments': gaps, 'meter_states': list(states.values()),
            'drafts': draft_views,
            'batches': [{'id': b.id, 'name': b.name, 'status': b.status,
                         'meters': b.meters, 'lines': b.lines,
                         'affected_days': len(b.affected_days)} for b in batches]}
