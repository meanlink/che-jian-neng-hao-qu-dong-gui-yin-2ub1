import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, MeterBatch
from src.services.business import metadata, seed_records, analyze
from src.services.reports import deliver
from src.services import meters as meter_service
from src.services import meter_engine
from src.schemas.meter import MeterIngestInput, MeterMergeInput
from src.utils.security import hash_password
from src.utils.serialization import checksum


def seed_meter_batch(db, rows, admin):
    """由模拟日耗反推累计电表读数，接入并发布一个可演示的电表批次（幂等）。"""
    meter_name = '澄川_冲压P01_电表原始读数.csv'
    if db.scalar(select(MeterBatch).where(MeterBatch.name == meter_name)):
        return
    readings, cumulative = [], 50000.0
    for index, row in enumerate(rows):
        if index > 0:
            cumulative = round(cumulative + row['kwh'], 4)
        readings.append({'meter_id': 'EM-P01-01', 'line': row['line'],
                         'read_time': row['day'] + 'T08:00', 'reading': cumulative})
    batch = meter_service.create_batch(
        db, MeterIngestInput(name=meter_name, source='meter-202611.csv',
                             request_token='seed-meter-p01', readings=readings), admin)
    meter_service.publish_batch(db, batch.id, admin)
    base = db.scalar(select(Dataset).where(Dataset.name == metadata()['dataset_name']))
    merged, _meta = meter_service.merge_dataset(
        db, MeterMergeInput(name='澄川_冲压一线_160日_电表口径.csv', dataset_id=base.id), admin)
    db.add(Audit(actor='seed', action='meter.seeded',
                 detail={'meter_batch': batch.id, 'merged_dataset': merged.id}, at=time.time()))


def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        if not db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name'])):
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        admin=db.scalar(select(User).where(User.role=='admin'))
        seed_meter_batch(db, d.records, admin)
        db.commit()
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,Audit,MeterBatch]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
if __name__=='__main__':seed()
