"""端到端冒烟：登录 → 接入草稿 → 缺测/隔离预览 → 发布 → 迟到补录重发布 →
合并归因批次 → 恢复体检 → 归因分析只取已发布完整批次。"""
import os
import sys
import pathlib
import tempfile

tmp = tempfile.mkdtemp()
os.environ['SEED_DEMO'] = 'false'
os.environ['ARTIFACT_DIR'] = os.path.join(tmp, 'artifacts')
os.environ['DATABASE_URL'] = f"sqlite:///{os.path.join(tmp, 'smoke.db')}"

BACKEND = pathlib.Path('/workspace/backend')
sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

engine_db = create_engine(os.environ['DATABASE_URL'], connect_args={'check_same_thread': False})


@event.listens_for(engine_db, 'connect')
def _timeout(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute('PRAGMA busy_timeout=30000')
    cur.close()


from src.config import database as db_module
db_module.engine = engine_db
TestingSession = sessionmaker(engine_db, expire_on_commit=False)
db_module.Session = TestingSession
from src.config.database import Base, get_db
from src import models
from src.utils.security import hash_password
Base.metadata.create_all(engine_db)
with TestingSession() as s:
    s.add(models.User(email='admin@demo.local', name='负责人', role='admin',
                      password=hash_password('Demo123!')))
    s.commit()


def override_db():
    with TestingSession() as db:
        yield db


from src.main import app
app.dependency_overrides[get_db] = override_db

client = TestClient(app)
auth = {'Authorization': 'Bearer ' + client.post('/auth/login',
        json={'email': 'admin@demo.local', 'password': 'Demo123!'}).json()['access_token']}

# 1) 原始批次：含跨日缺测 + 回零隔离
readings = [
    {'meter_id': 'EM-P01', 'line': '冲压P01', 'read_time': '2026-05-01T08:00', 'reading': 100},
    {'meter_id': 'EM-P01', 'line': '冲压P01', 'read_time': '2026-05-02T08:00', 'reading': 130},
    {'meter_id': 'EM-P01', 'line': '冲压P01', 'read_time': '2026-05-03T08:00', 'reading': 180},
    {'meter_id': 'EM-P01', 'line': '冲压P01', 'read_time': '2026-05-06T08:00', 'reading': 300},
    {'meter_id': 'EM-P01', 'line': '冲压P01', 'read_time': '2026-05-07T08:00', 'reading': 20},  # 回零
]
b1 = client.post('/meters/batches', json={'name': '冒烟原始批', 'readings': readings}, headers=auth)
assert b1.status_code == 201, b1.text
batch1 = b1.json()
print('草稿预览 day_counts =', batch1['preview']['day_counts'])
assert batch1['preview']['segment_counts']['gap'] == 1
assert batch1['preview']['segment_counts']['isolated'] == 1
p1 = client.post(f"/meters/batches/{batch1['id']}/publish", headers=auth)
assert p1.status_code == 200, p1.text

daily1 = client.get('/meters/daily', headers=auth).json()
print('首次发布日耗 =', [(d['day'], d['kwh']) for d in daily1])
assert {d['day'] for d in daily1} == {'2026-05-02', '2026-05-03'}

# 2) 读数依据追查
basis = client.get(f"/meters/daily/{daily1[0]['id']}/basis", headers=auth).json()
assert basis['identity_check'] is True
print('读数依据 =', basis['start_reading']['reading'], '->', basis['end_reading']['reading'])

# 3) 迟到补录：补齐缺测日，撤销旧跨段关系
backfill = readings[:3] + [
    {'meter_id': 'EM-P01', 'line': '冲压P01', 'read_time': '2026-05-04T08:00', 'reading': 220},
    {'meter_id': 'EM-P01', 'line': '冲压P01', 'read_time': '2026-05-05T08:00', 'reading': 260},
    readings[3]]
b2 = client.post('/meters/batches', json={'name': '冒烟补录批', 'readings': backfill}, headers=auth)
p2 = client.post(f"/meters/batches/{b2.json()['id']}/publish", headers=auth)
assert p2.status_code == 200
daily2 = client.get('/meters/daily', headers=auth).json()
print('补录后日耗 =', sorted((d['day'], d['kwh']) for d in daily2))
assert len(daily2) == 5 and abs(sum(d['kwh'] for d in daily2) - 200) < 1e-6
status = client.get('/meters/status', headers=auth).json()
print('状态 =', {k: status[k] for k in ('published_daily_rows', 'gap_segments', 'isolated_segments')})

# 4) 恢复体检：健康状态下应无需重建
from src.services import meters as meter_service
with TestingSession() as db:
    repaired = meter_service.recover_on_startup(db)
    db.commit()
assert repaired == [], repaired
print('启动恢复体检：无需重建 ✓')

# 5) 生产特征批次 + 合并（缺测/隔离日被排除）
prod_records = []
for i, day in enumerate(['2026-05-02', '2026-05-03', '2026-05-04', '2026-05-05', '2026-05-06']):
    prod_records.append({'day': day, 'line': '冲压P01', 'units': 800 + i, 'run_hours': 12,
                         'temp_c': 25, 'idle_hours': 1, 'kwh': 999})
ds = client.post('/datasets', json={'name': '冒烟生产批', 'records': prod_records}, headers=auth)
assert ds.status_code == 201, ds.text
merged = client.post('/meters/merge', json={'name': '冒烟合并批', 'dataset_id': ds.json()['id']}, headers=auth)
assert merged.status_code == 201, merged.text
print('合并结果 =', merged.json()['rows'], '条；跳过 =', len(merged.json()['merge_meta']['skipped']), '天')
assert merged.json()['rows'] == 5  # 补录后 02-06 全部完整
assert all(r['kwh'] != 999 for r in client.get(f"/datasets/{merged.json()['id']}", headers=auth).json()['records'])

# 6) 归因分析只取已发布完整批次（合并批可直接用于 analyze）
from src.services.business import analyze
merged_records = client.get(f"/datasets/{merged.json()['id']}", headers=auth).json()['records']
# 样本不足 70 天会被 analyze 拒绝，这里仅验证口径数据结构与守恒，不强行跑模型
assert all('kwh_source' in r and r['kwh_source'] == 'meter-published' for r in merged_records)
total = round(sum(r['kwh'] for r in merged_records), 6)
assert total == 200, total
print('合并批守恒：日耗合计 =', total, '= 有效读数差额 ✓')

print('\nSMOKE OK')
