"""电表路由端到端测试：鉴权、完整接入发布流程、并发发布不重复。"""
import sys
import pathlib
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker

BACKEND = pathlib.Path(__file__).resolve().parents[3] / 'backend'
sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('SEED_DEMO', 'false')
    artifact_dir = str(tmp_path / 'artifacts')
    monkeypatch.setenv('ARTIFACT_DIR', artifact_dir)
    from src.config import settings as settings_module
    monkeypatch.setattr(settings_module, 'ARTIFACT_DIR', artifact_dir, raising=False)
    from sqlalchemy import event
    db_file = tmp_path / 'meter_api.db'
    engine_db = create_engine(f'sqlite:///{db_file}', connect_args={'check_same_thread': False, 'timeout': 30})

    @event.listens_for(engine_db, 'connect')
    def _busy_timeout(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute('PRAGMA busy_timeout=30000')
        cursor.close()

    from src.config import database as db_module
    monkeypatch.setattr(db_module, 'engine', engine_db)
    TestingSession = sessionmaker(engine_db, expire_on_commit=False)
    monkeypatch.setattr(db_module, 'Session', TestingSession)
    from src.config.database import Base, get_db
    from src import models  # noqa: F401
    from src.utils.security import hash_password
    Base.metadata.create_all(engine_db)
    with TestingSession() as session:
        session.add_all([
            models.User(email='admin@demo.local', name='负责人', role='admin',
                        password=hash_password('Demo123!')),
            models.User(email='analyst@demo.local', name='分析员', role='analyst',
                        password=hash_password('Demo123!')),
            models.User(email='viewer@demo.local', name='观察员', role='viewer',
                        password=hash_password('Demo123!')),
        ])
        session.commit()

    def override_db():
        with TestingSession() as db:
            yield db

    from src.main import app
    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client, TestingSession
    app.dependency_overrides.clear()


def _token(client, email):
    response = client.post('/auth/login', json={'email': email, 'password': 'Demo123!'})
    assert response.status_code == 200
    return response.json()['access_token']


def _auth(token):
    return {'Authorization': f'Bearer {token}'}


def _rows(pairs, meter='M1', line='冲压P01'):
    return [{'meter_id': meter, 'line': line, 'read_time': t, 'reading': v} for t, v in pairs]


def test_viewer_cannot_ingest_but_can_read(client):
    test_client, _ = client
    viewer = _token(test_client, 'viewer@demo.local')
    payload = {'name': '越权批次', 'readings': _rows([('2026-05-01T08:00', 1), ('2026-05-02T08:00', 5)])}
    assert test_client.post('/meters/batches', json=payload, headers=_auth(viewer)).status_code == 403
    assert test_client.get('/meters/status', headers=_auth(viewer)).status_code == 200
    assert test_client.get('/meters/batches', headers=_auth(viewer)).status_code == 200


def test_full_ingest_publish_daily_basis_flow(client):
    test_client, _ = client
    analyst = _token(test_client, 'analyst@demo.local')
    payload = {'name': '端到端批次', 'readings': _rows([
        ('2026-05-01T08:00', 100), ('2026-05-02T08:00', 130),
        ('2026-05-03T08:00', 180), ('2026-05-06T08:00', 300)])}
    created = test_client.post('/meters/batches', json=payload, headers=_auth(analyst))
    assert created.status_code == 201
    batch = created.json()
    assert batch['status'] == 'draft'
    assert batch['preview']['segment_counts']['gap'] == 1
    detail = test_client.get(f"/meters/batches/{batch['id']}", headers=_auth(analyst)).json()
    assert len(detail['readings']) == 4  # 草稿区间在 preview 中，发布后才物化为表行
    assert len(detail['preview']['segments']) == 3
    published = test_client.post(f"/meters/batches/{batch['id']}/publish",
                                 headers=_auth(analyst))
    assert published.status_code == 200 and published.json()['status'] == 'published'
    published_detail = test_client.get(f"/meters/batches/{batch['id']}", headers=_auth(analyst)).json()
    assert len(published_detail['segments']) == 3
    daily = test_client.get('/meters/daily', headers=_auth(analyst)).json()
    assert {d['day']: d['kwh'] for d in daily} == {'2026-05-02': 30, '2026-05-03': 50}
    basis = test_client.get(f"/meters/daily/{daily[0]['id']}/basis", headers=_auth(analyst)).json()
    assert basis['identity_check'] is True
    status = test_client.get('/meters/status', headers=_auth(analyst)).json()
    assert status['gap_segments'] == 1 and status['published_daily_rows'] == 2


def test_duplicate_request_token_is_idempotent(client):
    test_client, _ = client
    analyst = _token(test_client, 'analyst@demo.local')
    payload = {'name': '令牌批次', 'request_token': 'dup-1',
               'readings': _rows([('2026-05-01T08:00', 1), ('2026-05-02T08:00', 5)])}
    first = test_client.post('/meters/batches', json=payload, headers=_auth(analyst)).json()
    second = test_client.post('/meters/batches', json=payload, headers=_auth(analyst)).json()
    assert first['id'] == second['id']
    batches = test_client.get('/meters/batches', headers=_auth(analyst)).json()
    assert len(batches) == 1


def test_concurrent_backfill_publishes_switch_atomically_without_duplicates(client):
    """两个并发补录批次针对同一电表：最终只保留最新代次，日耗不重复、对账守恒。"""
    test_client, TestingSession = client
    analyst = _token(test_client, 'analyst@demo.local')
    initial = {'name': '并发初始', 'readings': _rows([
        ('2026-05-01T08:00', 100), ('2026-05-02T08:00', 130),
        ('2026-05-03T08:00', 180), ('2026-05-06T08:00', 300)])}
    batch = test_client.post('/meters/batches', json=initial, headers=_auth(analyst)).json()
    test_client.post(f"/meters/batches/{batch['id']}/publish", headers=_auth(analyst))

    backfills = [
        {'name': '并发补录A', 'readings': _rows([
            ('2026-05-01T08:00', 100), ('2026-05-02T08:00', 130),
            ('2026-05-03T08:00', 180), ('2026-05-04T08:00', 220),
            ('2026-05-05T08:00', 260), ('2026-05-06T08:00', 300)])},
        {'name': '并发补录B', 'readings': _rows([
            ('2026-05-01T08:00', 100), ('2026-05-02T08:00', 130),
            ('2026-05-03T08:00', 180), ('2026-05-04T08:00', 220),
            ('2026-05-05T08:00', 260), ('2026-05-06T08:00', 300)])},
    ]
    outcomes = []

    def worker(payload):
        response = test_client.post('/meters/batches', json=payload, headers=_auth(analyst))
        assert response.status_code == 201
        bid = response.json()['id']
        published = test_client.post(f'/meters/batches/{bid}/publish', headers=_auth(analyst))
        outcomes.append((bid, published.status_code))

    threads = [threading.Thread(target=worker, args=(p,)) for p in backfills]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    from src.models import MeterDaily, MeterBatch, MeterState
    with TestingSession() as session:
        # 两个并发发布：一个成功，另一个要么重试成功（前批被替代），要么 409；
        # 任何情况下每表每日唯一、无重复日耗。
        daily = list(session.scalars(select(MeterDaily)))
        assert len(daily) == 5
        assert len({(d.meter_id, d.day) for d in daily}) == 5
        assert len({d.generation for d in daily}) == 1  # 整组一次性切换，无旧代次残留
        assert round(sum(d.kwh for d in daily), 6) == 200  # 300 - 100
        state = session.scalar(select(MeterState).where(MeterState.meter_id == 'M1'))
        statuses = {b.id: b.status for b in session.scalars(select(MeterBatch))}
        published_batches = [bid for bid, status in statuses.items() if status == 'published']
        assert published_batches == [state.batch_id]
        assert len(published_batches) == 1
        # 至少一个并发请求走完了完整流程；冲突请求没有留下半成品
        assert any(code == 200 for _bid, code in outcomes)
        assert all(code in (200, 409) for _bid, code in outcomes)


def test_merge_endpoint_and_legacy_dataset_import_remain(client):
    test_client, _ = client
    analyst = _token(test_client, 'analyst@demo.local')
    # 原有日样本导入路径（DatasetInput 要求 5 条以上）
    legacy = {'name': '原生产批次', 'records': [
        {'day': f'2026-05-{day:02d}', 'line': '冲压P01', 'units': 800 + day,
         'run_hours': 12, 'temp_c': 25, 'idle_hours': 1, 'kwh': 999}
        for day in range(2, 7)]}
    assert test_client.post('/datasets', json=legacy, headers=_auth(analyst)).status_code == 201
    meter_batch = test_client.post('/meters/batches', json={
        'name': '合并发布批', 'readings': _rows([
            ('2026-05-01T08:00', 100), ('2026-05-02T08:00', 130), ('2026-05-03T08:00', 180)])},
        headers=_auth(analyst)).json()
    test_client.post(f"/meters/batches/{meter_batch['id']}/publish", headers=_auth(analyst))
    datasets = test_client.get('/datasets', headers=_auth(analyst)).json()
    base_id = datasets[0]['id']
    merged = test_client.post('/meters/merge', json={
        'name': '电表口径批次', 'dataset_id': base_id}, headers=_auth(analyst))
    assert merged.status_code == 201
    body = merged.json()
    assert body['rows'] == 2 and body['merge_meta']['merged_rows'] == 2
    detail = test_client.get(f"/datasets/{body['id']}", headers=_auth(analyst)).json()
    assert {r['kwh'] for r in detail['records']} == {30, 50}
