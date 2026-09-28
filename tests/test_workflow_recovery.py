from dataclasses import replace
import time

import pytest
from meshdock.core.errors import ValidationError, InvalidTransitionError
from meshdock.core.models import AssetJob, AssetSpec, Candidate, JobState, ProcessState, ProcessTask
from meshdock.core.service import AssetPipelineService
from meshdock.core.staging import StagingStore
from meshdock.providers.mock import MockProvider


class RecoveryProvider(MockProvider):
    def __init__(self): self.resumed=[]; self.submitted=[]
    def capabilities(self): return replace(super().capabilities(),postprocess=("rig_check",))
    def validate_process(self,*args): pass
    def estimate_process_credits(self,*args): return 30.0
    def process(self,*args,**kwargs):
        self.submitted.append(args)
        raise AssertionError("Recovery must not create a paid task")
    def resume_process(self,job,candidate,operation,params,remote_id,destination,**kwargs):
        self.resumed.append(remote_id)
        return {"diagnostics":{"riggable":True}}


def fixture_job():
    job=AssetJob(spec=AssetSpec(asset_name="old_result",prompt="old result"),state=JobState.CANDIDATES_READY)
    job.candidates.append(Candidate(id="part",provider="mock",label="result",local_model_path="unused.obj",format="obj",imported_collection="OldCollection"))
    return job


def test_history_search_and_ownership_span_all_results(tmp_path):
    service=AssetPipelineService({"mock":MockProvider()},StagingStore(tmp_path))
    old=fixture_job();service._jobs[old.id]=old
    for i in range(120):
        job=fixture_job();job.spec=replace(job.spec,asset_name=f"new_{i}",prompt="new")
        job.candidates[0].imported_collection=f"Collection{i}"
        service._jobs[job.id]=job
    assert service.history_records(query="old result")["total"]==1
    pages=[service.history_records(offset=i,limit=7)["records"] for i in range(0,121,7)]
    assert len({r["job"]["id"] for page in pages for r in page})==121
    old.archived=True
    assert service.history_records(query="old result")["total"]==0
    assert service.find_candidate_collections({"OldCollection"})==[(old.id,"part")]
    old.archived=False;old.candidates[0].metadata["local_scene_source"]=True
    assert service.history_records(query="old result")["total"]==0


def test_restart_resumes_original_remote_task_without_submission(tmp_path):
    store=StagingStore(tmp_path);job=fixture_job()
    task=ProcessTask(operation="rig_check",provider="mock",source_candidate_id="part",params={},state=ProcessState.PROCESSING,provider_task_id="existing-remote-task")
    job.process_tasks.append(task);store.save_job(job)
    adapter=RecoveryProvider();service=AssetPipelineService({"mock":adapter},store)
    assert service.get_process_status(job.id,task.id)["state"]=="recovery_pending"
    assert service.can_resume_task(job.id,task.id)
    assert "provider_task_id" not in service.get_process_status(job.id,task.id)
    service.resume_process_task(job.id,task.id)
    deadline=time.monotonic()+3
    while time.monotonic()<deadline and service.get_process_status(job.id,task.id)["state"]!="completed": time.sleep(.01)
    assert service.get_process_status(job.id,task.id)["state"]=="completed"
    assert adapter.resumed==["existing-remote-task"] and not adapter.submitted
    assert service.get_job(job.id)["artifacts"][0]["diagnostics"]["riggable"] is True


def test_missing_remote_task_is_not_recoverable(tmp_path):
    store=StagingStore(tmp_path);job=fixture_job()
    task=ProcessTask(operation="rig_check",provider="mock",source_candidate_id="part",params={})
    job.process_tasks.append(task);store.save_job(job)
    service=AssetPipelineService({"mock":RecoveryProvider()},store)
    assert not service.can_resume_task(job.id,task.id)
    with pytest.raises(InvalidTransitionError): service.resume_process_task(job.id,task.id)


def test_process_budget_rejects_before_provider_submission(tmp_path):
    adapter=RecoveryProvider();service=AssetPipelineService({"mock":adapter},StagingStore(tmp_path))
    job=fixture_job();job.spec=replace(job.spec,max_estimated_credits=10);service._jobs[job.id]=job
    with pytest.raises(ValidationError,match="exceeds"): service.submit_candidate_process(job.id,"part","rig_check","mock")
    assert not job.process_tasks and not adapter.submitted and not adapter.resumed
