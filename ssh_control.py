#!/usr/bin/env python3
"""SSH 远程控制面板。

自动检测目标主机是否可连接，在界面上显示连接状态，并通过“启动/停止”
按钮在远程主机上执行预先填写的命令。
"""

import json
import logging
import os
import queue
import re
import shlex
import socket
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import filedialog, scrolledtext

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_TKDND = True
except ImportError:
    DND_FILES = None
    TkinterDnD = None
    HAS_TKDND = False

try:
    import paramiko
except ImportError:
    raise SystemExit(
        "缺少 paramiko，请确认 vendor 依赖目录存在，或使用 ./run.sh 启动"
    )

logging.getLogger("paramiko").addHandler(logging.NullHandler())


APP_TITLE = "SSH 远程控制"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".ssh_control.json")
DEFAULT_HOST = "192.168.44.100"
REMOTE_CONFIG_PATH = "/opt/ac_ws/config/robot.yaml"
DEFAULT_PID_PATH = "/opt/ac_ws/logs/start_all.pid"

COLORS = {
    "bg": "#0f131b",
    "panel": "#151b26",
    "panel2": "#1c2432",
    "border": "#2c3a52",
    "fg": "#edf2f9",
    "muted": "#8b9aae",
    "accent": "#3ddc84",
    "accent_dim": "#21a762",
    "accent2": "#35c8e8",
    "danger": "#ff5f56",
    "danger_dim": "#7c2d28",
    "warn": "#ffb454",
    "border2": "#223048",
}

DEFAULTS = {
    "host": DEFAULT_HOST,
    "port": 22,
    "username": "",
    "password": "",
    "key_file": "",
    "start_command": "cd /opt/ac_ws && sudo ./robot_service.sh",
    "stop_command": "cd /opt/ac_ws && sudo ./robot_service.sh stop",
    "auto_check": True,
    "check_interval": 5,
    "remember_password": False,
    "ui_font": None,
    "number_font": None,
}


def load_config():
    config = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        for key in DEFAULTS:
            if key in data:
                config[key] = data[key]
    except (OSError, ValueError, TypeError):
        pass
    return config


def save_config(config):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


class SshManager:
    """管理单条 SSH 连接，并保证检测和执行命令不会互相打扰。"""

    def __init__(self):
        self.client = None
        self.lock = threading.Lock()

    def _new_client(self, settings):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs = {
            "hostname": settings["host"],
            "port": int(settings["port"]),
            "username": settings["username"],
            "timeout": 5,
            "banner_timeout": 8,
            "auth_timeout": 8,
        }
        if settings.get("key_file"):
            connect_kwargs["key_filename"] = settings["key_file"]
        else:
            connect_kwargs["password"] = settings.get("password", "")
        client.connect(**connect_kwargs)
        return client

    def _close_locked(self):
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None

    def _connected_locked(self):
        if self.client is None:
            return False
        transport = self.client.get_transport()
        return bool(transport and transport.is_active())

    def connect(self, settings, force=False):
        with self.lock:
            if not force and self._connected_locked():
                return True
            self._close_locked()
            self.client = self._new_client(settings)
            return True

    def close(self):
        with self.lock:
            self._close_locked()

    def run(self, settings, command):
        with self.lock:
            if not self._connected_locked():
                self._close_locked()
                self.client = self._new_client(settings)
            client = self.client
            try:
                password = settings.get("password", "")
                needs_sudo = bool(re.search(r"\bsudo\s", command))
                if needs_sudo and password:
                    cleaned = re.sub(r"\bsudo\s+", "", command, count=1)
                    command = "sudo -S sh -c " + shlex.quote(cleaned)
                stdin, stdout, stderr = client.exec_command(command, timeout=60)
                if needs_sudo and password:
                    stdin.write(password + "\n")
                    stdin.flush()
                    stdin.channel.shutdown_write()
                out = stdout.read().decode("utf-8", "replace")
                err = stderr.read().decode("utf-8", "replace")
                code = stdout.channel.recv_exit_status()
                return code, out, err
            except Exception:
                self._close_locked()
                raise

    def _sudo_exec(self, client, command, password):
        self._sudo_capture(client, command, password)

    def _sudo_capture(self, client, command, password):
        if not password:
            raise PermissionError("目标目录没有写权限，且没有可用的 sudo 密码")
        sudo_command = "sudo -S sh -c " + shlex.quote(command)
        stdin, stdout, stderr = client.exec_command(sudo_command, timeout=30)
        stdin.write(password + "\n")
        stdin.flush()
        stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        if code != 0:
            raise OSError(f"sudo 命令失败：{err.strip() or out.strip()}")
        return out

    def upload(self, settings, local_path, remote_dir="/opt/ac_ws/models/"):
        with self.lock:
            if not self._connected_locked():
                self._close_locked()
                self.client = self._new_client(settings)
            client = self.client
            password = settings.get("password", "")
            basename = os.path.basename(local_path)
            safe_dir = shlex.quote(remote_dir.rstrip("/"))
            remote_path = remote_dir.rstrip("/") + "/" + basename
            try:
                sftp = client.open_sftp()
                try:
                    try:
                        sftp.put(local_path, remote_path)
                    except (PermissionError, OSError):
                        if not password:
                            raise
                        self._sudo_exec(
                            client,
                            f"mkdir -p {safe_dir} && chown "
                            f"{shlex.quote(settings['username'])} {safe_dir}",
                            password,
                        )
                        try:
                            sftp.put(local_path, remote_path)
                        except (PermissionError, OSError):
                            temp_path = "/tmp/" + basename
                            sftp.put(local_path, temp_path)
                            self._sudo_exec(
                                client,
                                f"mv {shlex.quote(temp_path)} {shlex.quote(remote_path)}",
                                password,
                            )
                finally:
                    sftp.close()
            except Exception:
                self._close_locked()
                raise

    def read_remote_text(self, settings, remote_path):
        with self.lock:
            if not self._connected_locked():
                self._close_locked()
                self.client = self._new_client(settings)
            client = self.client
            password = settings.get("password", "")
            try:
                sftp = client.open_sftp()
                try:
                    with sftp.open(remote_path, "r") as handle:
                        return handle.read().decode("utf-8", "replace")
                finally:
                    sftp.close()
            except (PermissionError, OSError):
                if not password:
                    raise
                return self._sudo_capture(
                    client, f"cat {shlex.quote(remote_path)}", password
                )

    def write_remote_text(self, settings, remote_path, text):
        with self.lock:
            if not self._connected_locked():
                self._close_locked()
                self.client = self._new_client(settings)
            client = self.client
            password = settings.get("password", "")
            try:
                sftp = client.open_sftp()
                try:
                    with sftp.open(remote_path, "w") as handle:
                        handle.write(text)
                finally:
                    sftp.close()
            except (PermissionError, OSError):
                if not password:
                    raise
                basename = os.path.basename(remote_path)
                temp_path = "/tmp/" + basename + ".new"
                sftp = client.open_sftp()
                try:
                    with sftp.open(temp_path, "w") as handle:
                        handle.write(text)
                finally:
                    sftp.close()
                target_dir = shlex.quote(os.path.dirname(remote_path))
                self._sudo_exec(
                    client,
                    f"mkdir -p {target_dir} && "
                    f"mv {shlex.quote(temp_path)} {shlex.quote(remote_path)}",
                    password,
                )

    def check_service_status(self, settings, pid_path=DEFAULT_PID_PATH):
        command = (
            "if [ -f " + shlex.quote(pid_path) + " ]; then "
            "while read pid; do "
            "[ -n \"$pid\" ] && kill -0 \"$pid\" 2>/dev/null "
            "&& echo RUNNING && exit 0; "
            "done < " + shlex.quote(pid_path) + "; "
            "fi; echo STOPPED"
        )
        with self.lock:
            if not self._connected_locked():
                self._close_locked()
                self.client = self._new_client(settings)
            client = self.client
            wrapped = "sh -c " + shlex.quote(command)
            stdin, stdout, stderr = client.exec_command(wrapped, timeout=15)
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            code = stdout.channel.recv_exit_status()
            if code != 0:
                raise OSError(f"状态检测失败：{err.strip() or out.strip()}")
        return "RUNNING" in out, pid_path


class RobotYaml:
    SECTIONS = ("first_model_config", "second_model_config")

    @staticmethod
    def _key_name(line):
        match = re.match(r"^([A-Za-z0-9_]+)\s*:", line)
        return match.group(1) if match else None

    @staticmethod
    def _unquote(value):
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            return value[1:-1]
        return value

    @staticmethod
    def _quote(value, quote):
        if quote == "'":
            return "'" + value.replace("'", "''") + "'"
        if quote == '"':
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return '"' + escaped + '"'
        if re.search(r"[\s:#]", value):
            return "'" + value.replace("'", "''") + "'"
        return value

    @classmethod
    def read_names(cls, text):
        result = {section: None for section in cls.SECTIONS}
        section = None
        section_indent = -1
        for raw_line in text.splitlines():
            stripped = raw_line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(raw_line) - len(stripped)
            key = cls._key_name(stripped)
            if section is None:
                if key in cls.SECTIONS:
                    section = key
                    section_indent = indent
                continue
            if indent <= section_indent:
                if key in cls.SECTIONS:
                    section = key
                    section_indent = indent
                else:
                    section = None
                continue
            match = re.match(r"^name\s*:\s*(.+?)\s*$", stripped)
            if match:
                value_text = re.split(r"\s+#", match.group(1).strip(), maxsplit=1)[0].strip()
                result[section] = cls._unquote(value_text)
        return result

    @classmethod
    def update_name(cls, text, section, new_name):
        if section not in cls.SECTIONS:
            raise ValueError(f"未知配置节：{section}")
        lines = text.splitlines()
        current_section = None
        section_indent = -1
        changed = False
        for index, raw_line in enumerate(lines):
            stripped = raw_line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(raw_line) - len(stripped)
            key = cls._key_name(stripped)
            if current_section is None:
                if key in cls.SECTIONS:
                    current_section = key
                    section_indent = indent
                continue
            if indent <= section_indent:
                if key in cls.SECTIONS:
                    current_section = key
                    section_indent = indent
                else:
                    current_section = None
                continue
            if current_section == section:
                match = re.match(r"^name\s*:\s*(.*)$", stripped)
                if match:
                    prefix = raw_line[: len(raw_line) - len(stripped)]
                    value_text = match.group(1).strip()
                    quote = ""
                    if value_text and value_text[0] in ("'", '"'):
                        quote = value_text[0]
                    comment = ""
                    comment_match = re.search(r"(\s+#.*)$", value_text)
                    if comment_match:
                        comment = comment_match.group(1)
                    lines[index] = (
                        f"{prefix}name: {cls._quote(new_name, quote)}{comment}"
                    )
                    changed = True
                    break
        if not changed:
            raise ValueError(f"在 {section} 下没有找到可修改的 name")
        return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


class SshControlApp:
    def __init__(self):
        self.config = load_config()
        self.manager = SshManager()
        self.checking = False
        self.command_busy = False
        self.show_password = False
        self.service_status_checked = False
        self.check_stop = threading.Event()
        self.message_queue = queue.Queue()

        self.root = TkinterDnD.Tk() if HAS_TKDND else tk.Tk()
        self.root.title(APP_TITLE)
        self.root.configure(bg=COLORS["bg"])
        self.root.geometry("1180x680")
        self.root.minsize(1000, 620)
        self._families = set(tkfont.families(self.root))
        self.font_family = self._pick_font(self.config.get("ui_font"))
        self.number_family = self._pick_number_font(self.config.get("number_font"))

        self._create_vars()
        self._build_ui()
        self._apply_config()
        self.auto_check_var.trace_add("write", lambda *_: self._auto_check_toggled())
        self._update_command_buttons()

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if self.auto_check_var.get():
            self.root.after(300, self._auto_tick)
        self.root.after(60, self._poll_messages)

    def _pick_font(self, saved=None):
        if saved and saved != "Helvetica" and saved in self._families:
            return saved
        candidates = [
            "MiSans",
            "HarmonyOS Sans SC",
            "Alibaba PuHuiTi 3.0",
            "Microsoft YaHei UI",
            "Microsoft YaHei",
            "PingFang SC",
            "Noto Sans SC",
            "Source Han Sans CN",
            "Noto Sans CJK SC",
            "WenQuanYi Micro Hei",
            "SimHei",
        ]
        for name in candidates:
            if name in self._families:
                return name
        return "Helvetica"

    def _pick_number_font(self, saved=None):
        if saved and saved not in ("Helvetica", "Courier New") and saved in self._families:
            return saved
        candidates = [
            "Cascadia Mono",
            "JetBrains Mono",
            "Consolas",
            "Menlo",
            "SF Mono",
            "DejaVu Sans Mono",
            "Noto Sans Mono",
            "Liberation Mono",
            "Courier New",
        ]
        for name in candidates:
            if name in self._families:
                return name
        return "Helvetica"

    def _create_vars(self):
        self.host_var = tk.StringVar()
        self.port_var = tk.StringVar()
        self.username_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.key_file_var = tk.StringVar()
        self.start_cmd_var = tk.StringVar()
        self.stop_cmd_var = tk.StringVar()
        self.interval_var = tk.StringVar()
        self.auto_check_var = tk.BooleanVar()
        self.remember_var = tk.BooleanVar()
        self.upload_var = tk.StringVar(value="拖入文件到此处")
        self.first_model_var = tk.StringVar()
        self.second_model_var = tk.StringVar()
        self.service_status_var = tk.StringVar(value="未检测")
        self.status_var = tk.StringVar(value="等待配置")
        self.last_check_var = tk.StringVar(value="")

        self.start_cmd_var.trace_add("write", lambda *_: self._update_command_buttons())
        self.stop_cmd_var.trace_add("write", lambda *_: self._update_command_buttons())

    def _apply_config(self):
        cfg = self.config
        self.host_var.set(str(cfg.get("host") or DEFAULT_HOST))
        self.port_var.set(str(cfg.get("port", 22)))
        self.username_var.set(str(cfg.get("username", "")))
        self.password_var.set(str(cfg.get("password", "")) if cfg.get("remember_password") else "")
        self.key_file_var.set(str(cfg.get("key_file", "")))
        self.start_cmd_var.set(str(cfg.get("start_command") or DEFAULTS["start_command"]))
        self.stop_cmd_var.set(str(cfg.get("stop_command") or DEFAULTS["stop_command"]))
        self.interval_var.set(str(cfg.get("check_interval", 5)))
        self.auto_check_var.set(bool(cfg.get("auto_check", True)))
        self.remember_var.set(bool(cfg.get("remember_password", False)))

    def _build_ui(self):
        main = tk.Frame(self.root, bg=COLORS["bg"], padx=18, pady=16)
        main.pack(fill="both", expand=True)

        header = tk.Frame(main, bg=COLORS["bg"])
        header.pack(fill="x")
        tk.Label(
            header, text=APP_TITLE, bg=COLORS["bg"], fg=COLORS["fg"],
            font=(self.font_family, 16, "bold"),
        ).pack(side="left")
        self.status_canvas = tk.Canvas(
            header, width=18, height=18, bg=COLORS["bg"], highlightthickness=0
        )
        self.status_canvas.pack(side="left", padx=(18, 6))
        self.status_dot = self.status_canvas.create_oval(
            3, 3, 15, 15, fill=COLORS["muted"], outline=""
        )
        tk.Label(
            header, textvariable=self.status_var, bg=COLORS["bg"],
            fg=COLORS["fg"], font=(self.font_family, 11, "bold"),
        ).pack(side="left")
        tk.Label(
            header, textvariable=self.last_check_var, bg=COLORS["bg"],
            fg=COLORS["muted"], font=(self.font_family, 9),
        ).pack(side="right")
        tk.Frame(main, bg=COLORS["accent2"], height=2).pack(fill="x", pady=(12, 0))

        body = tk.Frame(main, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, pady=(14, 0))
        body.columnconfigure(0, weight=1, uniform="col")
        body.columnconfigure(1, weight=1, uniform="col")
        body.rowconfigure(0, weight=1, uniform="row")
        body.rowconfigure(1, weight=1, uniform="row")

        top_left = tk.Frame(body, bg=COLORS["bg"])
        top_left.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        top_right = tk.Frame(body, bg=COLORS["bg"])
        top_right.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=(0, 6))
        bottom_left = tk.Frame(body, bg=COLORS["bg"])
        bottom_left.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(6, 0))
        bottom_right = tk.Frame(body, bg=COLORS["bg"])
        bottom_right.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(6, 0))

        self._build_connection_card(top_left)
        self._build_command_card(top_right)
        self._build_config_card(bottom_left)
        self._build_upload_card(bottom_right)
        self._build_log_card(main)

    def _build_connection_card(self, main):
        card = tk.Frame(
            main, bg=COLORS["panel"], highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        self.left_card = card
        card.pack(fill="both", expand=True, pady=(0, 0))
        inner = tk.Frame(card, bg=COLORS["panel"], padx=14, pady=10)
        inner.pack(fill="both", expand=True)

        title_row = tk.Frame(inner, bg=COLORS["panel"])
        title_row.pack(fill="x")
        self._section_title(title_row, "连接设置")
        self.detect_button = tk.Button(
            title_row, text="立即检测", command=self.detect_now,
            bg=COLORS["panel2"], fg=COLORS["fg"], activebackground=COLORS["border"],
            activeforeground=COLORS["fg"], relief="flat", padx=12, pady=3,
        )
        self.detect_button.pack(side="right")

        grid = tk.Frame(inner, bg=COLORS["panel"])
        grid.pack(fill="x", pady=(8, 0))
        grid.columnconfigure(1, weight=1, minsize=180)
        grid.columnconfigure(3, weight=1, minsize=180)

        self._grid_label(grid, "主机", 0, 0)
        self.host_entry = self._grid_entry(grid, self.host_var, 0, 1, padx=(4, 12))
        self._grid_label(grid, "端口", 0, 2)
        self.port_entry = self._grid_entry(grid, self.port_var, 0, 3, width=10, sticky="w")

        self._grid_label(grid, "用户名", 1, 0)
        self.username_entry = self._grid_entry(grid, self.username_var, 1, 1, padx=(4, 12))
        self._grid_label(grid, "密码", 1, 2)
        password_frame = tk.Frame(grid, bg=COLORS["panel"])
        password_frame.grid(row=1, column=3, sticky="ew", padx=(4, 0), pady=4)
        password_frame.columnconfigure(0, weight=1)
        self.password_entry = tk.Entry(
            password_frame, textvariable=self.password_var, show="•", bg=COLORS["panel2"],
            fg=COLORS["fg"], insertbackground=COLORS["fg"], relief="flat", bd=0,
            highlightthickness=1, highlightbackground=COLORS["border"],
            highlightcolor=COLORS["accent"],
        )
        self.password_entry.grid(row=0, column=0, sticky="ew")
        self.password_toggle = tk.Button(
            password_frame, text="显示", command=self._toggle_password,
            bg=COLORS["panel2"], fg=COLORS["muted"], activebackground=COLORS["border"],
            activeforeground=COLORS["fg"], relief="flat", padx=8, pady=2,
        )
        self.password_toggle.grid(row=0, column=1, padx=(6, 0))

        self._grid_label(grid, "私钥文件", 2, 0)
        key_frame = tk.Frame(grid, bg=COLORS["panel"])
        key_frame.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(4, 0), pady=4)
        key_frame.columnconfigure(0, weight=1)
        self.key_entry = tk.Entry(
            key_frame, textvariable=self.key_file_var, bg=COLORS["panel2"],
            fg=COLORS["fg"], insertbackground=COLORS["fg"], relief="flat", bd=0,
            highlightthickness=1, highlightbackground=COLORS["border"],
            highlightcolor=COLORS["accent"],
        )
        self.key_entry.grid(row=0, column=0, sticky="ew")
        tk.Button(
            key_frame, text="浏览", command=self._browse_key,
            bg=COLORS["panel2"], fg=COLORS["muted"], activebackground=COLORS["border"],
            activeforeground=COLORS["fg"], relief="flat", padx=10, pady=2,
        ).grid(row=0, column=1, padx=(6, 0))

        options = tk.Frame(inner, bg=COLORS["panel"])
        options.pack(fill="x", pady=(8, 0))
        tk.Checkbutton(
            options, text="自动检测连接", variable=self.auto_check_var,
            bg=COLORS["panel"], fg=COLORS["fg"], selectcolor=COLORS["panel2"],
            activebackground=COLORS["panel"], activeforeground=COLORS["fg"],
            highlightthickness=0,
        ).pack(side="left")
        tk.Label(
            options, text="间隔(秒)", bg=COLORS["panel"], fg=COLORS["muted"],
        ).pack(side="left", padx=(14, 4))
        tk.Spinbox(
            options, from_=2, to=120, textvariable=self.interval_var, width=5,
            bg=COLORS["panel2"], fg=COLORS["fg"], buttonbackground=COLORS["panel2"],
            relief="flat",
        ).pack(side="left")
        tk.Checkbutton(
            options, text="记住密码", variable=self.remember_var,
            bg=COLORS["panel"], fg=COLORS["muted"], selectcolor=COLORS["panel2"],
            activebackground=COLORS["panel"], activeforeground=COLORS["fg"],
            highlightthickness=0,
        ).pack(side="left", padx=(18, 0))
        tk.Button(
            options, text="保存设置", command=self.save_settings,
            bg=COLORS["panel2"], fg=COLORS["fg"], activebackground=COLORS["border"],
            activeforeground=COLORS["fg"], relief="flat", padx=12, pady=3,
        ).pack(side="right")

    def _build_control_card(self, main):
        card = tk.Frame(
            main, bg=COLORS["panel"], highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        self.control_card = card
        card.pack(fill="both", expand=True)
        inner = tk.Frame(card, bg=COLORS["panel"], padx=14, pady=10)
        inner.pack(fill="both", expand=True)

        title_row = tk.Frame(inner, bg=COLORS["panel"])
        title_row.pack(fill="x")
        self._section_title(title_row, "远程控制")
        tk.Label(
            title_row, textvariable=self.host_var, bg=COLORS["panel"],
            fg=COLORS["muted"], font=(self.number_family, 9),
        ).pack(side="right")

        status_row = tk.Frame(inner, bg=COLORS["panel"])
        status_row.pack(fill="x", pady=(10, 0))
        tk.Label(
            status_row, text="运行状态:", bg=COLORS["panel"],
            fg=COLORS["muted"], font=(self.font_family, 10),
        ).pack(side="left")
        self.service_status_label = tk.Label(
            status_row, textvariable=self.service_status_var,
            bg=COLORS["panel"], fg=COLORS["muted"],
            font=(self.font_family, 10, "bold"),
        )
        self.service_status_label.pack(side="left", padx=(6, 0))
        self.status_check_button = tk.Button(
            status_row, text="检测状态", command=self._check_status,
            bg=COLORS["panel2"], fg=COLORS["fg"], activebackground=COLORS["border"],
            activeforeground=COLORS["fg"], relief="flat", padx=10, pady=2,
        )
        self.status_check_button.pack(side="right")

        buttons = tk.Frame(inner, bg=COLORS["panel"])
        buttons.pack(fill="x", pady=(8, 0))
        self.start_button = tk.Button(
            buttons, text="启动", command=self.run_start, width=9,
            bg=COLORS["accent_dim"], fg="#06120b", activebackground=COLORS["accent"],
            activeforeground="#06120b", disabledforeground="#5c6f62",
            relief="flat", padx=10, pady=4, font=(self.font_family, 11, "bold"),
        )
        self.start_button.pack(side="left")
        self.stop_button = tk.Button(
            buttons, text="停止", command=self.run_stop, width=9,
            bg=COLORS["danger_dim"], fg="#ffffff", activebackground=COLORS["danger"],
            activeforeground="#ffffff", disabledforeground="#73524e",
            relief="flat", padx=10, pady=4, font=(self.font_family, 11, "bold"),
        )
        self.stop_button.pack(side="left", padx=(10, 0))

        tk.Frame(inner, bg=COLORS["border2"], height=1).pack(
            fill="x", pady=(10, 0)
        )

        config_title = tk.Frame(inner, bg=COLORS["panel"])
        config_title.pack(fill="x", pady=(10, 0))
        self._section_title(config_title, "远程配置")
        tk.Label(
            config_title, text=REMOTE_CONFIG_PATH, bg=COLORS["panel"],
            fg=COLORS["muted"], font=(self.font_family, 9),
        ).pack(side="right")

        grid = tk.Frame(inner, bg=COLORS["panel"])
        grid.pack(fill="x", pady=(8, 0))
        grid.columnconfigure(1, weight=1)
        self._grid_label(grid, "第一个模型 name", 0, 0)
        self._grid_entry(grid, self.first_model_var, 0, 1, padx=(4, 0))
        self._grid_label(grid, "第二个模型 name", 1, 0)
        self._grid_entry(grid, self.second_model_var, 1, 1, padx=(4, 0))

        config_buttons = tk.Frame(inner, bg=COLORS["panel"])
        config_buttons.pack(fill="x", pady=(8, 0))
        self.config_read_button = tk.Button(
            config_buttons, text="读取配置", command=self._read_config,
            bg=COLORS["panel2"], fg=COLORS["fg"], activebackground=COLORS["border"],
            activeforeground=COLORS["fg"], relief="flat", padx=12, pady=3,
        )
        self.config_read_button.pack(side="right")
        self.config_save_button = tk.Button(
            config_buttons, text="保存配置", command=self._save_config,
            bg=COLORS["accent_dim"], fg="#06120b", activebackground=COLORS["accent"],
            activeforeground="#06120b", disabledforeground="#5c6f62",
            relief="flat", padx=12, pady=3, font=(self.font_family, 10, "bold"),
        )
        self.config_save_button.pack(side="right", padx=(10, 0))

        tk.Frame(inner, bg=COLORS["border2"], height=1).pack(
            fill="x", pady=(10, 0)
        )

        upload_title = tk.Frame(inner, bg=COLORS["panel"])
        upload_title.pack(fill="x", pady=(10, 0))
        self._section_title(upload_title, "上传文件")
        tk.Label(
            upload_title, text="/opt/ac_ws/models/", bg=COLORS["panel"],
            fg=COLORS["muted"], font=(self.font_family, 9),
        ).pack(side="right")

        drop = tk.Frame(
            inner, bg=COLORS["panel2"], highlightbackground=COLORS["border"],
            highlightthickness=1, padx=10, pady=6,
        )
        drop.pack(fill="x", pady=(8, 0))
        drop.columnconfigure(0, weight=1)
        tk.Label(
            drop, textvariable=self.upload_var, bg=COLORS["panel2"],
            fg=COLORS["muted"], anchor="w", font=(self.font_family, 10),
        ).grid(row=0, column=0, sticky="ew")
        tk.Button(
            drop, text="选择文件", command=self._choose_file,
            bg=COLORS["panel2"], fg=COLORS["fg"], activebackground=COLORS["border"],
            activeforeground=COLORS["fg"], relief="flat", padx=10, pady=3,
        ).grid(row=0, column=1, padx=(10, 0))
        self.upload_button = tk.Button(
            drop, text="上传", command=self._upload_file,
            bg=COLORS["accent_dim"], fg="#06120b", activebackground=COLORS["accent"],
            activeforeground="#06120b", disabledforeground="#5c6f62",
            relief="flat", padx=14, pady=3, font=(self.font_family, 10, "bold"),
        )
        self.upload_button.grid(row=0, column=2, padx=(10, 0))

        if HAS_TKDND:
            drop.drop_target_register(DND_FILES)
            drop.dnd_bind("<<Drop>>", self._on_file_drop)
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_file_drop)

    def _build_command_card(self, main):
        card = tk.Frame(
            main, bg=COLORS["panel"], highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        self.command_card = card
        card.pack(fill="both", expand=True)
        inner = tk.Frame(card, bg=COLORS["panel"], padx=14, pady=8)
        inner.pack(fill="both", expand=True)

        title_row = tk.Frame(inner, bg=COLORS["panel"])
        title_row.pack(fill="x")
        self._section_title(title_row, "远程控制")

        buttons = tk.Frame(inner, bg=COLORS["panel"])
        buttons.pack(fill="x", pady=(8, 0))
        self.start_button = tk.Button(
            buttons, text="启动", command=self.run_start, width=9,
            bg=COLORS["accent_dim"], fg="#06120b", activebackground=COLORS["accent"],
            activeforeground="#06120b", disabledforeground="#5c6f62",
            relief="flat", padx=10, pady=4, font=(self.font_family, 11, "bold"),
        )
        self.start_button.pack(side="left")
        self.stop_button = tk.Button(
            buttons, text="停止", command=self.run_stop, width=9,
            bg=COLORS["danger_dim"], fg="#ffffff", activebackground=COLORS["danger"],
            activeforeground="#ffffff", disabledforeground="#73524e",
            relief="flat", padx=10, pady=4, font=(self.font_family, 11, "bold"),
        )
        self.stop_button.pack(side="left", padx=(10, 0))

        status_row = tk.Frame(inner, bg=COLORS["panel"])
        status_row.pack(fill="x", pady=(8, 0))
        tk.Label(
            status_row, text="运行状态:", bg=COLORS["panel"],
            fg=COLORS["muted"], font=(self.font_family, 10),
        ).pack(side="left")
        self.service_status_label = tk.Label(
            status_row, textvariable=self.service_status_var,
            bg=COLORS["panel"], fg=COLORS["muted"],
            font=(self.font_family, 10, "bold"),
        )
        self.service_status_label.pack(side="left", padx=(6, 0))
        self.status_check_button = tk.Button(
            status_row, text="检测状态", command=self._check_status,
            bg=COLORS["panel2"], fg=COLORS["fg"], activebackground=COLORS["border"],
            activeforeground=COLORS["fg"], relief="flat", padx=10, pady=2,
        )
        self.status_check_button.pack(side="right")

    def _build_upload_card(self, main):
        card = tk.Frame(
            main, bg=COLORS["panel"], highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        self.upload_card = card
        card.pack(fill="both", expand=True)
        inner = tk.Frame(card, bg=COLORS["panel"], padx=14, pady=8)
        inner.pack(fill="both", expand=True)

        title_row = tk.Frame(inner, bg=COLORS["panel"])
        title_row.pack(fill="x")
        self._section_title(title_row, "上传文件")
        tk.Label(
            title_row, text="/opt/ac_ws/models/", bg=COLORS["panel"],
            fg=COLORS["muted"], font=(self.font_family, 9),
        ).pack(side="right")

        drop = tk.Frame(
            inner, bg=COLORS["panel2"], highlightbackground=COLORS["border"],
            highlightthickness=1, padx=10, pady=6,
        )
        drop.pack(fill="x", pady=(8, 0))
        drop.columnconfigure(0, weight=1)
        tk.Label(
            drop, textvariable=self.upload_var, bg=COLORS["panel2"],
            fg=COLORS["muted"], anchor="w", font=(self.font_family, 10),
        ).grid(row=0, column=0, sticky="ew")
        tk.Button(
            drop, text="选择文件", command=self._choose_file,
            bg=COLORS["panel2"], fg=COLORS["fg"], activebackground=COLORS["border"],
            activeforeground=COLORS["fg"], relief="flat", padx=10, pady=3,
        ).grid(row=0, column=1, padx=(10, 0))
        self.upload_button = tk.Button(
            drop, text="上传", command=self._upload_file,
            bg=COLORS["accent_dim"], fg="#06120b", activebackground=COLORS["accent"],
            activeforeground="#06120b", disabledforeground="#5c6f62",
            relief="flat", padx=14, pady=3, font=(self.font_family, 10, "bold"),
        )
        self.upload_button.grid(row=0, column=2, padx=(10, 0))

        if HAS_TKDND:
            drop.drop_target_register(DND_FILES)
            drop.dnd_bind("<<Drop>>", self._on_file_drop)
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_file_drop)

    def _build_config_card(self, main):
        card = tk.Frame(
            main, bg=COLORS["panel"], highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        self.config_card = card
        card.pack(fill="both", expand=True)
        inner = tk.Frame(card, bg=COLORS["panel"], padx=14, pady=8)
        inner.pack(fill="both", expand=True)

        title_row = tk.Frame(inner, bg=COLORS["panel"])
        title_row.pack(fill="x")
        self._section_title(title_row, "远程配置")
        tk.Label(
            title_row, text=REMOTE_CONFIG_PATH, bg=COLORS["panel"],
            fg=COLORS["muted"], font=(self.font_family, 9),
        ).pack(side="right")

        grid = tk.Frame(inner, bg=COLORS["panel"])
        grid.pack(fill="x", pady=(8, 0))
        grid.columnconfigure(1, weight=1)
        self._grid_label(grid, "第一个模型 name", 0, 0)
        self._grid_entry(grid, self.first_model_var, 0, 1, padx=(4, 0))
        self._grid_label(grid, "第二个模型 name", 1, 0)
        self._grid_entry(grid, self.second_model_var, 1, 1, padx=(4, 0))

        buttons = tk.Frame(inner, bg=COLORS["panel"])
        buttons.pack(fill="x", pady=(8, 0))
        self.config_read_button = tk.Button(
            buttons, text="读取配置", command=self._read_config,
            bg=COLORS["panel2"], fg=COLORS["fg"], activebackground=COLORS["border"],
            activeforeground=COLORS["fg"], relief="flat", padx=12, pady=3,
        )
        self.config_read_button.pack(side="right")
        self.config_save_button = tk.Button(
            buttons, text="保存配置", command=self._save_config,
            bg=COLORS["accent_dim"], fg="#06120b", activebackground=COLORS["accent"],
            activeforeground="#06120b", disabledforeground="#5c6f62",
            relief="flat", padx=12, pady=3, font=(self.font_family, 10, "bold"),
        )
        self.config_save_button.pack(side="right", padx=(10, 0))

    def _on_file_drop(self, event):
        files = self.root.tk.splitlist(event.data)
        if files:
            self._upload_file(files[0])

    def _choose_file(self):
        path = filedialog.askopenfilename(title="选择要上传的文件")
        if path:
            self.upload_var.set(path)
            self._append_log(f"已选择文件：{path}", "muted")

    def _upload_file(self, local_path=None):
        if self.command_busy:
            return
        path = local_path or self.upload_var.get().strip()
        if not path or path == "拖入文件到此处":
            self._append_log("请先选择或拖入要上传的文件", "bad")
            return
        if not os.path.isfile(path):
            self._append_log(f"文件不存在：{path}", "bad")
            return
        self.upload_var.set(path)
        try:
            settings = self._settings()
        except ValueError as exc:
            self._set_status("bad", "配置无效", str(exc))
            return
        self._set_command_busy(True)
        threading.Thread(
            target=self._upload_worker, args=(settings, path), daemon=True
        ).start()

    def _upload_worker(self, settings, local_path):
        name = os.path.basename(local_path)
        remote_path = "/opt/ac_ws/models/" + name
        self._post_log(f"正在上传 {name} 到 /opt/ac_ws/models/", "muted")
        try:
            self.manager.upload(settings, local_path)
        except paramiko.AuthenticationException:
            self._post_status("bad", "无法连接", "认证失败，请检查用户名、密码或私钥")
            self._post_log("上传失败：认证失败", "bad")
        except (socket.timeout, socket.gaierror, paramiko.SSHException, OSError) as exc:
            message = str(exc).strip() or type(exc).__name__
            self._post_status("bad", "无法连接", message)
            self._post_log(f"上传失败：{message}", "bad")
        except ValueError as exc:
            self._post_status("bad", "配置无效", str(exc))
            self._post_log(f"上传失败：{exc}", "bad")
        else:
            self._post_status("ok", "已连接", "文件上传完成")
            self._post_log(f"上传完成：{remote_path}", "ok")
        finally:
            self.message_queue.put(("busy", False))

    def _read_config(self):
        if self.command_busy:
            return
        try:
            settings = self._settings()
        except ValueError as exc:
            self._set_status("bad", "配置无效", str(exc))
            return
        self._set_command_busy(True)
        threading.Thread(
            target=self._read_config_worker, args=(settings,), daemon=True
        ).start()

    def _read_config_worker(self, settings):
        self._post_log(f"正在读取 {REMOTE_CONFIG_PATH}", "muted")
        try:
            text = self.manager.read_remote_text(settings, REMOTE_CONFIG_PATH)
            names = RobotYaml.read_names(text)
            self.message_queue.put(
                ("config", names["first_model_config"], names["second_model_config"])
            )
            self._post_log("配置读取完成", "ok")
        except paramiko.AuthenticationException:
            self._post_status("bad", "无法连接", "认证失败，请检查用户名、密码或私钥")
            self._post_log("读取配置失败：认证失败", "bad")
        except (socket.timeout, socket.gaierror, paramiko.SSHException, OSError) as exc:
            message = str(exc).strip() or type(exc).__name__
            self._post_status("bad", "无法连接", message)
            self._post_log(f"读取配置失败：{message}", "bad")
        except ValueError as exc:
            self._post_log(f"读取配置失败：{exc}", "bad")
        finally:
            self.message_queue.put(("busy", False))

    def _save_config(self):
        if self.command_busy:
            return
        first = self.first_model_var.get().strip()
        second = self.second_model_var.get().strip()
        if not first or not second:
            self._append_log("请先读取配置并填写两个 name", "bad")
            return
        try:
            settings = self._settings()
        except ValueError as exc:
            self._set_status("bad", "配置无效", str(exc))
            return
        self._set_command_busy(True)
        threading.Thread(
            target=self._save_config_worker,
            args=(settings, first, second),
            daemon=True,
        ).start()

    def _save_config_worker(self, settings, first, second):
        self._post_log(f"正在保存 {REMOTE_CONFIG_PATH}", "muted")
        try:
            text = self.manager.read_remote_text(settings, REMOTE_CONFIG_PATH)
            text = RobotYaml.update_name(text, "first_model_config", first)
            text = RobotYaml.update_name(text, "second_model_config", second)
            self.manager.write_remote_text(settings, REMOTE_CONFIG_PATH, text)
            self._post_log("配置保存完成", "ok")
        except paramiko.AuthenticationException:
            self._post_status("bad", "无法连接", "认证失败，请检查用户名、密码或私钥")
            self._post_log("保存配置失败：认证失败", "bad")
        except (socket.timeout, socket.gaierror, paramiko.SSHException, OSError) as exc:
            message = str(exc).strip() or type(exc).__name__
            self._post_status("bad", "无法连接", message)
            self._post_log(f"保存配置失败：{message}", "bad")
        except ValueError as exc:
            self._post_log(f"保存配置失败：{exc}", "bad")
        finally:
            self.message_queue.put(("busy", False))

    def _check_status(self):
        if self.command_busy:
            return
        try:
            settings = self._settings()
        except ValueError as exc:
            self._set_status("bad", "配置无效", str(exc))
            return
        self._set_command_busy(True)
        threading.Thread(
            target=self._status_worker, args=(settings,), daemon=True
        ).start()

    def _status_worker(self, settings):
        self._post_log("正在检测运行状态...", "muted")
        try:
            running, pid_path = self.manager.check_service_status(settings)
            self.message_queue.put(("service_status", running, pid_path))
        except paramiko.AuthenticationException:
            self._post_status("bad", "无法连接", "认证失败，请检查用户名、密码或私钥")
            self._post_log("状态检测失败：认证失败", "bad")
        except (socket.timeout, socket.gaierror, paramiko.SSHException, OSError) as exc:
            message = str(exc).strip() or type(exc).__name__
            self._post_status("bad", "无法连接", message)
            self._post_log(f"状态检测失败：{message}", "bad")
        except ValueError as exc:
            self._post_log(f"状态检测失败：{exc}", "bad")
        finally:
            self.message_queue.put(("busy", False))

    def _build_log_card(self, main):
        card = tk.Frame(
            main, bg=COLORS["panel"], highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        card.pack(fill="x", pady=(10, 0))
        inner = tk.Frame(card, bg=COLORS["panel"], padx=14, pady=10)
        inner.pack(fill="both", expand=True)

        title_row = tk.Frame(inner, bg=COLORS["panel"])
        title_row.pack(fill="x")
        self._section_title(title_row, "执行日志")
        self.log_text = scrolledtext.ScrolledText(
            inner, bg=COLORS["bg"], fg=COLORS["fg"], insertbackground=COLORS["fg"],
            relief="flat", wrap="word", height=8,
            font=(self.number_family, 9),
        )
        self.log_text.pack(fill="both", expand=True, pady=(8, 0))
        self.log_text.tag_configure("ok", foreground=COLORS["accent"])
        self.log_text.tag_configure("bad", foreground=COLORS["danger"])
        self.log_text.tag_configure("muted", foreground=COLORS["muted"])
        self.log_text.tag_configure("out", foreground=COLORS["fg"])
        self.log_text.configure(state="disabled")

    def _grid_label(self, parent, text, row, column):
        tk.Label(
            parent, text=text, bg=COLORS["panel"], fg=COLORS["muted"],
        ).grid(row=row, column=column, sticky="w", pady=4)

    def _section_title(self, parent, text):
        tk.Frame(
            parent, bg=COLORS["accent2"], width=3, height=16,
        ).pack(side="left", padx=(0, 7))
        tk.Label(
            parent, text=text, bg=parent.cget("bg"), fg=COLORS["fg"],
            font=(self.font_family, 12, "bold"),
        ).pack(side="left")

    def _grid_entry(self, parent, var, row, column, width=None, sticky="ew", padx=(0, 0)):
        options = {"width": width} if width is not None else {}
        entry = tk.Entry(
            parent, textvariable=var, bg=COLORS["panel2"], fg=COLORS["fg"],
            insertbackground=COLORS["fg"], relief="flat", bd=0,
            highlightthickness=1, highlightbackground=COLORS["border"],
            highlightcolor=COLORS["accent"], **options,
        )
        entry.grid(row=row, column=column, sticky=sticky, padx=padx, pady=4)
        return entry

    def _set_status(self, kind, text, detail=""):
        colors = {
            "ok": COLORS["accent"],
            "bad": COLORS["danger"],
            "check": COLORS["warn"],
            "idle": COLORS["muted"],
        }
        self.status_canvas.itemconfigure(self.status_dot, fill=colors[kind])
        self.status_var.set(text)
        suffix = f" · {detail}" if detail else ""
        self.last_check_var.set(f"最后检测 {time.strftime('%H:%M:%S')}{suffix}")
        if kind == "ok" and not self.service_status_checked:
            self.service_status_checked = True
            self.root.after(300, self._check_status)

    def _post_status(self, kind, text, detail=""):
        self.message_queue.put(("status", kind, text, detail))

    def _settings(self):
        port_text = self.port_var.get().strip()
        try:
            port = int(port_text)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            raise ValueError("端口必须是 1-65535 的数字")
        host = self.host_var.get().strip()
        username = self.username_var.get().strip()
        if not host:
            raise ValueError("请填写主机地址")
        if not username:
            raise ValueError("请填写 SSH 用户名")
        return {
            "host": host,
            "port": port,
            "username": username,
            "password": self.password_var.get(),
            "key_file": self.key_file_var.get().strip(),
        }

    def _auto_tick(self):
        if self.check_stop.is_set() or not self.auto_check_var.get():
            return
        try:
            settings = self._settings()
        except ValueError as exc:
            self._set_status("idle", "等待配置", str(exc))
        else:
            threading.Thread(
                target=self._check_worker, args=(settings, False), daemon=True
            ).start()
        try:
            interval = max(2, int(self.interval_var.get()))
        except ValueError:
            interval = 5
        self.root.after(interval * 1000, self._auto_tick)

    def _auto_check_toggled(self):
        if self.auto_check_var.get() and not self.check_stop.is_set():
            self.root.after(200, self._auto_tick)

    def detect_now(self):
        if self.checking or self.command_busy:
            return
        try:
            settings = self._settings()
        except ValueError as exc:
            self._set_status("bad", "配置无效", str(exc))
            return
        threading.Thread(
            target=self._check_worker, args=(settings, True), daemon=True
        ).start()

    def _check_worker(self, settings, force):
        if self.checking:
            return
        self.checking = True
        self._post_status("check", "正在检测连接...")
        started = time.monotonic()
        try:
            self.manager.connect(settings, force=force)
            elapsed = time.monotonic() - started
            self._post_status("ok", "已连接", f"耗时 {elapsed:.2f}s")
        except paramiko.AuthenticationException:
            self._post_status("bad", "无法连接", "认证失败，请检查用户名、密码或私钥")
        except (socket.timeout, socket.gaierror, paramiko.SSHException, OSError) as exc:
            message = str(exc).strip() or type(exc).__name__
            self._post_status("bad", "无法连接", message)
        except ValueError as exc:
            self._post_status("bad", "配置无效", str(exc))
        finally:
            self.checking = False

    def run_start(self):
        self._execute_command("start", self.start_cmd_var.get().strip())

    def run_stop(self):
        self._execute_command("stop", self.stop_cmd_var.get().strip())

    def _execute_command(self, kind, command):
        if self.command_busy:
            return
        if not command:
            self._append_log("请先填写命令再执行", "bad")
            return
        try:
            settings = self._settings()
        except ValueError as exc:
            self._set_status("bad", "配置无效", str(exc))
            return
        self._set_command_busy(True)
        threading.Thread(
            target=self._run_command_worker, args=(kind, settings, command), daemon=True
        ).start()

    def _run_command_worker(self, kind, settings, command):
        label = "启动" if kind == "start" else "停止"
        self._post_log(f"正在通过 SSH 执行{label}命令: {command}", "muted")
        try:
            code, out, err = self.manager.run(settings, command)
        except paramiko.AuthenticationException:
            self._post_status("bad", "无法连接", "认证失败，请检查用户名、密码或私钥")
            self._post_log("执行失败：认证失败", "bad")
        except (socket.timeout, socket.gaierror, paramiko.SSHException, OSError) as exc:
            message = str(exc).strip() or type(exc).__name__
            self._post_status("bad", "无法连接", message)
            self._post_log(f"执行失败：{message}", "bad")
        except ValueError as exc:
            self._post_status("bad", "配置无效", str(exc))
            self._post_log(f"执行失败：{exc}", "bad")
        else:
            self._post_status("ok", "已连接", "命令已执行")
            tag = "ok" if code == 0 else "bad"
            self._post_log(f"{label}命令执行完成，退出码 {code}", tag)
            if out.strip():
                self._post_log(out.rstrip(), "out")
            if err.strip():
                self._post_log(err.rstrip(), "bad")
            self.message_queue.put(("status_refresh",))
        finally:
            self.message_queue.put(("busy", False))

    def _set_command_busy(self, busy):
        self.command_busy = busy
        self._update_command_buttons()
        self.detect_button.configure(state="disabled" if busy else "normal")
        self.upload_button.configure(state="disabled" if busy else "normal")
        self.config_read_button.configure(state="disabled" if busy else "normal")
        self.config_save_button.configure(state="disabled" if busy else "normal")
        self.status_check_button.configure(state="disabled" if busy else "normal")

    def _update_command_buttons(self):
        start_ready = bool(self.start_cmd_var.get().strip()) and not self.command_busy
        stop_ready = bool(self.stop_cmd_var.get().strip()) and not self.command_busy
        self.start_button.configure(state="normal" if start_ready else "disabled")
        self.stop_button.configure(state="normal" if stop_ready else "disabled")

    def _post_log(self, text, tag):
        self.message_queue.put(("log", text, tag))

    def _poll_messages(self):
        try:
            while True:
                kind, *payload = self.message_queue.get_nowait()
                if kind == "status":
                    self._set_status(*payload)
                elif kind == "log":
                    self._append_log(*payload)
                elif kind == "busy":
                    self._set_command_busy(payload[0])
                elif kind == "config":
                    self.first_model_var.set(payload[0] or "")
                    self.second_model_var.set(payload[1] or "")
                elif kind == "service_status":
                    running, pid_path = payload
                    self.service_status_var.set("已启动" if running else "未启动")
                    self.service_status_label.configure(
                        fg=COLORS["accent"] if running else COLORS["muted"]
                    )
                    self._append_log(
                        f"运行状态：{'已启动' if running else '未启动'}（{pid_path}）",
                        "ok" if running else "muted",
                    )
                elif kind == "status_refresh":
                    self.root.after(300, self._check_status)
        except queue.Empty:
            pass
        if not self.check_stop.is_set():
            self.root.after(60, self._poll_messages)

    def _append_log(self, text, tag):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n", tag)
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 800:
            self.log_text.delete("1.0", f"{line_count - 600}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _toggle_password(self):
        self.show_password = not self.show_password
        self.password_entry.configure(show="" if self.show_password else "•")
        self.password_toggle.configure(text="隐藏" if self.show_password else "显示")

    def _browse_key(self):
        path = filedialog.askopenfilename(
            title="选择 SSH 私钥文件",
            filetypes=[("私钥文件", "*"), ("所有文件", "*")],
        )
        if path:
            self.key_file_var.set(path)

    def save_settings(self, silent=False):
        try:
            settings = self._settings()
        except ValueError as exc:
            if not silent:
                self._append_log(f"设置未保存：{exc}", "bad")
            return
        try:
            interval = max(2, int(self.interval_var.get()))
        except ValueError:
            interval = 5
        self.config.update(
            {
                "host": settings["host"],
                "port": settings["port"],
                "username": settings["username"],
                "key_file": settings["key_file"],
                "start_command": self.start_cmd_var.get().strip(),
                "stop_command": self.stop_cmd_var.get().strip(),
                "auto_check": bool(self.auto_check_var.get()),
                "check_interval": interval,
                "remember_password": bool(self.remember_var.get()),
                "ui_font": self.font_family,
                "number_font": self.number_family,
            }
        )
        if self.remember_var.get():
            self.config["password"] = self.password_var.get()
        else:
            self.config.pop("password", None)
        save_config(self.config)
        if not silent:
            self._append_log("设置已保存", "ok")

    def close(self):
        self.check_stop.set()
        self.save_settings(silent=True)
        self.manager.close()
        self.root.destroy()


if __name__ == "__main__":
    SshControlApp().root.mainloop()
