"""M8 争议处理 HTTP 与持久化测试：幂等提交、乐观锁、重发回滚。"""
from fastapi.testclient import TestClient
from app import main, store
from app.engine import evaluate
from copy import deepcopy
import json
import pytest


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'test.sqlite'))
    return TestClient(main.app)


def _payload(rid, revision=1):
    return {'run_id': rid, 'revision': revision, 'disputes': [
        {'ticket': '00002', 'version': '1', 'claimed_amount': '1000.00',
         'evidence_refs': ['4', '5'], 'evidence_due': ['复秤小票照片']}]}


def test_submit_creates_order_and_persists(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset':'clean'}).json()['manifest']['run_id']
    r = c.post('/api/disputes', json=_payload(rid))
    assert r.status_code == 200
    data = r.json()
    assert len(data['dispute_orders']) == 1
    assert data['dispute_orders'][0]['payable_amount'] == '1000.00'
    # 落库可查，且 revision 已推进
    saved = c.get('/api/runs/' + rid).json()
    assert saved['revision'] == 2 and saved['dispute_orders'][0]['hold_amount'] == '75.02'


def test_submit_idempotent_no_double_hold(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset':'clean'}).json()['manifest']['run_id']
    first = c.post('/api/disputes', json=_payload(rid)).json()
    did = first['dispute_orders'][0]['dispute_id']
    second = c.post('/api/disputes', json=_payload(rid, revision=first['revision']))
    assert second.status_code == 200
    data = second.json()
    assert len(data['dispute_orders']) == 1
    assert data['dispute_orders'][0]['dispute_id'] == did
    assert data['dispute_orders'][0].get('idempotent_hit') is True
    assert data['dispute_summary']['hold_total'] == '75.02'  # 未重复占款


def test_pending_dispute_does_not_hold(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset':'clean'}).json()['manifest']['run_id']
    payload = {'run_id': rid, 'disputes': [
        {'ticket': '00002', 'version': '1', 'claimed_amount': '1000.00',
         'evidence_refs': ['999']}]}
    r = c.post('/api/disputes', json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data['dispute_orders'] == []
    assert data['dispute_pending'][0]['code'] == 'EVIDENCE_UNLINKED'
    # 封存被拦截
    assert data['archive']['status'] == '待处理'


def test_stale_revision_rejected_without_write(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset':'clean'}).json()['manifest']['run_id']
    assert c.post('/api/disputes', json=_payload(rid, revision=1)).status_code == 200
    # 旧版本号重放 → 409，且不新增处理单
    r = c.post('/api/disputes', json=_payload(rid, revision=1))
    assert r.status_code == 409 and r.json()['code'] == 'STALE_VERSION'
    saved = c.get('/api/runs/' + rid).json()
    assert len(saved['dispute_orders']) == 1


def test_resend_failure_restores_old_version(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset':'clean'}).json()['manifest']['run_id']
    created = c.post('/api/disputes', json=_payload(rid)).json()
    did = created['dispute_orders'][0]['dispute_id']
    before = c.get('/api/runs/' + rid).json()
    assert before['dispute_orders'][0].get('resend_count', 0) == 0
    # 令写库失败：update_run 抛错，handler 必须用备份恢复旧版
    original_update = store.update_run
    def failing_update(*a, **k):
        raise RuntimeError('disk full')
    monkeypatch.setattr(store, 'update_run', failing_update)
    r = c.post('/api/disputes/resend',
               json={'run_id': rid, 'dispute_id': did, 'revision': before['revision']})
    assert r.status_code == 422
    assert r.json()['code'] == 'RESEND_ROLLED_BACK'
    # 只恢复 update_run；不能用 monkeypatch.undo()，否则 main.DB_PATH 也会被还原
    monkeypatch.setattr(store, 'update_run', original_update)
    after = c.get('/api/runs/' + rid).json()
    assert after['dispute_orders'][0].get('resend_count', 0) == 0
    assert after['dispute_orders'][0]['status'] == '待处理单'


def test_resend_success_advances_counter(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset':'clean'}).json()['manifest']['run_id']
    created = c.post('/api/disputes', json=_payload(rid)).json()
    did = created['dispute_orders'][0]['dispute_id']
    r = c.post('/api/disputes/resend',
               json={'run_id': rid, 'dispute_id': did, 'revision': created['revision']})
    assert r.status_code == 200
    order = next(o for o in r.json()['dispute_orders'] if o['dispute_id'] == did)
    assert order['resend_count'] == 1 and order['status'] == '已重发'


def test_resend_unknown_dispute_404(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset':'clean'}).json()['manifest']['run_id']
    r = c.post('/api/disputes/resend',
               json={'run_id': rid, 'dispute_id': 'DNOPE', 'revision': 1})
    assert r.status_code == 404


def test_original_evidence_intact_after_submit(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    original = c.post('/api/runs', json={'dataset':'clean'}).json()
    rid = original['manifest']['run_id']
    c.post('/api/disputes', json=_payload(rid))
    saved = c.get('/api/runs/' + rid).json()
    assert saved['final'] == original['final']
    assert saved['version_audit'] == original['version_audit']
    assert saved['quarantine'] == original['quarantine']
    assert saved['counts'] == original['counts']
    # 封存包内原始证据与争议档案并存
    zbytes = c.get('/api/export/' + rid).content
    import zipfile, io
    z = zipfile.ZipFile(io.BytesIO(zbytes))
    assert 'evidence.json' in z.namelist() and 'disputes.json' in z.namelist()


def test_dispute_page_renders(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset':'clean'}).json()['manifest']['run_id']
    html = c.get('/dispute?run_id=' + rid).text
    assert '可执行争议处理单' in html and '待处理队列' in html
    # 导航包含第八个模块
    assert '/dispute?run_id=' + rid in html


def test_validation_errors(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    assert c.post('/api/disputes', json={'run_id': '', 'disputes': []}).status_code == 422
    rid = c.post('/api/runs', json={'dataset':'clean'}).json()['manifest']['run_id']
    assert c.post('/api/disputes', json={'run_id': rid, 'disputes': [{}]}).status_code == 422
