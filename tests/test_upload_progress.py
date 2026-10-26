import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import ssh_control as app


class UploadProgressTests(unittest.TestCase):
    def test_progress_callback_survives_permission_fallbacks(self):
        for failures in (0, 1, 2):
            with self.subTest(failures=failures), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "model.bin"
                path.write_bytes(b"data")
                manager = app.SshManager()
                manager.client = Mock()
                manager._connected_locked = Mock(return_value=True)
                manager._sudo_exec = Mock()
                sftp = manager.client.open_sftp.return_value
                sftp.put.side_effect = [PermissionError("denied")] * failures + [None]
                callback = Mock()
                manager.upload({"password": "test", "username": "robot"}, str(path), callback=callback)
                self.assertEqual(sftp.put.call_count, failures + 1)
                for call in sftp.put.call_args_list:
                    self.assertIs(call.kwargs["callback"], callback)
                self.assertEqual(callback.call_args_list, [unittest.mock.call(0, 4)] * (failures + 1))
                sftp.close.assert_called_once()

    def test_busy_drop_is_rejected_before_parsing(self):
        ui = app.SshControlApp.__new__(app.SshControlApp)
        ui.command_busy = True
        ui.uploading = True
        ui._append_log = Mock()
        ui._parse_drop_files = Mock()
        self.assertEqual(ui._on_file_drop(SimpleNamespace(data="other.bin")), "refuse_drop")
        ui._parse_drop_files.assert_not_called()
        self.assertIn("上传", ui._append_log.call_args.args[0])

    def test_cannot_choose_a_new_file_during_upload(self):
        ui = app.SshControlApp.__new__(app.SshControlApp)
        ui.command_busy = True
        ui.uploading = True
        ui._append_log = Mock()
        with unittest.mock.patch.object(app.filedialog, "askopenfilename") as dialog:
            ui._choose_file()
        dialog.assert_not_called()

    def test_worker_reports_final_result_after_transfer(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                ui = app.SshControlApp.__new__(app.SshControlApp)
                ui.message_queue = queue.Queue()
                ui.manager = Mock()
                ui._post_log = Mock()
                ui._post_status = Mock()
                def transfer(settings, path, callback):
                    callback(50, 100)
                    callback(100, 100)
                    if failure:
                        raise OSError("remote move failed")
                ui.manager.upload.side_effect = transfer
                ui._upload_worker({}, "/tmp/model.bin")
                events = list(ui.message_queue.queue)
                self.assertIn(("upload_progress", 50, 100), events)
                self.assertEqual(events[-2:], [("upload_done", not failure), ("busy", False)])

    def test_ui_only_shows_complete_after_success_and_unlocks_after_failure(self):
        ui = app.SshControlApp.__new__(app.SshControlApp)
        ui.message_queue = queue.Queue()
        ui.check_stop = threading.Event()
        ui.check_stop.set()
        ui.uploading = True
        ui.upload_percent_var = Mock()
        ui.upload_progress_var = Mock()
        ui._set_command_busy = Mock()
        ui.message_queue.put(("upload_progress", 100, 100))
        ui._poll_messages()
        self.assertLess(ui.upload_percent_var.set.call_args.args[0], 100)
        for success in (False, True):
            ui.uploading = True
            ui.message_queue.put(("upload_done", success))
            ui.message_queue.put(("busy", False))
            ui._poll_messages()
            self.assertFalse(ui.uploading)
            self.assertIn("完成" if success else "失败", ui.upload_progress_var.set.call_args.args[0])
            ui._set_command_busy.assert_called_with(False)
        ui.upload_percent_var.set.assert_called_with(100)


if __name__ == "__main__":
    unittest.main()
