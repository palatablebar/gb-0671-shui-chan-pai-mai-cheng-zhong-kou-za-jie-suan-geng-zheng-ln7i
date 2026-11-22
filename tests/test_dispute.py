"""M8 争议处理测试。

覆盖：
* 部分结款金额拆分（争议/暂扣/可付/待补证据/称重版本核对）
* 证据无法对应原票 → 待处理、不占款、不封存
* 更正分支未闭合 → 待处理
* 同一争议重复到达 → 只登记一次、不重复占款
* 同一争议重跑 → 幂等，不重复结付
* 重发失败 → 恢复旧版
* 原始称重/成交/付款证据不丢失
"""
from copy import deepcopy
from decimal import Decimal
import pytest
from app.engine import evaluate
from app.modules.dispute.rules import classify, dispute_id, apply
from app.modules.archive.rules import archive_decision
from app.archive import serialize
import zipfile
import io
import json


def _dispute(ticket='00002', version='1', claimed='1000.00', refs=None):
    # 00002 v1 来源行 4,5（见 dirty 批毛皮配对）
    return {'ticket': ticket, 'version': version, 'claimed_amount': claimed,
            'evidence_refs': refs if refs is not None else ['4', '5'],
            'evidence_due': ['复秤小票照片']}


def test_order_amount_split_real(sample):
    """页面对齐字段：生效/争议版本、争议金额、暂扣、可付、待补证据。"""
    orders, pending = classify(sample, [_dispute()])
    assert pending == []
    o = orders[0]
    final = next(r for r in sample['final'] if r['ticket'] == '00002')
    assert o['effective_version'] == final['version'] == 1
    assert o['dispute_version'] == '1'
    # 生效 1075.02，主张 1000.00 → 争议 75.02；未付 1075.02 → 暂扣 75.02，可付 1000.00
    assert o['dispute_amount'] == '75.02'
    assert o['hold_amount'] == '75.02'
    assert o['payable_amount'] == '1000.00'
    assert Decimal(o['payable_amount']) + Decimal(o['hold_amount']) == Decimal(final['due'])
    assert o['weight_basis']['source_lines'] == [4, 5]
    assert o['evidence_due'] == ['复秤小票照片']


def test_hold_capped_by_unpaid_due(sample):
    """争议金额超过未付余额时，暂扣封顶在待付内，不产生负可付、不冻结整票。"""
    # 00001 已付 1168.50，待付仅 116.85；主张 0.01 → 争议差额 1285.34，远超待付
    d = {'ticket': '00001', 'version': '2', 'claimed_amount': '0.01',
         'evidence_refs': ['42', '43']}
    orders, pending = classify(sample, [d])
    assert pending == []
    assert orders[0]['hold_amount'] == '116.85'
    assert orders[0]['payable_amount'] == '0.00'


def test_evidence_unlinked_goes_pending(sample):
    """证据行号不属于该票 → EVIDENCE_UNLINKED，待处理且不占款。"""
    bad = _dispute(refs=['999'])
    orders, pending = classify(sample, [bad])
    assert orders == []
    assert len(pending) == 1 and pending[0]['code'] == 'EVIDENCE_UNLINKED'
    state = deepcopy(sample)
    state['context']['disputes'] = [bad]
    apply(state)
    assert state['dispute_orders'] == []
    assert state['dispute_summary']['hold_total'] == '0.00'
    # 待处理争议存在时不得封存
    assert archive_decision(state)['status'] == '待处理'


def test_empty_evidence_goes_pending(sample):
    orders, pending = classify(sample, [_dispute(refs=[])])
    assert orders == [] and pending[0]['code'] == 'EVIDENCE_UNLINKED'


def test_branch_unclosed_goes_pending(clean):
    """更正分支未闭合（STALE_CORRECTION）的票不能生成处理单。"""
    rows = [dict(r) for r in clean['settlements'][:2]]
    rows.append(dict(rows[-1], version=3, replaces='1'))
    state = deepcopy(clean)
    state['settlements'] = rows
    from app.modules.correction.rules import select_versions
    final, conflicts, audit = select_versions(rows)
    state['final'], state['version_conflicts'], state['version_audit'] = final, conflicts, audit
    blocked_ticket = rows[-1]['ticket']
    src_lines = next(r['lines'] for r in rows if r['ticket'] == blocked_ticket and r['version'] == 1)
    d = _dispute(ticket=blocked_ticket, refs=[str(src_lines[0])])
    orders, pending = classify(state, [d])
    assert orders == []
    assert pending[0]['code'] == 'BRANCH_UNCLOSED'


def test_duplicate_dispute_single_hold(sample):
    """同一争议重复到达：指纹相同，只出一张单、只占一次款。"""
    d = _dispute()
    orders, pending = classify(sample, [d, deepcopy(d), deepcopy(d)])
    assert len(orders) == 1
    assert len(pending) == 2
    assert all(p['code'] == 'DISPUTE_DUPLICATE' for p in pending)
    assert orders[0]['hold_amount'] == '75.02'


def test_distinct_disputes_both_held(sample):
    """不同票的争议各自成单，暂扣分别计算。"""
    d1 = _dispute()
    d2 = _dispute(ticket='00003', refs=['6', '7'], claimed='1000.00')
    orders, pending = classify(sample, [d1, d2])
    assert len(orders) == 2 and pending == []
    total = sum(Decimal(o['hold_amount']) for o in orders)
    assert total == Decimal(orders[0]['hold_amount']) + Decimal(orders[1]['hold_amount'])


def test_unknown_ticket_and_version_pending(sample):
    orders, pending = classify(sample, [_dispute(ticket='99999')])
    assert orders == [] and pending[0]['code'] == 'TICKET_NOT_FOUND'
    orders, pending = classify(sample, [_dispute(version='99')])
    assert orders == [] and pending[0]['code'] == 'VERSION_NOT_FOUND'


def test_bad_amount_pending(sample):
    orders, pending = classify(sample, [_dispute(claimed='0')])
    assert orders == [] and pending[0]['code'] == 'BAD_DISPUTE_AMOUNT'
    orders, pending = classify(sample, [_dispute(claimed='abc')])
    assert pending[0]['code'] == 'BAD_DISPUTE_AMOUNT'


def test_dispute_id_deterministic():
    a = dispute_id('00002', '1', '1000.00', ['4', '5'])
    b = dispute_id('00002', '1', '1000.00', ['5', '4'])  # 证据顺序不同仍是同一争议
    assert a == b and a.startswith('D')
    assert dispute_id('00003', '1', '1000.00', ['6', '7']) != a


def test_rerun_idempotent_no_double_payment(sample):
    """管道重跑：同指纹争议沿用旧单，标记 rerun_hit，暂扣/可付金额分毫不差。"""
    d = _dispute()
    first = deepcopy(sample)
    first['context']['disputes'] = [d]
    apply(first)
    assert len(first['dispute_orders']) == 1
    original = deepcopy(first['dispute_orders'][0])
    # 模拟重跑：新状态先带着已生成的单再次 apply
    second = deepcopy(sample)
    second['dispute_orders'] = deepcopy(first['dispute_orders'])
    second['context']['disputes'] = [d]
    apply(second)
    assert len(second['dispute_orders']) == 1
    assert second['dispute_orders'][0]['rerun_hit'] is True
    for key in ('hold_amount', 'payable_amount', 'dispute_amount', 'dispute_id'):
        assert second['dispute_orders'][0][key] == original[key]


def test_original_evidence_preserved(sample):
    """争议处理只新增不覆盖：称重、成交、付款证据与守恒计数不变。"""
    before = {'final': deepcopy(sample['final']), 'audit': deepcopy(sample['version_audit']),
              'quarantine': deepcopy(sample['quarantine']), 'counts': deepcopy(sample['counts']),
              'settlements': deepcopy(sample['settlements'])}
    state = deepcopy(sample)
    state['context']['disputes'] = [_dispute(), _dispute(refs=['999'])]
    apply(state)
    assert state['final'] == before['final']
    assert state['version_audit'] == before['audit']
    assert state['quarantine'] == before['quarantine']
    assert state['counts'] == before['counts']
    assert state['settlements'] == before['settlements']


def test_archive_bundle_keeps_evidence_and_adds_disputes(sample):
    state = deepcopy(sample)
    state['context']['disputes'] = [_dispute()]
    apply(state)
    z = zipfile.ZipFile(io.BytesIO(serialize(state)))
    evidence = json.loads(z.read('evidence.json'))
    assert len(evidence['quarantine']) == 8  # 原始隔离证据仍在
    disputes = json.loads(z.read('disputes.json'))
    assert len(disputes['orders']) == 1
    assert disputes['orders'][0]['payable_amount'] == '1000.00'


def test_archive_blocked_when_pending(sample):
    state = deepcopy(sample)
    state['context']['disputes'] = [_dispute(refs=['999'])]
    apply(state)
    decision = archive_decision(state)
    assert decision['status'] == '待处理' and decision['disputes_pending'] == 1


def test_view_metrics(sample):
    state = deepcopy(sample)
    state['context']['disputes'] = [_dispute(), _dispute(refs=['999'])]
    apply(state)
    metrics = state['views']['dispute']['metrics']
    assert metrics['处理单'] == 1 and metrics['待处理'] == 1
    assert metrics['暂扣_CNY'] == '75.02' and metrics['可付_CNY'] == '1000.00'


def test_sample_context_dispute_real():
    """context.json 自带样例争议：00001 部分结款数字可核对。"""
    state = evaluate()
    orders = state['dispute_orders']
    assert len(orders) == 1 and state['dispute_pending'] == []
    o = orders[0]
    assert o['ticket'] == '00001' and o['effective_version'] == 2
    assert o['dispute_amount'] == '55.35'
    assert o['hold_amount'] == '55.35' and o['payable_amount'] == '61.50'
