"""Worker action wiring should cover every task scheduled by continuous_runner."""
import argparse
from unittest.mock import patch

import agent_worker


def test_portal_discovery_action_is_available_to_the_scheduler():
    expected = {"imported": 2, "duplicates": 1, "errors": 0, "portals": ["wellfound"]}
    with patch("portal_discovery.discover", return_value=expected) as discover:
        agent_worker.run(argparse.Namespace(action="portals", limit=1, job=None, jobs=None, dry_run=False, url=None))
    discover.assert_called_once_with(1)


def test_employer_batch_is_processed_as_soon_as_its_feed_finishes():
    def discover(args):
        assert args.limit == 2
        args.on_batch("2026-10-24T00:00:00+00:00", {"company": "Example"})

    with (
        patch("jobpilot.cmd_discover", side_effect=discover),
        patch.object(agent_worker, "process_fresh_jobs") as process,
        patch("portal_discovery.discover", return_value={}),
        patch("remote_discovery.discover", return_value={}),
        patch("himalayas_discovery.discover", return_value={}),
    ):
        agent_worker.run(argparse.Namespace(action="discover", limit=2, job=None, jobs=None, dry_run=False, url=None))

    assert process.call_args_list[0].args == ("2026-10-24T00:00:00+00:00", 2)
