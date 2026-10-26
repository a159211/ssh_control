import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

import ssh_control as app


def properties(active="active", enabled="enabled", load="loaded", sub="running"):
    return f"LoadState={load}\nActiveState={active}\nSubState={sub}\nUnitFileState={enabled}\n"


class SystemdStatusTests(unittest.TestCase):
    def read_status(self, output, code=0):
        manager = app.SshManager()
        manager.run = Mock(return_value=(code, output, ""))
        result = manager.check_service_status({}, "test_all.sh")
        command = manager.run.call_args.args[1]
        self.assertIn("systemctl show", command)
        self.assertIn("test_all.sh", command)
        self.assertNotIn("start_all.pid", command)
        return result

    def test_boot_started_service_is_running(self):
        status = self.read_status(properties())
        self.assertIs(status.running, True)
        self.assertEqual(status.text, "已启动")
        self.assertEqual(status.autostart_text, "已启用")

    def test_runtime_and_autostart_are_independent(self):
        status = self.read_status(properties(active="inactive", sub="dead"))
        self.assertIs(status.running, False)
        self.assertEqual(status.text, "未启动")
        self.assertEqual(status.autostart_text, "已启用")
        status = self.read_status(properties(enabled="disabled"))
        self.assertIs(status.running, True)
        self.assertEqual(status.autostart_text, "未启用")

    def test_transitional_and_failure_states(self):
        for state, text, running in [
            ("activating", "启动中…", None),
            ("deactivating", "停止中…", None),
            ("failed", "运行失败", False),
            ("reloading", "重载中…", True),
            ("inactive", "未启动", False),
        ]:
            with self.subTest(state=state):
                status = self.read_status(properties(active=state))
                self.assertEqual(status.text, text)
                self.assertIs(status.running, running)

    def test_autostart_special_states_are_explicit(self):
        for state, text in [
            ("enabled-runtime", "临时启用（重启失效）"),
            ("masked", "已屏蔽"),
            ("static", "静态单元（由依赖触发）"),
            ("indirect", "间接启用"),
        ]:
            with self.subTest(state=state):
                self.assertEqual(self.read_status(properties(enabled=state)).autostart_text, text)

    def test_missing_unit_is_not_reported_as_stopped(self):
        status = self.read_status(properties(load="not-found", active="inactive", enabled=""))
        self.assertIsNone(status.running)
        self.assertEqual(status.text, "服务不存在")
        self.assertEqual(status.autostart_text, "服务不存在")

    def test_invalid_or_failed_query_is_rejected(self):
        for code, output in [(1, ""), (0, ""), (0, "ActiveState=active\n")]:
            with self.subTest(code=code, output=output):
                manager = app.SshManager()
                manager.run = Mock(return_value=(code, output, "query failed"))
                with self.assertRaises(OSError):
                    manager.check_service_status({}, "test_all.sh")

    def test_existing_motion_config_gets_new_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text('{"start_command":"cd /opt/ac_ws && sudo ./robot_service.sh",'
                            '"stop_command":"cd /opt/ac_ws && sudo ./robot_service.sh stop",'
                            '"username":"robot"}')
            config = app.load_config(str(path), app.DEFAULTS)
        ui = app.SshControlApp.__new__(app.SshControlApp)
        ui.profile = app.PROFILES["motion"]
        ui.config = config
        for name in ("host", "port", "username", "password", "key_file", "start_cmd",
                     "stop_cmd", "interval", "auto_check", "remember"):
            setattr(ui, name + "_var", Mock())
        ui._apply_config()
        ui.start_cmd_var.set.assert_called_once_with("sudo systemctl start test_all.sh")
        ui.stop_cmd_var.set.assert_called_once_with("sudo systemctl stop test_all.sh")
        ui.username_var.set.assert_called_once_with("robot")

    def test_poll_updates_both_labels_and_buttons(self):
        ui = app.SshControlApp.__new__(app.SshControlApp)
        ui.message_queue = queue.Queue()
        ui.check_stop = threading.Event()
        ui.check_stop.set()
        ui.service_running = None
        ui.service_status = None
        ui.service_status_var = Mock()
        ui.service_status_label = Mock()
        ui.autostart_status_var = Mock()
        ui.autostart_status_label = Mock()
        ui._update_command_buttons = Mock()
        ui._append_log = Mock()
        status = self.read_status(properties())
        ui.message_queue.put(("service_status", status, True))
        ui._poll_messages()
        self.assertIs(ui.service_running, True)
        ui.service_status_var.set.assert_called_with("已启动")
        ui.autostart_status_var.set.assert_called_with("已启用")
        ui._update_command_buttons.assert_called_once()

    def test_query_error_clears_stale_state_in_auto_refresh(self):
        ui = app.SshControlApp.__new__(app.SshControlApp)
        ui.manager = Mock()
        ui.manager.check_service_status.side_effect = OSError("SSH disconnected")
        ui.service_name = "test_all.sh"
        ui.bms_log_path = ""
        ui.message_queue = queue.Queue()
        ui._refresh_runtime({})
        event = ui.message_queue.get_nowait()
        self.assertEqual(event[0], "service_status_error")
        self.assertIn("SSH disconnected", event[1])


if __name__ == "__main__":
    unittest.main()
