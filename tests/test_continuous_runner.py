"""Scheduler cadence state advances only after a source run succeeds."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
import os

import continuous_runner


def test_failed_source_run_does_not_record_successful_discovery_time():
    with tempfile.TemporaryDirectory(dir=continuous_runner.ROOT / "tmp") as folder:
        state_file = Path(folder) / "schedule.json"
        responses = [{"id": "run-1"}, {"runs": [{"id": "run-1", "status": "Failed"}]}]
        with patch.object(continuous_runner, "SCHEDULE_STATE", state_file), \
             patch.object(continuous_runner, "wait_idle", side_effect=[True, True]), \
             patch.object(continuous_runner, "api", side_effect=responses):
            try:
                continuous_runner.action("portals", limit=1)
            except RuntimeError:
                pass
            else:
                raise AssertionError("A failed discovery run should be reported")
        assert not state_file.exists()


def test_successful_source_run_records_its_completion_cadence():
    with tempfile.TemporaryDirectory(dir=continuous_runner.ROOT / "tmp") as folder:
        state_file = Path(folder) / "schedule.json"
        responses = [{"id": "run-2"}, {"runs": [{"id": "run-2", "status": "Completed"}]}]
        with patch.object(continuous_runner, "SCHEDULE_STATE", state_file), \
             patch.object(continuous_runner, "wait_idle", side_effect=[True, True]), \
             patch.object(continuous_runner, "api", side_effect=responses):
            assert continuous_runner.action("portals", limit=1)
        assert "portals" in json.loads(state_file.read_text(encoding="utf-8"))


def test_sign_in_startup_sweep_runs_immediate_discovery_and_queue_refresh():
    calls=[]
    with patch.dict(os.environ, {"JOBPILOT_INITIAL_SCAN":"1"}), \
         patch.object(continuous_runner, "apply_ready_batch", return_value=False), \
         patch.object(continuous_runner, "action", side_effect=lambda name, **kwargs: calls.append((name,kwargs)) or True):
        continuous_runner.startup_discovery_sweep()
    assert calls == [
        ("discover", {"limit":5}),
        ("naukri", {"limit":1}),
        ("score", {"limit":100}),
        ("queue", {"limit":50}),
    ]
    assert "JOBPILOT_INITIAL_SCAN" not in os.environ


def test_regular_runner_restart_does_not_repeat_sign_in_sweep():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JOBPILOT_INITIAL_SCAN", None)
        with patch.object(continuous_runner, "action") as action:
            continuous_runner.startup_discovery_sweep()
        action.assert_not_called()


def test_bulk_batch_uses_configured_size_and_explicit_ids():
    items = [{'job_id': i} for i in range(25)]
    with patch.object(continuous_runner.Path, 'read_text', return_value='{"bulkBatchSize":25}'), \
         patch.object(continuous_runner, 'api', side_effect=[{}, {'items': items}]) as api, \
         patch.object(continuous_runner, 'action', return_value=True) as action:
        assert continuous_runner.apply_ready_batch()
    assert api.call_args.args == ('apply/preview?limit=25',)
    action.assert_called_once_with('apply', limit=25, job_ids=list(range(25)))


def test_paused_bulk_does_not_preview_or_apply():
    with patch.object(continuous_runner, 'api', return_value={'autonomy_paused': True}) as api, \
         patch.object(continuous_runner, 'action') as action:
        assert not continuous_runner.apply_ready_batch()
    api.assert_called_once_with('state')
    action.assert_not_called()


def test_retry_failure_does_not_prevent_ready_application():
    calls = []
    def action(name, **kwargs):
        calls.append(name)
        raise RuntimeError('database busy')
    def apply_once():
        calls.append('apply')
        if calls.count('apply') > 1:
            raise KeyboardInterrupt
        return True
    with patch.object(continuous_runner, 'api', return_value={}), \
         patch.object(continuous_runner, 'apply_ready_batch', side_effect=apply_once), \
         patch.object(continuous_runner, 'action', side_effect=action), \
         patch.object(continuous_runner.time, 'monotonic', return_value=1000):
        try:
            continuous_runner.run_loop()
        except KeyboardInterrupt:
            pass
    assert calls == ['apply', 'retry', 'apply']
