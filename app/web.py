from fastapi import Request
from fastapi.templating import Jinja2Templates
from app.config import ROOT, META
from app.engine import evaluate
from app import store

templates=Jinja2Templates(directory=str(ROOT/'app/templates'))

def render(request:Request,key):
    from app.main import DB_PATH
    run_id=request.query_params.get('run_id')
    if run_id:
        state=store.get(DB_PATH,run_id)
    else:
        state=store.save(DB_PATH,evaluate({'dataset':request.query_params.get('dataset','dirty')}))
    view=state['views'].get(key)
    if view is None:
        # 旧批次在争议模块上线前封存：不改动其证据，回退到当前规则重算视图
        state=store.save(DB_PATH,evaluate({'dataset':state.get('source','dirty')},
                                          snapshot=state.get('input_snapshot')))
        view=state['views'][key]
    return templates.TemplateResponse(request=request,name=f'{key}.html',context={'meta':META,'key':key,'state':state,'view':view})
