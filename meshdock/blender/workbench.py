"""Graphical AI workbench over Blender's real 3D viewport.

Hit regions are regenerated from the same layout used for drawing. Native Blender
popovers handle text editing and provider-specific controls (including IME input).
"""
import math
import json
import time
import traceback
import bpy
from bpy.app.handlers import persistent
import blf
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Quaternion, Vector
from .runtime import get_runtime
from .workbench_controls import tr, generation_blocker, generation_constraints
from . import workbench_preview as preview

_enabled = False
_handler = None
_sessions = {}
_textures = {}
_draw_errors = []
BG = (0.075, 0.083, 0.10, 1)
PANEL = (0.105, 0.115, 0.14, 1)
CARD = (0.15, 0.165, 0.195, 1)
HOVER = (0.22, 0.24, 0.29, 1)
ACCENT = (0.43, 0.34, 0.95, 1)
TEXT = (0.92, 0.94, 0.98, 1)
MUTED = (0.61, 0.65, 0.72, 1)

# Compact type and spacing scale shared by every workbench page. Keeping these
# values together prevents the GPU UI from drifting as new providers are added.
TYPE_BRAND = 18
TYPE_PAGE = 16
TYPE_BODY = 13
TYPE_CAPTION = 11
ROW = 36
ROW_SMALL = 30
GAP_XS = 6
GAP_SM = 10
GAP_MD = 16
GAP_LG = 24


class Canvas:
    def __init__(self, region, session):
        self.s = max(1.0, bpy.context.preferences.system.ui_scale, region.width / 1800) * 1.05
        # WINDOW overlaps Blender's transparent header. Reserve its actual height.
        top = region.y + region.height
        overlap = [r.height for r in bpy.context.area.regions
                   if r.type in {'HEADER', 'TOOL_HEADER'} and r.height > 1
                   and r.y >= top - r.height - 2]
        self.inset = (sum(overlap) + 8) / self.s
        self.w, self.h = region.width / self.s, region.height / self.s - self.inset
        self.session = session
        self.hits = []
        self.clip = None

    def box(self, x, y, w, h, color=CARD, radius=8):
        if w <= 0 or h <= 0:
            return
        r = min(radius, w/2, h/2)
        vertices = []
        for cx, cy, start in [(x+r,y+r,180),(x+w-r,y+r,270),
                              (x+w-r,y+h-r,0),(x+r,y+h-r,90)]:
            for i in range(7):
                angle=math.radians(start+i*15)
                vertices.append(((cx+r*math.cos(angle))*self.s,
                                 (self.h-cy-r*math.sin(angle))*self.s))
        shader=gpu.shader.from_builtin('UNIFORM_COLOR')
        shader.bind(); shader.uniform_float('color',color)
        batch_for_shader(shader,'TRI_FAN',{'pos':vertices}).draw(shader)

    def text(self, x, y, text, size=14, color=TEXT, width=None):
        blf.size(0,size*self.s)
        text=str(text)
        if width is not None:
            while text and blf.dimensions(0,text)[0] > width*self.s:
                text=text[:-2]+'…' if len(text)>1 else ''
        blf.color(0,*color)
        blf.position(0,x*self.s,(self.h-y-size)*self.s,0)
        blf.draw(0,text)

    def button(self, x, y, w, h, text, action, tip='', active=False, enabled=True, center=False, size=TYPE_BODY):
        hovered = self.session.hover == action
        color = ACCENT if active and enabled else HOVER if hovered and enabled else CARD
        if hovered and enabled:
            self.box(x-2,y-2,w+4,h+4,(0.60,0.55,0.96,1))
        self.box(x,y,w,h,color)
        padding = min(12, w * 0.17)
        if center:
            self.center_text(x,y,w,h,text,size,TEXT if enabled else MUTED)
        else:
            self.text(x+padding,y+(h-size)/2-1,text,size,TEXT if enabled else MUTED,width=w-padding*2)
        if enabled and (self.clip is None or (y>=self.clip[0] and y+h<=self.clip[1])):
            self.hits.append((x,y,w,h,action,tip or text))

    def hit(self,x,y,w,h,action,tip):
        if self.clip is not None:
            bottom=min(y+h,self.clip[1]);y=max(y,self.clip[0]);h=bottom-y
        if h>0 and w>0:
            self.hits.append((x,y,w,h,action,tip))

    def center_text(self,x,y,w,h,text,size=14,color=TEXT):
        blf.size(0,size*self.s)
        text=str(text)
        while text and blf.dimensions(0,text)[0]>(w-8)*self.s:
            text=text[:-2]+'…' if len(text)>1 else ''
        tw,th=blf.dimensions(0,text)
        blf.color(0,*color)
        blf.position(0,(x+w/2)*self.s-tw/2,(self.h-y-h/2)*self.s-th/2,0)
        blf.draw(0,text)

    def icon(self, x, y, name, size=18, color=TEXT):
        paths = {
            'image': [[(0,0),(1,0),(1,1),(0,1),(0,0)],[(0,.8),(.35,.45),(.6,.7),(.8,.4),(1,.65)]],
            'multiview': [[(.2,0),(1,0),(1,.8)],[(0,.2),(.8,.2),(.8,1),(0,1),(0,.2)]],
            'text': [[(0,.1),(1,.1)],[(.5,.1),(.5,1)],[(.25,1),(.75,1)]],
            'generate': [[(.5,0),(.62,.37),(1,.5),(.62,.63),(.5,1),(.38,.63),(0,.5),(.38,.37),(.5,0)]],
            'process': [[(.1,.9),(.85,.15)],[(.55,.1),(.9,.1),(.9,.45)]],
            'platform': [[(0,.25),(.5,0),(1,.25),(0,.25)],[(.15,.35),(.15,.8)],[(.5,.35),(.5,.8)],[(.85,.35),(.85,.8)],[(0,1),(1,1)]],
            'history': [[(.15,.2),(.5,0),(.85,.2),(1,.5),(.85,.85),(.5,1),(.15,.85),(0,.5),(0,.15)],[(0,.4),(.3,.4)],[(.5,.2),(.5,.55),(.75,.55)]],
            'animate': [[(.25,0),(.9,.5),(.25,1),(.25,0)]],
        }
        shader=gpu.shader.from_builtin('UNIFORM_COLOR')
        shader.bind(); shader.uniform_float('color',color)
        for path in paths.get(name, paths['generate']):
            points=[((x+a*size)*self.s,(self.h-y-b*size)*self.s) for a,b in path]
            batch_for_shader(shader,'LINE_STRIP',{'pos':points}).draw(shader)

    def picture(self, image, x, y, w, h):
        if not image or not image.size[0] or not image.size[1]:
            return
        factor=min(w/image.size[0],h/image.size[1])
        iw,ih=image.size[0]*factor,image.size[1]*factor
        x+=(w-iw)/2; y+=(h-ih)/2
        key=image.as_pointer()
        try:
            texture=_textures.get(key)
            if texture is None:
                texture=gpu.texture.from_image(image); _textures[key]=texture
            # Blender 5+ image textures are scene-linear; convert for the UI framebuffer.
            shader=gpu.shader.from_builtin('IMAGE_SCENE_LINEAR_TO_REC709_SRGB')
            shader.bind(); shader.uniform_sampler('image',texture)
            positions=[(x*self.s,(self.h-y-ih)*self.s),((x+iw)*self.s,(self.h-y-ih)*self.s),
                       ((x+iw)*self.s,(self.h-y)*self.s),(x*self.s,(self.h-y)*self.s)]
            batch_for_shader(shader,'TRI_FAN',{'pos':positions,'texCoord':[(0,0),(1,0),(1,1),(0,1)]}).draw(shader)
        except (ReferenceError,RuntimeError):
            _textures.pop(key,None)

    def scissor(self,x,y,w,h):
        gpu.state.scissor_test_set(True)
        gpu.state.scissor_set(int(x*self.s),int((self.h-y-h)*self.s),int(w*self.s),int(h*self.s))
        self.clip=(y,y+h)

    def unclip(self):
        gpu.state.scissor_test_set(False)
        self.clip=None


def draw():
    context=bpy.context
    if not _enabled or not context.area or context.area.type!='VIEW_3D':
        return
    if not context.workspace or not context.workspace.get('meshdock_workbench'):
        return
    session=_sessions.get(context.window.as_pointer())
    if session is None:
        return
    try:
        gpu.state.blend_set('ALPHA')
        canvas=Canvas(context.region,session)
        session.paint(canvas,context)
        session.hits=canvas.hits
        session.scale=canvas.s
        session.inset=canvas.inset
    except Exception:
        error=traceback.format_exc()
        if error not in _draw_errors:
            _draw_errors.append(error)
            print('MESHDOCK_DRAW_ERROR',error)
    finally:
        gpu.state.scissor_test_set(False)
        gpu.state.blend_set('NONE')


class MESHDOCK_OT_workbench_controller(bpy.types.Operator):
    bl_idname='meshdock.workbench_controller'
    bl_label='Mesh Dock Workbench'
    bl_options={'INTERNAL'}

    def invoke(self,context,event):
        key=context.window.as_pointer()
        if key in _sessions:
            return {'CANCELLED'}
        self.window=context.window
        self.key=key
        self.hits=[]; self.scale=1; self.inset=0; self.native=False
        self.hover=None; self.hover_at=0.0; self.focus=-1
        self.left_scroll=0; self.page=0; self.history_offset=0; self.history_query=''; self.history_total=0; self.filter='ALL'; self.compact='CREATE'
        self.category='GENERATE'; self.history_visible=True; self.sections=set(); self.scene_key=context.scene.as_pointer()
        self.spinning=False; self.drag=None; self.scroll_drag=False; self.scrollbar=None; self.mouse=(0,0); self.jobs=[]; self.last_refresh=0
        self.notice=''; self.notice_until=0
        self._timer=context.window_manager.event_timer_add(0.12,window=context.window)
        _sessions[key]=self
        self.refresh(context)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def cleanup(self):
        _sessions.pop(self.key,None)
        if self._timer:
            try:
                bpy.context.window_manager.event_timer_remove(self._timer)
            except (ReferenceError, ValueError, RuntimeError):
                pass
            self._timer=None

    def notify(self,message):
        self.notice=message; self.notice_until=time.monotonic()+5

    def refresh(self,context):
        query=context.scene.meshdock.history_search
        if query!=self.history_query: self.page=0;self.history_query=query
        limit=getattr(self,'history_limit',6)
        history=get_runtime().service.history_records(query,self.page*limit,limit)
        self.history_items=history['records'];self.history_total=history['total']
        self.jobs=list({r['job']['id']:r['job'] for r in self.history_items}.values())
        self.last_refresh=time.monotonic()
        p=context.scene.meshdock
        for reference in p.reference_images:
            if not reference.preview_image:
                try:
                    managed=get_runtime().service.references.get(reference.reference_id)
                    reference.preview_image=bpy.data.images.load(managed.local_path,check_existing=True)
                    reference.preview_image.preview_ensure()
                except Exception:
                    pass
        key=p.last_job_id+'/'+p.candidate_id
        job=next((j for j in self.jobs if j['id']==p.last_job_id),None)
        # Resolve the active object by persistent identity, never by history page.
        from .process_target import generated_target
        ids=generated_target(context)
        if ids and getattr(self,'selected_ids',None)!=ids:
            self.selected_ids=ids
            p.selected_job_key=ids[0];p.last_job_id=ids[0];p.candidate_id=ids[1]
        elif not ids: self.selected_ids=None
        if p.last_job_id not in ('','__none__'):
            try: job=get_runtime().service.get_job(p.last_job_id)
            except Exception: job=None
        self.current_job=get_runtime().service.scene_attention_job(context.scene.get('meshdock_jobs',[])) or job
        if self.scene_key!=context.scene.as_pointer():
            self.scene_key=context.scene.as_pointer();self.left_scroll=0;self.page=0
        if self.category=='RETOPOLOGY':
            from .properties import _process_capabilities
            if 'retopology' in _process_capabilities(p) and p.process_operation!='retopology':
                p.process_operation='retopology'
        if job:
            process=next((task for task in job.get('process_tasks',[]) if task['id']==p.last_process_task_id),None)
            if process and process.get('result_candidate_id') and p.get('workbench_seen_process')!=process['id']:
                get_runtime().request_process_result(job['id'],process['id'])
                p['workbench_seen_process']=process['id']
                self.notify(tr('处理完成，新结果将加入原场景','Processing complete. Result returns to its scene.'))
            for candidate in job['candidates']:
                if candidate.get('imported'):
                    preview.request_thumbnail(job['id'],candidate['id'])

    def paint(self,c,context):
        p=context.scene.meshdock
        # Native T/N regions take precedence over the workbench overlays.
        if context.space_data.show_region_toolbar or context.space_data.show_region_ui:
            self.left_width=0;self.right_start=c.w;self.width=c.w
            return
        left=330 if c.w>=1180 else 300
        if c.w<640: left=min(300,c.w*.60)
        right=0
        self.left_width=left; self.right_start=c.w; self.width=c.w
        show_left=True; show_right=False
        if self.native:
            self.left_width=0; self.right_start=c.w
            c.button(c.w/2-90,c.h-54,180,36,tr('返回生成面板','Show generation'),('native',),active=True)
            return
        self.left_width=left if show_left else 0
        self.right_start=c.w-right if show_right else c.w
        if show_left:
            self.paint_left(c,context,left)
        if show_right:
            self.paint_history(c,context,left)
        x=left+22 if show_left else 24
        end=c.w-right-22 if show_right else c.w-24
        available=end-x
        if self.notice and time.monotonic()<self.notice_until and available>140:
            c.box(x,c.h-58,available,36,(0.16,0.23,0.21,1))
            c.text(x+12,c.h-49,self.notice,13,width=available-24)
        large=context.scene.get('meshdock_large_reference',-1)
        if 0<=large<len(p.reference_images):
            c.box(0,58,c.w,c.h-58,(0.025,0.03,0.04,0.97),0)
            c.picture(p.reference_images[large].preview_image,50,100,c.w-100,c.h-160)
            c.hits=[]
            c.button(c.w-130,72,100,36,tr('关闭预览','Close'),('close_reference',))
        if self.hover and time.monotonic()-self.hover_at>0.6:
            hit=next((h for h in c.hits if h[4]==self.hover),None)
            if hit:
                tip=hit[5]
                tx=min(max(8,hit[0]),c.w-min(360,len(tip)*14)-8)
                ty=min(c.h-42,hit[1]+hit[3]+5)
                tw=min(360,max(100,len(tip)*13))
                c.box(tx,ty,tw,32,(0.03,0.035,0.045,0.98))
                c.text(tx+10,ty+7,tip,12,width=tw-20)

    def paint_left(self,c,context,width):
        p=context.scene.meshdock
        c.box(0,0,width,c.h,PANEL,0)
        c.box(0,0,64,c.h,BG,0)
        c.text(80,14,'Mesh Dock',TYPE_BRAND)
        entries=[(tr('生成','Create'),'generate','GENERATE'),
                 (tr('重拓扑','Retopo'),'process','RETOPOLOGY'),
                 (tr('处理','Process'),'image','PROCESS'),
                 (tr('动画','Animate'),'animate','ANIMATE'),
                 (tr('平台','Platforms'),'platform','PLATFORM'),
                 (tr('历史','History'),'history','HISTORY')]
        step=min(72,max(36,(c.h-58)/len(entries)))
        for i,(label,icon,key) in enumerate(entries):
            ry=50+i*step;h=step-8
            c.button(6,ry,52,h,'',('category',key),tip=tr('切换到','Switch to ')+label,active=self.category==key)
            if h>=44:
                c.icon(23,ry+7,icon,18)
                c.center_text(6,ry+h-25,52,22,label,11)
            else: c.center_text(6,ry,52,h,label,11)
        c.scissor(70,52,width-82,max(0,c.h-150))
        x=80; w=width-96; y=62-self.left_scroll
        if self.category=='HISTORY':
            c.unclip();self.left_max=0;self.scrollbar=None
            self.paint_history(c,context,width)
            return
        if self.category=='PLATFORM':
            self.paint_platform(c,context,x,y,w,width)
            return
        if self.category!='GENERATE':
            self.paint_task(c,context,x,y,w,width)
            return
        c.text(x,y,tr('生成模型','Create model'),TYPE_PAGE); y+=32
        account=p.generation_account.split('|')[0]
        label={'tripo_global':'Tripo · Global','tripo_cn':'Tripo · CN','hunyuan_direct':tr('混元 3D','Hunyuan 3D'),
               'tokenhub_global':'TokenHub','tokenhub_cn':'TokenHub'}.get(account,tr('连接账号','Connect account'))
        c.button(x,y,w,ROW,tr('平台 · ','Platform · ')+label+'  ▾',('field','generation_account'));y+=ROW+GAP_XS
        model=p.generation_model.split('-20')[0] if p.generation_model!='__none__' else tr('选择模型','Choose model')
        c.button(x,y,w,ROW,tr('模型 · ','Model · ')+model,('field','generation_model'));y+=ROW+GAP_MD
        labels=[(tr('单图','Image'),'image'),(tr('多视图','Views'),'multiview'),(tr('文字','Text'),'text')]
        c.box(x,y,w,56,BG,9)
        for i,(label,mode) in enumerate(labels):
            bx=x+3+i*(w-6)/3; bw=(w-6)/3
            c.button(bx,y+3,bw-2,50,'',('mode',mode),tip=label,active=p.input_mode==mode)
            c.icon(bx+bw/2-8,y+9,mode,16)
            c.center_text(bx,y+27,bw-2,22,label,12)
        y+=56+GAP_MD
        if p.input_mode=='multiview':
            from .properties import active_references
            refs={r.view:r for r in active_references(p)}
            rule=generation_constraints(p).get('constraints',{}).get('multiview',{})
            required=set(rule.get('required_views',[]))
            side=(w-8)/2
            view_labels={'front':tr('正面','Front'),'back':tr('背面','Back'),'left':tr('左侧','Left'),'right':tr('右侧','Right'),
                         'top':tr('顶部','Top'),'bottom':tr('底部','Bottom'),'left_front':tr('左前 45°','Front left 45°'),'right_front':tr('右前 45°','Front right 45°')}
            all_views=list(rule.get('allowed_views',[]))
            views=all_views if 'extra_views' in self.sections else all_views[:4]
            c.text(x,y,f'{len(refs)} / {rule.get("max_images",len(views))}'+tr(' · 至少 ',' · Min. ')+str(rule.get('min_images',2))+tr(' 张 · * 必需',' · * Required'),TYPE_CAPTION,MUTED,width=w)
            y+=25
            inactive=len(p.reference_images)-len(refs)
            if inactive:
                c.text(x,y,tr('另有 ','Also ')+str(inactive)+tr(' 张暂不参与，已保留',' retained, not used'),12,MUTED,width=w);y+=24
            for i,view in enumerate(views):
                label=view_labels.get(view,view)
                bx=x+(i%2)*(side+8); by=y+(i//2)*(side+30)
                c.box(bx,by,side,side+22,CARD)
                ref=refs.get(view)
                if ref and ref.preview_image:
                    c.picture(ref.preview_image,bx+4,by+4,side-8,side-8)
                else:
                    c.center_text(bx,by,side,side,'+',24,MUTED)
                c.text(bx+8,by+side,label+(' *' if view in required else ''),11,width=side-16)
                c.hit(bx,by,side,side+22,('upload',view),tr('添加或替换','Add or replace')+' '+label+(tr('（必需）',' (required)') if view in required else ''))
            y+=math.ceil(len(views)/2)*(side+30)
            if len(all_views)>4:
                c.button(x,y,w,ROW_SMALL,tr('可选视图','Optional views')+(' ▾' if 'extra_views' in self.sections else ' ▸'),('section','extra_views'));y+=ROW_SMALL+GAP_XS
            c.button(x,y,w,ROW_SMALL,tr('管理参考图','Manage references'),('options','REFERENCES'));y+=ROW_SMALL+GAP_MD
        elif p.input_mode!='text':
            from .properties import active_references
            refs=list(active_references(p))
            image=refs[0].preview_image if refs else None
            c.box(x,y,w,190,CARD)
            if image:
                c.picture(image,x+6,y+6,w-12,158)
                c.text(x+10,y+166,refs[0].filename,11,MUTED,width=w-20)
            else:
                c.center_text(x,y+50,w,40,tr('添加参考图','Add reference'),18)
                c.center_text(x,y+94,w,24,'PNG · JPG · WEBP',TYPE_CAPTION,MUTED)
            c.hit(x,y,w,190,('upload','front'),tr('选择或替换正面参考图','Choose or replace the front reference'))
            y+=202
            c.button(x,y,w,ROW_SMALL,tr('管理参考图','Manage references')+f'  ·  {len(refs)}',('options','REFERENCES'))
            y+=ROW_SMALL+GAP_MD
        else:
            c.button(x,y,w,92,p.prompt or tr('描述你想生成的模型…','Describe your model…'),('options','INPUT'),tr('编辑模型描述，支持中文输入','Edit the prompt; supports IME input'))
            y+=92+GAP_MD
        y=self.paint_generation_settings(c,context,x,y,w)
        c.text(x,y+8,tr('生成数量','Quantity'),TYPE_BODY)
        c.button(x+w-105,y,30,32,'−',('quantity',-1),tip=tr('减少生成数量','Decrease quantity'),enabled=p.candidate_count>1,center=True)
        c.center_text(x+w-72,y,36,32,str(p.candidate_count),14)
        c.button(x+w-32,y,30,32,'+',('quantity',1),tip=tr('增加生成数量','Increase quantity'),enabled=p.candidate_count<4,center=True);y+=48

        self.finish_left(c,width,y)
        if self.paint_task_status(c,context,x,w): return
        try:
            from .properties import generation_cost_estimate
            estimate=generation_cost_estimate(p)
        except Exception:
            estimate=None
        blocker=generation_blocker(p)
        caption=blocker or (tr('预计 ','Est. ')+f'{estimate:g} '+tr('积分','credits') if estimate is not None else tr('费用由服务商确定','Provider pricing'))
        c.text(x,c.h-94,caption,12,MUTED,width=w)
        c.button(x,c.h-65,w,44,tr('生成模型','Generate model'),('generate',),tip=caption,active=True,enabled=not blocker,center=True,size=14)

    def finish_left(self,c,width,y):
        self.left_max=max(0,y+self.left_scroll-(c.h-112))
        self.left_scroll=min(self.left_scroll,self.left_max)
        c.unclip()
        self.scrollbar=None
        if self.left_max>0:
            top=50; height=max(40,c.h-158)
            handle=max(30,height*height/(height+self.left_max))
            offset=(height-handle)*self.left_scroll/self.left_max
            c.box(width-8,top,3,height,CARD,2)
            c.box(width-8,top+offset,3,handle,MUTED,2)
            self.scrollbar=(top,height,handle)
            c.hit(width-15,top,15,height,('scrollbar',),tr('滚动设置','Scroll settings'))
        c.box(64,c.h-103,width-64,103,PANEL,0)

    def field(self,c,p,x,y,w,name,label):
        value=getattr(p,name)
        enabled=not (name in {'tripo_quad','tripo_texture','tripo_pbr'} and p.tripo_generate_parts)
        if name=='tripo_texture_quality': enabled=p.tripo_texture and not p.tripo_generate_parts
        names={'auto':tr('自动','Auto'),'standard':tr('标准','Standard'),'detailed':tr('精细','Detailed'),
               'extreme':tr('超精细','Extreme'),'low':tr('低','Low'),'medium':tr('中','Medium'),'high':tr('高','High'),
               'triangle':tr('三角面','Triangles'),'quadrilateral':tr('四边面','Quads'),
               'Normal':tr('完整模型','Full model'),'Geometry':tr('仅几何','Geometry'),'LowPoly':tr('低多边形','Low poly'),
               'retopology':tr('重拓扑','Retopology'),'texture':tr('贴图','Texture'),'rig':tr('绑定','Rig'),
               '__none__':tr('暂无可用项','Unavailable')}
        from .workflow import provider_label,operation_label
        shown=names.get(str(value),str(value))
        if name=='process_provider': shown=provider_label(str(value))
        elif name=='process_operation': shown=operation_label(str(value))
        elif p.bl_rna.properties[name].type=='ENUM':
            item=p.bl_rna.properties[name].enum_items.get(str(value))
            if item: shown=bpy.app.translations.pgettext_iface(item.name)
        if isinstance(value,(int,float)) and not isinstance(value,bool): shown=f'{value:,g}'
        if isinstance(value,bool):
            c.text(x,y+9,label,TYPE_BODY,width=w-50)
            c.button(x+w-44,y+3,44,28,tr('开','On') if value else tr('关','Off'),('toggle',name),active=value,center=True,enabled=enabled)
        else:
            c.text(x,y+9,label,TYPE_BODY,width=w*.5-6)
            c.button(x+w*.5,y,w*.5,32,shown,('field',name),tip=label,center=True,enabled=enabled)
        return y+40

    def paint_generation_settings(self,c,context,x,y,w):
        from .properties import selected_generation_account, generation_topology_limits
        from ..core.provider_ids import TRIPO_PROVIDERS
        p=context.scene.meshdock
        provider=selected_generation_account(p)[0]
        for key,title in [('geometry',tr('几何与拓扑','Geometry')),('material',tr('材质与贴图','Material')),('advanced',tr('高级','Advanced'))]:
            opened=key in self.sections
            c.button(x,y,w,32,('▾ ' if opened else '› ')+title,('section',key));y+=38
            if not opened: continue
            fields=[]
            if key=='geometry':
                if provider in TRIPO_PROVIDERS and p.generation_model in {'v3.0-20250812','v3.1-20260211'}:
                    fields=[('tripo_geometry_quality',tr('几何质量','Quality')),('tripo_smart_low_poly',tr('低多边形','Low poly')),('tripo_quad',tr('四边面','Quads')),('tripo_generate_parts',tr('分部件生成','Parts'))]
                elif provider not in TRIPO_PROVIDERS:
                    fields=[('hunyuan_generate_type',tr('生成类型','Type'))]
                if generation_topology_limits(p).get('supported'):
                    fields.append(('use_custom_face_limit',tr('指定面数','Face limit')))
                    if p.use_custom_face_limit: fields.append(('target_face_count',tr('目标面数','Faces')))
            elif key=='material':
                if provider in TRIPO_PROVIDERS:
                    fields=[('tripo_texture',tr('生成贴图','Texture')),('tripo_pbr','PBR'),('tripo_export_uv',tr('导出 UV','Export UV')),('tripo_texture_quality',tr('贴图质量','Quality'))]
                else:
                    fields=[('hunyuan_enable_pbr','PBR'),('hunyuan_result_format',tr('结果格式','Format'))]
            else:
                fields=[('priority',tr('任务优先级','Priority')),('max_estimated_credits',tr('费用上限','Credit limit')),('advanced_json',tr('高级参数','Extra options'))]
            for name,label in fields:
                if name=='tripo_texture_quality' and p.generation_model=='v2.5-20250123': continue
                y=self.field(c,p,x+6,y,w-12,name,label)
        return y

    def paint_task_status(self,c,context,x,w):
        job=getattr(self,'current_job',None)
        if not job: return False
        runtime=get_runtime();p=context.scene.meshdock
        task=next((t for t in reversed(job.get('process_tasks',[])) if t['state'] in {'queued','processing','failed','recovery_pending'}),None)
        current=task or job;key=current['id']
        error=runtime._auto_import_errors.get(key)
        status=current['state']
        pending=not task and runtime.auto_import_status(job['id'])=='pending'
        if status not in {'queued','submitted','processing','failed','recovery_pending'} and not error and not pending: return False
        if getattr(self,'dismissed_task',None)==key: return False
        labels={'queued':tr('排队中','Queued'),'submitted':tr('已提交','Submitted'),'processing':tr('处理中','Processing'),
                'failed':tr('任务失败','Failed'),'recovery_pending':tr('等待恢复','Resume needed')}
        caption=tr('导入失败','Import failed') if error else tr('等待导入','Waiting to import') if pending else labels.get(status,status)
        if current.get('paused'): caption=tr('已暂停查询','Polling paused')
        if job.get('phase')=='downloading' and not task: caption=tr('下载中','Downloading')
        c.text(x,c.h-94,caption+f" · {current.get('progress',0)*100:.0f}%",13,MUTED,width=w)
        c.button(x,c.h-65,w*.67-4,44,tr('查看任务','Task details'),('task_details',job['id'],task['id'] if task else ''),active=True,center=True)
        c.button(x+w*.67,c.h-65,w*.33,44,tr('新任务','New task'),('new_task',key),center=True)
        return True

    def paint_task(self,c,context,x,y,w,width):
        from .properties import _process_capabilities,selected_process_provider
        from .workflow import process_fields,process_blocker,target_summary
        p=context.scene.meshdock;caps=_process_capabilities(p)
        title={'RETOPOLOGY':tr('重拓扑','Retopology'),'PROCESS':tr('模型处理','Processing'),'ANIMATE':tr('绑定与动画','Rig & animate')}[self.category]
        c.text(x,y,title,TYPE_PAGE);y+=32
        obj=context.active_object
        target=obj.name if obj else tr('请选择模型','Select a model')
        c.button(x,y,w,ROW,tr('目标 · ','Target · ')+target,('target_scope',),tip=tr('选择本次处理范围','Select the processing scope'));y+=ROW+GAP_XS
        if obj: c.text(x,y,target_summary(context),TYPE_CAPTION,MUTED,width=w);y+=GAP_LG
        if self.category=='ANIMATE':
            c.box(x,y,w,ROW,BG,8)
            for op,zh,en in [('rig_check','检查','Check'),('rig','绑定','Rig'),('animate','生成动作','Motion')]:
                index=('rig_check','rig','animate').index(op);bw=(w-6)/3;bx=x+3+index*bw
                c.button(bx,y+3,bw-2,ROW_SMALL,tr(zh,en),('process_mode',op),active=p.process_operation==op,enabled=op in caps,center=True)
            y+=ROW+GAP_SM
            if 'animate' not in caps:
                c.text(x,y,tr('生成动作前，请先完成绑定','Rig the model before generating motion'),12,MUTED,width=w);y+=30
        elif self.category=='PROCESS':
            y=self.field(c,p,x,y,w,'process_operation',tr('处理方式','Operation'))
        blocker=process_blocker(context,p)
        if not blocker:
            y=self.field(c,p,x,y,w,'process_provider',tr('处理服务','Service'))
            if p.process_operation=='retopology':
                pairs=[('process_face_limit',tr('目标面数','Target faces')),('process_quad',tr('四边面','Quads')),('process_bake',tr('烘焙贴图','Bake textures'))] if (selected_process_provider(p) or '').startswith('tripo_') else [('process_face_level',tr('面数级别','Density')),('process_polygon_type',tr('面类型','Polygons'))]
            else: pairs=process_fields(p)
            for name,label in pairs: y=self.field(c,p,x,y,w,name,label)
            if p.process_operation=='rig':
                c.text(x,y,tr('默认双足角色 · 保留原模型','Biped character · keeps original'),12,MUTED,width=w);y+=28
            y=self.field(c,p,x,y,w,'show_advanced_process',tr('高级参数','Advanced'))
            if p.show_advanced_process: y=self.field(c,p,x,y,w,'process_json','JSON')
        else:
            # Split at natural UI width rather than silently truncating the reason.
            for start in range(0,len(blocker),20): c.text(x,y,blocker[start:start+20],12,MUTED,width=w);y+=23
        if self.category=='ANIMATE':
            from .workflow import rig_check_result
            checked=rig_check_result(getattr(self,'current_job',None),p.candidate_id)
            if checked is not None:
                c.text(x,y,tr('检查通过，可以绑定','Check passed: ready to rig') if checked else tr('暂不适合自动绑定，请调整模型','Not riggable yet; adjust the model'),12,MUTED,width=w);y+=28
            y+=12
            y=self.field(c,p,x,y,w,'action_name',tr('预览动作','Preview action'))
            can_play=bool(obj and p.action_name not in ('','__none__'))
            c.button(x,y,w/2-4,32,tr('播放','Play'),('job','preview_character_action'),enabled=can_play,center=True)
            c.button(x+w/2,y,w/2,32,tr('停止','Stop'),('job','stop_character_preview'),enabled=bool(context.screen.is_animation_playing),center=True);y+=42
        self.finish_left(c,width,y)
        if self.paint_task_status(c,context,x,w): return
        from .workflow import operation_label
        caption=operation_label(p.process_operation) if not blocker else tr('暂不可处理','Unavailable')
        tip=blocker or tr('生成新结果并保留原模型','Creates a new result and keeps the original')
        c.button(x,c.h-65,w,44,caption,('job','process_candidate'),tip=tip,active=True,enabled=not blocker,center=True,size=14)

    def paint_platform(self,c,context,x,y,w,width):
        from .platform_links import PLATFORM_LABELS
        c.text(x,y,tr('账号','Accounts'),TYPE_PAGE);y+=32
        credentials=get_runtime().credentials
        for provider,label in PLATFORM_LABELS.items():
            profiles=credentials.list_profiles(provider)
            c.text(x,y,label,TYPE_BODY,width=w);y+=22
            if not profiles:
                c.text(x,y,tr('未连接','Not connected'),TYPE_CAPTION,MUTED,width=w);y+=20
            for index,profile in enumerate(profiles,1):
                caption=str(profile.get('note') or tr('账号 ','Account ')+str(index))
                status=tr('已启用','Enabled') if profile['enabled'] else tr('已停用','Disabled')
                c.text(x,y,caption+' · '+status,TYPE_CAPTION,MUTED,width=w);y+=21
                from . import account_checks
                c.button(x,y,w,26,account_checks.caption(provider,profile['id']),('check_account',provider,profile['id']),enabled=provider.startswith('tripo_'),tip=tr('只查询账号，不创建生成任务','Read-only check; no generation'),size=TYPE_CAPTION);y+=32
            c.button(x,y,w*.53-4,ROW_SMALL,tr('管理 / 添加','Manage / add'),('account',provider),center=True)
            c.button(x+w*.53,y,w*.47,ROW_SMALL,tr('获取 Key ↗','Get Key ↗'),('key_link',provider),center=True)
            y+=ROW_SMALL+GAP_MD
        self.finish_left(c,width,y)

    def paint_history(self,c,context,width):
        from .workflow import operation_label
        p=context.scene.meshdock;pad=80;w=width-96
        c.text(pad,62,tr('历史','History'),TYPE_PAGE)
        c.button(pad,92,w,32,p.history_search or tr('搜索名称、操作…','Search name, operation…'),('field','history_search'))
        y=136
        job=getattr(self,'current_job',None)
        task=next((t for t in reversed(job.get('process_tasks',[])) if t['state'] in {'queued','processing','recovery_pending','failed'}),None) if job else None
        current=task or job
        if current and current['state'] in {'queued','submitted','processing','recovery_pending','failed'}:
            labels={'queued':tr('排队中','Queued'),'submitted':tr('已提交','Submitted'),'processing':tr('处理中','Processing'),'recovery_pending':tr('等待恢复','Resume needed'),'failed':tr('任务失败','Failed')}
            c.button(pad,y,w,32,labels[current['state']]+f" · {current.get('progress',0)*100:.0f}%",('task_details',job['id'],task['id'] if task else ''));y+=44
        limit=max(1,int((c.h-y-55)/88))
        if getattr(self,'history_limit',6)!=limit:
            self.history_limit=limit;self.page=0;self.last_refresh=0
        pages=max(1,math.ceil(self.history_total/limit))
        if self.page>=pages: self.page=pages-1;self.last_refresh=0
        records=getattr(self,'history_items',[])[:limit]
        if not records: c.text(pad,y+14,tr('暂无记录','No history yet'),TYPE_CAPTION,MUTED,width=w)
        for slot,record in enumerate(records):
            j=record['job'];a=record['candidate'];index=record['index'];cy=y+slot*88
            active=j['id']==p.last_job_id and a['id']==p.candidate_id
            c.box(pad-2,cy-2,w+4,80,ACCENT if active else CARD)
            c.box(pad,cy,w,76,BG)
            image=preview.get_thumbnail(j['id'],a['id'])
            if image: c.picture(image,pad+4,cy+5,64,64)
            else: c.icon(pad+23,cy+24,'generate',22,MUTED)
            title=a['label'] if a.get('metadata',{}).get('process_operation') else j['spec'].get('prompt') or j['spec']['asset_name'].replace('_',' ')
            title=title+f' · {index+1}'
            op=operation_label(a.get('metadata',{}).get('process_operation','')) or tr('生成','Generate')
            meta=str(j.get('created_at',''))[:10]+' · '+op
            c.text(pad+76,cy+13,title,13,width=w-82)
            c.text(pad+76,cy+40,meta,12,MUTED,width=w-82)
            c.hit(pad,cy,w,76,('select',j['id'],a['id']),title+' · '+meta)
        footer=c.h-42
        c.button(pad,footer,36,28,'‹',('page',-1),enabled=self.page>0,center=True)
        c.center_text(pad+40,footer,w-80,28,f'{self.page+1} / {pages}',12,MUTED)
        c.button(pad+w-36,footer,36,28,'›',('page',1),enabled=self.page+1<pages,center=True)


    def dispatch(self,action,context):
        p=context.scene.meshdock
        kind=action[0]
        if kind=='scrollbar':
            self.scroll_drag=True
            top,height,handle=self.scrollbar
            self.left_scroll=max(0,min(self.left_max,(self.mouse[1]-top-handle/2)/max(1,height-handle)*self.left_max))
        elif kind=='close_reference': context.scene['meshdock_large_reference']=-1
        elif kind=='options':
            bpy.ops.meshdock.workbench_options('INVOKE_DEFAULT',section=action[1])
        elif kind=='compact':
            self.category='HISTORY' if action[1]=='ASSETS' else 'GENERATE';self.left_scroll=0
        elif kind=='check_account': bpy.ops.meshdock.check_account(provider=action[1],profile_id=action[2])
        elif kind=='account': bpy.ops.meshdock.manage_credentials('INVOKE_DEFAULT',provider=action[1])
        elif kind=='key_link':
            from .platform_links import KEY_URLS
            bpy.ops.wm.url_open(url=KEY_URLS[action[1]])
        elif kind=='process_mode': p.process_operation=action[1]
        elif kind=='target_scope':
            from .process_target import generated_target
            ids=generated_target(context)
            if ids:
                col=preview.candidate_collection(*ids)
                for obj in context.selected_objects: obj.select_set(False)
                for obj in col.all_objects:
                    if obj.name in context.view_layer.objects and not obj.hide_get(): obj.select_set(True)
            context.area.tag_redraw()
        elif kind=='task_details': bpy.ops.meshdock.task_details('INVOKE_DEFAULT',job_id=action[1],task_id=action[2])
        elif kind=='new_task': self.dismissed_task=action[1]
        elif kind=='history_batch':
            self.history_offset=max(0,self.history_offset+action[1]*40);self.page=0;self.refresh(context)
        elif kind=='category':
            self.category=action[1];self.left_scroll=0;self.compact='CREATE'
            if self.category=='ANIMATE':
                from .properties import _process_capabilities
                caps=_process_capabilities(p)
                p.process_operation=next((op for op in ('animate','rig','rig_check') if op in caps),'__none__')
            if self.category=='RETOPOLOGY':
                from .properties import _process_capabilities
                if 'retopology' in _process_capabilities(p): p.process_operation='retopology'
        elif kind=='section':
            self.sections.symmetric_difference_update({action[1]})
        elif kind=='toggle': setattr(p,action[1],not getattr(p,action[1]))
        elif kind=='field':
            from .workbench_controls import open_field
            open_field(context,action[1])
        elif kind=='mode':
            # Keep each input draft when the provider callback prunes incompatible views.
            fields=('reference_id','view','filename','format','width','height','bytes','dimensions')
            drafts=json.loads(context.scene.get('meshdock_input_drafts','{}'))
            previous=[{field:getattr(r,field) for field in fields} for r in p.reference_images]
            drafts[p.input_mode]=previous
            target=action[1]
            if target not in drafts:
                drafts[target]=[]
                if target!='text':
                    for record in previous:
                        if target=='image' and record['view']!='front':
                            continue
                        store=get_runtime().service.references
                        managed=store.get(record['reference_id'])
                        from pathlib import Path
                        clone=store.register(Path(managed.local_path),record['view'])
                        drafts[target].append({**record,'reference_id':clone['id']})
            p.input_mode=target
            p.reference_images.clear()
            for record in drafts[target]:
                try:
                    managed=get_runtime().service.references.get(record['reference_id'])
                except Exception:
                    continue
                item=p.reference_images.add()
                for field in fields: setattr(item,field,record[field])
                item.preview_image=bpy.data.images.load(managed.local_path,check_existing=True)
            context.scene['meshdock_input_drafts']=json.dumps(drafts)
            self.left_scroll=0
        elif kind=='upload': bpy.ops.meshdock.add_reference_image('INVOKE_DEFAULT',view=action[1])
        elif kind=='quantity': p.candidate_count=max(1,min(4,p.candidate_count+action[1]))
        elif kind=='generate': bpy.ops.meshdock.workbench_generate('INVOKE_DEFAULT')
        elif kind=='select': preview.select_model(context,action[1],action[2]);self.refresh(context)
        elif kind=='filter': self.filter=action[1];self.page=0
        elif kind=='page': self.page=max(0,self.page+action[1]);self.refresh(context)
        elif kind=='native':
            self.native=not self.native
            self.focus=-1; self.hover=None; self.spinning=False
            space=context.space_data
            space.show_region_toolbar=self.native
            space.show_gizmo=self.native
            space.overlay.show_overlays=self.native
            if not self.native:
                if context.object and context.object.mode!='OBJECT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                space.show_region_ui=False
                space.show_region_tool_header=False
        elif kind=='shading': context.space_data.shading.type=action[1]
        elif kind=='frame': preview.focus(context,preview.candidate_collection(p.last_job_id,p.candidate_id))
        elif kind=='spin': self.spinning=not self.spinning
        elif kind=='add':
            source,collection=preview.add_to_source(context,p.last_job_id,p.candidate_id)
            self.notify(tr('已加入场景：','Added to scene: ')+source.name)
        elif kind=='export': bpy.ops.meshdock.workbench_export('INVOKE_DEFAULT')
        elif kind=='job': getattr(bpy.ops.meshdock,action[1])('INVOKE_DEFAULT')
        elif kind=='back':
            source=bpy.data.scenes.get(context.scene.get('meshdock_source',''))
            workspace=bpy.data.workspaces.get(context.workspace.get('meshdock_source_workspace',''))
            if source: context.window.scene=source
            if workspace: context.window.workspace=workspace
        context.area.tag_redraw()

    def modal(self,context,event):
        if not _enabled:
            self.cleanup();return {'CANCELLED'}
        if not context.workspace or not context.workspace.get('meshdock_workbench'):
            self.cleanup();return {'CANCELLED'}
        if not context.area or context.area.type!='VIEW_3D' or context.region.type!='WINDOW':
            return {'PASS_THROUGH'}
        if event.type=='TIMER':
            if time.monotonic()-self.last_refresh>0.65:
                try:
                    self.refresh(context)
                    preview.tick_thumbnails()
                except Exception as exc:
                    self.notify(str(exc))
            if self.spinning:
                rv=context.space_data.region_3d
                rv.view_rotation=Quaternion((0,0,1),0.018) @ rv.view_rotation
            context.area.tag_redraw()
            return {'PASS_THROUGH'}
        mx,my=event.mouse_region_x/self.scale,(context.region.height-event.mouse_region_y)/self.scale-self.inset
        self.mouse=(mx,my)
        if my < 0:
            return {'PASS_THROUGH'}
        if self.scroll_drag:
            if event.type=='LEFTMOUSE' and event.value=='RELEASE':
                self.scroll_drag=False;return {'RUNNING_MODAL'}
            if event.type=='MOUSEMOVE' and self.scrollbar:
                top,height,handle=self.scrollbar
                self.left_scroll=max(0,min(self.left_max,(my-top-handle/2)/max(1,height-handle)*self.left_max))
                context.area.tag_redraw();return {'RUNNING_MODAL'}
        hit=next((h for h in reversed(self.hits) if h[0]<=mx<=h[0]+h[2] and h[1]<=my<=h[1]+h[3]),None)
        if event.type=='LEFTMOUSE' and event.value=='PRESS' and hit and hit[4][0]=='field':
            self.pending_field=hit[4]
            return {'RUNNING_MODAL'}
        if event.type=='LEFTMOUSE' and event.value=='RELEASE' and getattr(self,'pending_field',None):
            action=self.pending_field;self.pending_field=None
            if hit and hit[4]==action:
                try: self.dispatch(action,context)
                except Exception as exc: self.notify(str(exc))
            return {'RUNNING_MODAL'}
        if event.type=='RIGHTMOUSE' and event.value=='PRESS' and hit and hit[4][0]=='select':
            context.scene.meshdock.last_job_id=hit[4][1]
            context.scene.meshdock.candidate_id=hit[4][2]
            target_job,target_candidate=hit[4][1],hit[4][2]
            def menu(menu_self,menu_context):
                op=menu_self.layout.operator('meshdock.workbench_export',text=tr('导出模型…','Export model…'),icon='EXPORT')
                op.job_id=target_job;op.candidate_id=target_candidate
            context.window_manager.popup_menu(menu,title=tr('生成结果','Generated result'))
            return {'RUNNING_MODAL'}
        if event.type=='MOUSEMOVE':
            hover=hit[4] if hit else None
            if hover!=self.hover:
                self.hover=hover;self.hover_at=time.monotonic()
                context.area.tag_redraw()
        if context.scene.get('meshdock_large_reference',-1)>=0 and event.type=='ESC' and event.value=='PRESS':
            context.scene['meshdock_large_reference']=-1
            context.area.tag_redraw();return {'RUNNING_MODAL'}
        if event.type=='TAB' and event.value=='PRESS' and self.hits and not self.native and (mx<self.left_width or mx>self.right_start):
            self.focus=(self.focus+(-1 if event.shift else 1))%len(self.hits)
            self.hover=self.hits[self.focus][4];self.hover_at=time.monotonic()-1
            context.area.tag_redraw();return {'RUNNING_MODAL'}
        if event.type in {'RET','NUMPAD_ENTER','SPACE'} and event.value=='PRESS' and self.focus>=0 and self.hits and not self.native and (mx<self.left_width or mx>self.right_start):
            hit=self.hits[self.focus%len(self.hits)]
        elif not (event.type=='LEFTMOUSE' and event.value=='PRESS'):
            if event.type in {'WHEELUPMOUSE','WHEELDOWNMOUSE'} and mx<self.left_width:
                self.left_scroll=max(0,min(getattr(self,'left_max',0),self.left_scroll+(-36 if event.type=='WHEELUPMOUSE' else 36)))
                context.area.tag_redraw();return {'RUNNING_MODAL'}
            if mx<self.left_width or mx>self.right_start :
                if event.type not in {'TIMER','MOUSEMOVE','WINDOW_DEACTIVATE'}:
                    return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}
        if hit:
            try: self.dispatch(hit[4],context)
            except Exception as exc: self.notify(str(exc));self.report({'WARNING'},str(exc))
            return {'RUNNING_MODAL'}
        if mx<self.left_width or mx>self.right_start :
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}


def ensure_controllers():
    if not _enabled or bpy.app.background:
        return
    keys={w.as_pointer() for w in bpy.context.window_manager.windows}
    for key,session in list(_sessions.items()):
        if key not in keys:
            session.cleanup()
    for window in bpy.context.window_manager.windows:
        if window.workspace.get('meshdock_workbench') and window.workspace.get('meshdock_ready'):
            from .workspace import migrate_legacy_scene
            migrate_legacy_scene(window)
            if window.as_pointer() not in _sessions:
                area=next((a for a in window.screen.areas if a.type=='VIEW_3D'),None)
                if area:
                    region=next((r for r in area.regions if r.type=='WINDOW'),None)
                    if region:
                        with bpy.context.temp_override(window=window,area=area,region=region):
                            bpy.ops.meshdock.workbench_controller('INVOKE_DEFAULT')



@persistent
def _load_pre(_unused):
    for session in list(_sessions.values()):
        try:
            session.cleanup()
        except ReferenceError:
            pass
    _sessions.clear()


@persistent
def _load_post(_unused):
    # Old operator RNA is invalid after loading a file; never dereference it here.
    _sessions.clear()
    _textures.clear()
    preview.clear()
    get_runtime().restore_scene_tasks()


def start():
    global _enabled,_handler
    _enabled=True
    _draw_errors.clear()
    if _load_pre not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_load_pre)
    if _load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_load_post)
    if _handler is None and not bpy.app.background:
        _handler=bpy.types.SpaceView3D.draw_handler_add(draw,(),'WINDOW','POST_PIXEL')


def stop():
    global _enabled,_handler
    _enabled=False
    if _load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_load_pre)
    if _load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post)
    for session in list(_sessions.values()):
        try:
            session.cleanup()
        except ReferenceError:
            pass
    _sessions.clear()
    if _handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handler,'WINDOW');_handler=None
    for window in bpy.context.window_manager.windows:
        if preview.is_preview(window.scene):
            source=bpy.data.scenes.get(window.scene.get('meshdock_source',''))
            if source: window.scene=source
    _textures.clear();preview.clear()


CLASSES=(MESHDOCK_OT_workbench_controller,)
