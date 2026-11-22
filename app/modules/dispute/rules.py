"""M8 争议处理规则。

拍卖后买家对一票的复秤扣杂提出争议时，按生效版先结整票会让卖家当日拿到钱，
但争议部分之后可能变成追偿；整票冻结虽不误付，却拖住已核实货款。
本模块生成"部分结款"的可执行争议处理单：

* 可付金额 = 生效版应付 − 累计已付 − 暂扣金额，已核实货款当日可付；
* 暂扣金额 = 争议金额（买家主张与生效版的差额绝对值，封顶在未付余额内）；
* 证据无法对应原票、更正分支未闭合、同一争议重复到达时只保留待处理，
  不生成处理单、不占款、不封存；
* 争议以确定性指纹识别，同一争议重跑不重复占款或重复结付；
* 原始称重、成交与付款证据全程只读，只新增不覆盖。
"""
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
from app.coerce import money
from app.errors import BusinessError

# 待处理原因码 → 说明
PENDING_CODES = {
    'EVIDENCE_UNLINKED': '争议证据无法对应原票',
    'BRANCH_UNCLOSED': '更正分支未闭合，生效版无法确认',
    'DISPUTE_DUPLICATE': '同一争议重复到达',
    'BAD_DISPUTE_AMOUNT': '争议金额缺失或非正数',
    'TICKET_NOT_FOUND': '争议票号不在生效票内',
    'VERSION_NOT_FOUND': '争议依据版本不属于该票',
}


def dispute_id(ticket, version, claimed, evidence_refs):
    """争议指纹：同输入重跑得到相同编号，作为幂等键。"""
    payload = json.dumps(
        {'ticket': ticket, 'version': str(version), 'claimed': str(claimed),
         'evidence': sorted(str(r) for r in evidence_refs)},
        ensure_ascii=False, sort_keys=True)
    return 'D' + hashlib.sha256(payload.encode()).hexdigest()[:12].upper()


def _as_decimal(value, code='BAD_DISPUTE_AMOUNT'):
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        raise BusinessError(code, PENDING_CODES[code])
    if not result.is_finite() or result <= 0:
        raise BusinessError(code, PENDING_CODES[code])
    return result


def _branch_open_tickets(state):
    """更正分支未闭合的票：correction 模块已在 version_conflicts 标注。"""
    return {c['ticket'] for c in state.get('version_conflicts', [])}


def _version_index(state):
    """票号 → 该票全部计价版本（含未生效），用于核对争议依据版本与称重来源。"""
    grouped = defaultdict(list)
    for row in state.get('settlements', []):
        grouped[row['ticket']].append(row)
    return grouped


def classify(state, disputes):
    """把到达的争议分成可处理单与待处理队列。纯函数，便于单测。"""
    finals = {r['ticket']: r for r in state['final']}
    versions = _version_index(state)
    open_branch = _branch_open_tickets(state)
    seen = {}
    orders, pending = [], []
    for index, raw in enumerate(disputes):
        item = deepcopy(raw) if isinstance(raw, dict) else {}
        ticket = str(item.get('ticket', '')).strip()
        evidence_refs = [str(r).strip() for r in item.get('evidence_refs', []) if str(r).strip()]
        claimed = item.get('claimed_amount', item.get('claimed', ''))
        try:
            claimed_dec = _as_decimal(claimed)
        except BusinessError as exc:
            pending.append(_pending(item, index, exc.code, exc.message, evidence_refs, ticket))
            continue
        did = dispute_id(ticket, item.get('version', ''), claimed, evidence_refs)
        item['dispute_id'] = did
        # 同一争议重复到达：指纹相同的后续条目只保留待处理，不重复占款
        if did in seen:
            pending.append(_pending(item, index, 'DISPUTE_DUPLICATE',
                                    PENDING_CODES['DISPUTE_DUPLICATE'], evidence_refs, ticket,
                                    duplicate_of=seen[did]))
            continue
        seen[did] = index
        # 分支未闭合优先判定：该票不在生效票内正是因为更正链断裂
        if ticket in open_branch:
            pending.append(_pending(item, index, 'BRANCH_UNCLOSED',
                                    PENDING_CODES['BRANCH_UNCLOSED'], evidence_refs, ticket))
            continue
        if ticket not in finals:
            pending.append(_pending(item, index, 'TICKET_NOT_FOUND',
                                    PENDING_CODES['TICKET_NOT_FOUND'], evidence_refs, ticket))
            continue
        ticket_versions = {str(r['version']) for r in versions.get(ticket, [])}
        version = str(item.get('version', '')).strip()
        if version not in ticket_versions:
            pending.append(_pending(item, index, 'VERSION_NOT_FOUND',
                                    PENDING_CODES['VERSION_NOT_FOUND'], evidence_refs, ticket))
            continue
        if not _evidence_linked(state, ticket, evidence_refs):
            pending.append(_pending(item, index, 'EVIDENCE_UNLINKED',
                                    PENDING_CODES['EVIDENCE_UNLINKED'], evidence_refs, ticket))
            continue
        orders.append(_build_order(state, item, ticket, version, claimed_dec, evidence_refs, index))
    return orders, pending


def _pending(item, index, code, detail, evidence_refs, ticket, duplicate_of=None):
    row = {'arrival_index': index, 'ticket': ticket, 'code': code, 'detail': detail,
           'evidence_refs': evidence_refs, 'raw': {k: v for k, v in item.items()
                                                   if k not in ('evidence_refs',)}}
    if duplicate_of is not None:
        row['duplicate_of_index'] = duplicate_of
    return row


def _evidence_linked(state, ticket, refs):
    """证据必须能对应到原票：引用该票任一计价版本的原始称重行号。"""
    if not refs:
        return False
    valid = set()
    for row in state.get('settlements', []):
        if row['ticket'] == ticket:
            valid.update(str(line) for line in row.get('lines', []))
    return all(str(r) in valid for r in refs)


def _build_order(state, item, ticket, version, claimed_dec, evidence_refs, index):
    """生成一张可执行争议处理单（部分结款）。"""
    final = next(r for r in state['final'] if r['ticket'] == ticket)
    amount = Decimal(final['amount'])
    paid = Decimal(final['paid'])
    due = amount - paid
    # 争议金额：买家主张应付与当前生效应付的差额；若直接给差额则按差额解释
    raw_claim = str(item.get('claimed_amount', item.get('claimed', '')))
    claim_value = Decimal(str(raw_claim))
    if item.get('claim_type') == 'difference':
        difference = claim_value
    else:
        difference = abs(amount - claim_value)
    difference = Decimal(money(difference))
    # 暂扣不超过未付余额，避免整票冻结拖住已核实货款，也不产生负可付
    hold = min(difference, max(due, Decimal('0')))
    hold = Decimal(money(hold))
    payable = due - hold
    version_row = next(r for r in state['settlements']
                       if r['ticket'] == ticket and str(r['version']) == version)
    return {
        'dispute_id': item['dispute_id'],
        'arrival_index': index,
        'ticket': ticket,
        'status': '待处理单',
        'effective_version': final['version'],
        'dispute_version': version,
        'weight_basis': {
            'gross_kg': version_row['gross_kg'], 'tare_kg': version_row['tare_kg'],
            'net_kg': version_row['net_kg'], 'billable_kg': version_row['billable_kg'],
            'rate': version_row['rate'], 'price': version_row['price'],
            'source_lines': version_row['lines'],
        },
        'effective_amount': money(amount),
        'claimed_amount': money(claim_value),
        'dispute_amount': money(difference),
        'paid_amount': money(paid),
        'hold_amount': money(hold),
        'payable_amount': money(payable),
        'evidence_refs': evidence_refs,
        'evidence_due': item.get('evidence_due', []),
        'note': item.get('note', ''),
    }


def apply(state):
    """管道内应用：读取 context.disputes（样例自带或 overrides 传入）。

    已结付/已占款记录通过 state 传入时（重跑场景），同指纹争议直接复用旧处理单，
    不重新占款；这是幂等重跑的管道侧入口，HTTP 侧的持久化幂等见 handler/store。
    """
    disputes = state.get('context', {}).get('disputes', [])
    if not isinstance(disputes, list):
        raise BusinessError('BAD_CONTEXT', '争议清单须为数组')
    prior = {o['dispute_id']: o for o in state.get('dispute_orders', [])}
    orders, pending = classify(state, disputes)
    merged = []
    for order in orders:
        old = prior.get(order['dispute_id'])
        if old is not None:
            # 同一争议重跑：沿用旧单号与旧占款，标记重跑命中，绝不重复结付
            reused = deepcopy(old)
            reused['rerun_hit'] = True
            merged.append(reused)
        else:
            merged.append(order)
    state['dispute_orders'] = merged
    state['dispute_pending'] = pending
    totals = _totals(merged)
    state['dispute_summary'] = totals
    state['views']['dispute'] = {
        'title': '争议处理',
        'rows': merged,
        'pending_rows': pending,
        'metrics': {
            '处理单': len(merged), '待处理': len(pending),
            '争议_CNY': totals['dispute_total'],
            '暂扣_CNY': totals['hold_total'],
            '可付_CNY': totals['payable_total'],
        },
    }


def _totals(orders):
    dispute_total = sum((Decimal(o['dispute_amount']) for o in orders), Decimal(0))
    hold_total = sum((Decimal(o['hold_amount']) for o in orders), Decimal(0))
    payable_total = sum((Decimal(o['payable_amount']) for o in orders), Decimal(0))
    return {'dispute_total': money(dispute_total), 'hold_total': money(hold_total),
            'payable_total': money(payable_total), 'orders': len(orders)}
