"""Explicit per-task recovery, with no implicit retry or paid resubmission."""
import bpy
from bpy.props import StringProperty
from .runtime import get_runtime
from .workbench_controls import tr

class MESHDOCK_OT_task_command(bpy.types.Operator):
    bl_idname='meshdock.task_command'
    bl_label='Task action'
    job_id: StringProperty()
    task_id: StringProperty()
    action: StringProperty()
    def execute(self,context):
        rt=get_runtime();service=rt.service
        try:
            if self.action=='import':
                rt._auto_import_errors.pop(self.task_id or self.job_id,None)
                if self.task_id: rt.request_process_result(self.job_id,self.task_id)
                else: rt.request_auto_import(self.job_id)
            elif self.task_id:
                fn={'pause':service.pause_process_task,'continue':service.resume_paused_process_task,
                    'resume':service.resume_process_task,'cancel':service.cancel_process_task}[self.action]
                fn(self.job_id,self.task_id);rt.request_process_result(self.job_id,self.task_id)
            else:
                fn={'pause':service.pause_job,'continue':service.resume_paused_job,'resume':service.resume_job,
                    'cancel':service.cancel_job}[self.action]
                fn(self.job_id)
                if self.action=='resume': rt.request_auto_import(self.job_id)
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'},str(exc));return {'CANCELLED'}

class MESHDOCK_OT_task_details(bpy.types.Operator):
    bl_idname='meshdock.task_details'
    bl_label='Task status'
    job_id: StringProperty()
    task_id: StringProperty()
    def invoke(self,context,event): return context.window_manager.invoke_popup(self,width=460)
    def draw(self,context):
        rt=get_runtime();layout=self.layout
        try:
            job=rt.service.get_job(self.job_id)
            task=rt.service.get_process_status(self.job_id,self.task_id) if self.task_id else job
            layout.label(text=job['spec']['asset_name'])
            state=task['state'];error=task.get('error') or {}
            message=rt._auto_import_errors.get(self.task_id or self.job_id) or error.get('message','')
            labels={'queued':tr('排队中','Queued'),'processing':tr('处理中','Processing'),'recovery_pending':tr('等待恢复','Resume needed'),
                    'failed':tr('任务失败','Failed'),'cancelled':tr('已在本机取消','Cancelled locally'),'completed':tr('已完成','Complete')}
            layout.label(text=labels.get(state,state))
            if message:
                for i in range(0,min(len(message),500),48): layout.label(text=message[i:i+48])
            def button(action,label):
                op=layout.operator('meshdock.task_command',text=label);op.job_id=self.job_id;op.task_id=self.task_id;op.action=action
            if (self.task_id or self.job_id) in rt._auto_import_errors:
                button('import',tr('重试导入已下载结果','Retry importing downloaded result'))
            elif state in {'queued','submitted','processing'}:
                button('continue' if task.get('paused') else 'pause',tr('继续查询','Continue polling') if task.get('paused') else tr('暂停查询','Pause polling'))
                button('cancel',tr('停止本机任务','Stop local task'))
                layout.label(text=tr('云端可能继续执行并计费','The provider may continue processing and billing'))
            elif state in {'failed','recovery_pending','cancelled'}:
                has_id=rt.service.can_resume_task(self.job_id,self.task_id)
                if has_id: button('resume',tr('查询并恢复原任务','Resume the existing remote task'))
                else: layout.label(text=tr('无法安全恢复，请检查原因后新建任务','Cannot safely resume. Check the cause before submitting a new task.'))
        except Exception as exc: layout.label(text=str(exc)[:80])
    def execute(self,context): return {'FINISHED'}

CLASSES=(MESHDOCK_OT_task_command,MESHDOCK_OT_task_details)
