import io, csv, json, zipfile, hashlib
from app.errors import BusinessError

def serialize(result):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',json.dumps({**result['manifest'],'revision':result.get('revision',1),'status':result.get('status','待复核')},ensure_ascii=False,indent=2))
        archive.writestr('evidence.json',json.dumps({'counts':result['counts'],'views':result['views'],'quarantine':result['quarantine']},ensure_ascii=False,indent=2))
        # 争议证据单独成档；原始称重、成交与付款证据仍在 evidence.json 中，只新增不覆盖
        if result.get('dispute_orders') or result.get('dispute_pending'):
            archive.writestr('disputes.json',json.dumps({
                'orders':result.get('dispute_orders',[]),
                'pending':result.get('dispute_pending',[]),
                'summary':result.get('dispute_summary',{})},ensure_ascii=False,indent=2))
    return buffer.getvalue()
