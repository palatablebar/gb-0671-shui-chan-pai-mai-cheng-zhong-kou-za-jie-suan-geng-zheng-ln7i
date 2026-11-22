"""M8 争议处理 HTTP 入口。

* GET  /dispute              争议处理页（核对称重版本、金额拆分、待补证据、待处理队列）
* POST /api/dispute          对当前批次纯计算争议处理单（不落库，可安全重跑）
* POST /api/disputes         提交争议并落库生成处理单（幂等：同指纹不重复占款/结付）
* POST /api/disputes/resend  重发处理单；失败恢复旧版，成功才切换
"""
from copy import deepcopy
from fastapi import APIRouter, Request, Body
from app.engine import evaluate
from app.errors import BusinessError
from app.web import render
from app import store
from app.modules.dispute.rules import classify, dispute_id

router = APIRouter()


def _db():
    # 延迟引用，便于测试 monkeypatch main.DB_PATH（与 web.render 同模式）
    from app.main import DB_PATH
    return DB_PATH


@router.get('/dispute')
def page(request: Request):
    return render(request, 'dispute')


@router.post('/api/dispute')
def calculate(payload: dict = Body(default={})):
    """无副作用试算：用于页面重跑核对，不写库、不占款。"""
    state = evaluate(payload, until=8)
    return state


def _load_run(run_id):
    return store.get(_db(), run_id)


def _validate_submission(payload):
    run_id = payload.get('run_id')
    if not isinstance(run_id, str) or not run_id.strip():
        raise BusinessError('BAD_DISPUTE', 'run_id 必填')
    disputes = payload.get('disputes')
    if not isinstance(disputes, list) or not disputes:
        raise BusinessError('BAD_DISPUTE', 'disputes 须为非空数组')
    for item in disputes:
        if not isinstance(item, dict) or not str(item.get('ticket', '')).strip():
            raise BusinessError('BAD_DISPUTE', '每条争议须包含票号')
    return run_id.strip(), disputes


@router.post('/api/disputes')
def submit(payload: dict = Body(...)):
    """提交争议、生成可执行处理单并持久化到批次。

    幂等键为争议指纹 dispute_id；同指纹重放直接返回已存在的处理单，
    不新增暂扣、不重复结付。待处理项只登记，不占款、不封存。
    """
    run_id, disputes = _validate_submission(payload)
    state = _load_run(run_id)
    expected_revision = payload.get('revision')
    if expected_revision is not None and expected_revision != state.get('revision'):
        # 乐观锁前置校验：即使请求内容是幂等重复，版本陈旧也必须让调用方刷新
        raise BusinessError('STALE_VERSION', '处理单版本已更新，请刷新后重试', 409)
    orders, pending = classify(state, disputes)
    # 纯计算先行：任何 BusinessError 都在写库前抛出，旧版天然不动
    existing = {o['dispute_id']: o for o in state.get('dispute_orders', [])}
    existing_pending = state.get('dispute_pending', [])
    known_pending = {_pending_key(p) for p in existing_pending}
    new_orders = []
    matched = []
    for order in orders:
        old = existing.get(order['dispute_id'])
        if old is not None:
            matched.append(order['dispute_id'])
        else:
            new_orders.append(order)
    new_pending = [p for p in pending if _pending_key(p) not in known_pending]
    if not new_orders and not new_pending:
        # 全部为重复到达：零写入，仅在响应内存对象上标记幂等命中
        for order in state.get('dispute_orders', []):
            if order['dispute_id'] in matched:
                order['idempotent_hit'] = True
        state['submit_note'] = '全部为已登记争议，未重复占款或结付'
        return state
    backup = deepcopy(state)
    next_state = deepcopy(state)
    next_state.setdefault('dispute_orders', [])
    next_state['dispute_orders'].extend(new_orders)
    next_state.setdefault('dispute_pending', [])
    next_state['dispute_pending'].extend(new_pending)
    next_state['dispute_summary'] = _summary(next_state['dispute_orders'])
    # 争议队列变化后重算封存裁决：有待处理项时不得封存
    from app.modules.archive.rules import apply as archive_apply
    archive_apply(next_state)
    try:
        saved = store.update_run(_db(), run_id, next_state,
                                 expected_revision=expected_revision)
    except BusinessError:
        # 乐观锁/未找到：数据库未发生写入，直接把业务错误交回调用方
        raise
    except Exception:
        # 写库失败：恢复旧版，调用方重试也不会看到半截处理单
        store.replace_run(_db(), run_id, backup)
        raise
    return saved


@router.post('/api/disputes/resend')
def resend(payload: dict = Body(...)):
    """处理单重发：先在旧版快照上重建，写库失败则恢复旧版。"""
    run_id = payload.get('run_id')
    dispute = payload.get('dispute_id')
    if not isinstance(run_id, str) or not isinstance(dispute, str):
        raise BusinessError('BAD_DISPUTE', 'run_id 与 dispute_id 必填')
    state = _load_run(run_id.strip())
    orders = state.get('dispute_orders', [])
    target = next((o for o in orders if o['dispute_id'] == dispute), None)
    if target is None:
        raise BusinessError('DISPUTE_NOT_FOUND', '处理单不存在，无法重发', 404)
    backup = deepcopy(state)
    next_state = deepcopy(state)
    for order in next_state['dispute_orders']:
        if order['dispute_id'] == dispute:
            order['resend_count'] = int(order.get('resend_count', 0)) + 1
            order['status'] = '已重发'
    try:
        return store.update_run(_db(), run_id.strip(), next_state,
                                expected_revision=payload.get('revision'))
    except BusinessError:
        raise
    except Exception:
        store.replace_run(_db(), run_id.strip(), backup)
        raise BusinessError('RESEND_ROLLED_BACK', '重发失败，已恢复旧版处理单')


def _pending_key(pending_row):
    # 待处理项按"票号+原因码+指纹原文"去重，重复到达只登记一次
    raw = pending_row.get('raw', {})
    fingerprint = dispute_id(pending_row.get('ticket', ''), raw.get('version', ''),
                             raw.get('claimed_amount', raw.get('claimed', '')),
                             pending_row.get('evidence_refs', []))
    return pending_row.get('code', ''), fingerprint


def _summary(orders):
    from app.modules.dispute.rules import _totals
    return _totals(orders)
