"""On-demand, read-only account checks. Secrets never enter UI state."""
import threading,time
import bpy
from bpy.props import StringProperty
from .runtime import get_runtime
from .workbench_controls import tr
_states={}

def status(provider,profile): return _states.get((provider,profile),{})

def check(runtime,provider,profile):
    key=(provider,profile)
    if _states.get(key,{}).get('state')=='checking': return
    if not provider.startswith('tripo_'):
        _states[key]={'state':'unsupported','at':time.time()};return
    _states[key]={'state':'checking','at':time.time()}
    def worker():
        try:
            adapter=runtime.service.providers[provider]
            response=adapter._http.request_json('GET',adapter.base_url+'/account/balance',token=runtime.credentials.use(provider,profile))
            if response.get('code')!=0: raise ValueError('invalid account response')
            balance=response.get('data',{}).get('balance')
            if not isinstance(balance,(int,float)): raise ValueError('missing balance')
            _states[key]={'state':'verified','balance':balance,'at':time.time()}
        except Exception as exc:
            code=getattr(exc,'code','unavailable')
            _states[key]={'state':'failed','code':code,'at':time.time()}
    threading.Thread(target=worker,name='meshdock-account-check',daemon=True).start()

def caption(provider,profile):
    state=status(provider,profile)
    if state.get('state')=='verified': return tr('已验证 · ','Verified · ')+str(state['balance'])+tr(' 积分',' credits')
    if state.get('state')=='checking': return tr('正在检测…','Checking…')
    if state.get('state')=='failed':
        return tr('密钥无效，请重新添加','Invalid key; add again') if state.get('code')=='provider_authentication_failed' else tr('检测失败，请检查网络或账号','Check failed; check network or account')
    return tr('未验证 · 请到平台确认','Not verified · check provider') if not provider.startswith('tripo_') else tr('检测授权与余额','Check access & balance')

class MESHDOCK_OT_check_account(bpy.types.Operator):
    bl_idname='meshdock.check_account'
    bl_label='Check account'
    provider: StringProperty()
    profile_id: StringProperty()
    def execute(self,context):
        check(get_runtime(),self.provider,self.profile_id)
        return {'FINISHED'}
CLASSES=(MESHDOCK_OT_check_account,)
