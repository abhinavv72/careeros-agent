import os
import unittest
from unittest.mock import Mock,patch
from contextlib import nullcontext
import agent_launcher


class LauncherTests(unittest.TestCase):
    def test_child_process_gets_user_site_for_user_installed_dependencies(self):
        with patch('agent_launcher.site.getusersitepackages',return_value=str(agent_launcher.ROOT)), \
             patch.dict('agent_launcher.os.environ',{'PYTHONPATH':'existing-path'},clear=False):
            env=agent_launcher.child_environment()
        self.assertEqual(env['PYTHONPATH'],os.pathsep.join(['existing-path',str(agent_launcher.ROOT)]))

    def test_existing_ready_backend_is_reused(self):
        health={'service':'jobpilot','root':str(agent_launcher.ROOT),'ready':True,'restart_required':False}
        with patch('agent_launcher.get_health',return_value=health),patch('agent_launcher.subprocess.Popen') as process:
            self.assertEqual(agent_launcher.ensure_server(),health)
            process.assert_not_called()

    def test_startup_race_reuses_healthy_server_started_by_another_launcher(self):
        health={'service':'jobpilot','root':str(agent_launcher.ROOT),'ready':True,'pid':12345,'restart_required':False}
        process=Mock();process.poll.return_value=1
        with patch('agent_runtime.exclusive_lock',return_value=nullcontext()), \
             patch('agent_launcher.get_health',side_effect=[None,health]), \
             patch('agent_launcher.check_server_dependencies'), \
             patch('agent_launcher.subprocess.Popen',return_value=process):
            self.assertEqual(agent_launcher.ensure_server(),health)

    def test_active_updated_backend_is_not_interrupted(self):
        health={'service':'jobpilot','root':str(agent_launcher.ROOT),'restart_required':True,'active':'active-run'}
        with patch('agent_launcher.get_health',return_value=health),patch('agent_launcher.subprocess.Popen') as process:
            with self.assertRaises(RuntimeError):agent_launcher.ensure_server()
            process.assert_not_called()

    def test_missing_dependencies_are_reported_before_server_shutdown_or_spawn(self):
        health={'service':'jobpilot','root':str(agent_launcher.ROOT),'ready':True,'restart_required':True,'active':None}
        with patch('agent_launcher.get_health',return_value=health), \
             patch('agent_launcher.importlib.util.find_spec',side_effect=lambda name:None if name=='fastapi' else object()), \
             patch('agent_launcher.urlopen') as open_url, patch('agent_launcher.subprocess.Popen') as process:
            with self.assertRaisesRegex(RuntimeError,'pip install -r requirements-agent.txt'):
                agent_launcher.ensure_server()
            open_url.assert_not_called()
            process.assert_not_called()

    def test_other_workspace_is_not_reused_or_stopped(self):
        health={'service':'jobpilot','root':str(agent_launcher.ROOT.parent),'ready':True}
        with patch('agent_launcher.get_health',return_value=health),patch('agent_launcher.subprocess.Popen') as process:
            with self.assertRaises(RuntimeError):agent_launcher.ensure_server()
            process.assert_not_called()
