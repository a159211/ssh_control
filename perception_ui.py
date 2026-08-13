#!/usr/bin/env python3
"""SSH 感知代码启动界面（bringup1 / bringup2 双面板）。

进入 /home/ac/Documents/AC/work_ws 分别执行 bash bringup1.sh / bash bringup2.sh，
每个面板独立通过 `ros2 node list` 查询节点来判断对应的 ros2 launch 是否已启动。
"""

import os
import queue
import shlex
import socket
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import scrolledtext

try:
    from tkinterdnd2 import TkinterDnD
    HAS_TKDND = True
except ImportError:
    TkinterDnD = None
    HAS_TKDND = False

try:
    import paramiko
except ImportError:
    raise SystemExit(
        "缺少 paramiko，请确认 vendor 依赖目录存在，或使用 ./run_perception.sh 启动"
    )

from ssh_control import COLORS, PROFILES, SshManager, load_config, save_config

PANEL_STATUS_COLORS = {
    "ok": COLORS["accent"],
    "idle": COLORS["muted"],
    "bad": COLORS["danger"],
    "check": COLORS["warn"],
}


def detect_command(workdir):
    """生成 ros2 node list 检测命令，自动加载 /opt/ros 与工作区环境。"""
    return (
        "cd " + shlex.quote(workdir) + " && bash -c '"
        "for d in /opt/ros/*/; do [ -f \"$d/setup.bash\" ] && . \"$d/setup.bash\"; done; "
        "[ -f install/setup.bash ] && . install/setup.bash; "
        "timeout 15 ros2 node list'"
    )


def start_command(workdir, script):
    """启动命令：setsid 新建会话后台运行脚本，立即返回。

    setsid 让脚本成为新会话/进程组组长，之后 ros2 launch 拉起的节点默认
    都在同一进程组里，停止时按进程组整体杀掉即可。PID 记录到
    /tmp/<script>.pid 供停止命令使用；用脚本自身的 $$ 写 PID，避免
    setsid 在特殊情况下 fork 后 $! 记录到已退出的父进程。

    后台任务必须放在立刻退出的内层子 shell ( ... & ) 里：直接
    cmd & 时 bash 会为后台列表另开一个子 shell 并在里面等 cmd 结束，
    那个子 shell 一直占着 SSH 通道的 stdout/stderr，导致命令读不到
    结束、界面按钮被阻塞。
    </dev/null 必须带上：否则后台 ros2 进程会一直占着 SSH 通道的 stdin，
    导致命令读取不到结束，界面按钮被阻塞。
    """
    inner = "echo $$ > /tmp/" + script + ".pid; exec bash " + shlex.quote(script)
    return (
        "cd " + shlex.quote(workdir) + " && (setsid bash -c "
        + shlex.quote(inner)
        + " </dev/null >/tmp/" + script + ".log 2>&1 &)"
    )


def stop_command(workdir, script, keyword=""):
    """停止命令：三重杀灭并输出诊断信息。

    1) 按启动时记录的 PID 杀整个进程组（ros2 节点是脚本的子进程，只杀
       脚本自身会留下孤儿节点），TERM 后等 1 秒再 KILL 兜底；
    2) pkill 匹配 "bash <脚本名>"，覆盖 pid 文件缺失/失效的情况；
    3) 按检测关键字 pkill 节点进程（如 depth_image_bridge_node），
       覆盖从旧版本启动、已脱离进程组的孤儿节点。
    最后清理 pid 文件并列出仍存活的相关进程，方便排查。
    pkill 模式用 [b]ring1.sh / [d]epth... 写法，避免匹配到本次 ssh
    命令自身的命令行而误杀。
    """
    pid_file = "/tmp/" + script + ".pid"
    script_pattern = "bash " + "[" + script[0] + "]" + script[1:]
    parts = [
        "cd " + shlex.quote(workdir),
        "echo \"== 停止 " + script + " ==\"",
        "pidfile=" + shlex.quote(pid_file),
        "pid=$(cat \"$pidfile\" 2>/dev/null)",
        "echo \"pid 文件内容: ${pid:-无}\"",
        "if [ -n \"$pid\" ]; then "
        + "kill -TERM -\"$pid\" 2>&1; echo \"进程组 TERM rc=$?\"; "
        + "sleep 1; "
        + "kill -KILL -\"$pid\" 2>&1; echo \"进程组 KILL rc=$?\"; "
        + "fi",
        "pkill -f " + shlex.quote(script_pattern) + " 2>&1; echo \"pkill 脚本 rc=$?\"",
    ]
    if keyword:
        kw_pattern = "[" + keyword[0] + "]" + keyword[1:]
        parts += [
            "pkill -f " + shlex.quote(kw_pattern) + " 2>&1; echo \"pkill 节点 rc=$?\"",
            "pkill -9 -f " + shlex.quote(kw_pattern) + " 2>&1; echo \"pkill -9 节点 rc=$?\"",
        ]
    parts += [
        "rm -f \"$pidfile\"",
        "echo \"== 停止后相关进程 ==\"",
        "ps -ef | grep -E 'bringup|depth_image_bridge|camera_container|fastdds' | grep -v grep || echo \"(无相关进程)\"",
    ]
    return " ; ".join(parts)


class PerceptionApp:
    def __init__(self):
        self.profile = PROFILES["perception"]
        self.config = load_config(
            self.profile["config_path"], self.profile["defaults"]
        )
        self.workdir = self.profile["workdir"]
        self.panels = self._load_panels()
        self.manager = SshManager()
        self.checking = False
        self.checking_since = None
        self.command_busy = False
        self.show_password = False
        self.initial_checked = False
        self.check_stop = threading.Event()
        self.message_queue = queue.Queue()
        self.panel_running = [None] * len(self.panels)
        self.panel_start_buttons = {}
        self.panel_stop_buttons = {}
        self.panel_detect_buttons = {}
        self.panel_status_labels = {}

        self.root = TkinterDnD.Tk() if HAS_TKDND else tk.Tk()
        self.root.title(self.profile["title"])
        self.root.configure(bg=COLORS["bg"])
        self.root.geometry("1180x760")
        self.root.minsize(960, 600)
        self._families = set(tkfont.families(self.root))
        self.font_family = self._pick_font(self.config.get("ui_font"))
        self.number_family = self._pick_number_font(self.config.get("number_font"))

        self._create_vars()
        self._build_ui()
        self._apply_config()
        self.auto_check_var.trace_add("write", lambda *_: self._auto_check_toggled())
        self._update_panel_buttons()

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

    def _load_panels(self):
        # 面板命令和检测关键字全部由代码按当前配置生成，不读取配置文件里
        # 保存的旧值，避免旧配置覆盖新逻辑导致失效
        return [
            {
                "name": panel["name"],
                "script": panel["script"],
                "start_command": start_command(self.workdir, panel["script"]),
                "stop_command": stop_command(
                    self.workdir, panel["script"], panel.get("detect_keyword", "")
                ),
                "detect_command": detect_command(self.workdir),
                "detect_keyword": panel.get("detect_keyword", ""),
            }
            for panel in self.profile["panels"]
        ]

    def _create_vars(self):
        self.host_var = tk.StringVar()
        self.port_var = tk.StringVar()
        self.username_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.key_file_var = tk.StringVar()
        self.interval_var = tk.StringVar()
        self.auto_check_var = tk.BooleanVar()
        self.remember_var = tk.BooleanVar()
        self.status_var = tk.StringVar(value="等待配置")
        self.last_check_var = tk.StringVar(value="")
        self.panel_status_vars = [
            tk.StringVar(value="未启动") for _ in self.panels
        ]
        self.panel_last_vars = [
            tk.StringVar(value="") for _ in self.panels
        ]

    def _apply_config(self):
        cfg = self.config
        self.host_var.set(
            str(cfg.get("host") or self.profile["defaults"]["host"])
        )
        self.port_var.set(str(cfg.get("port", 22)))
        self.username_var.set(str(cfg.get("username", "")))
        self.password_var.set(
            str(cfg.get("password", "")) if cfg.get("remember_password") else ""
        )
        self.key_file_var.set(str(cfg.get("key_file", "")))
        self.interval_var.set(str(cfg.get("check_interval", 5)))
        self.auto_check_var.set(bool(cfg.get("auto_check", True)))
        self.remember_var.set(bool(cfg.get("remember_password", False)))

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

    def _build_ui(self):
        main = tk.Frame(self.root, bg=COLORS["bg"], padx=18, pady=16)
        main.pack(fill="both", expand=True)

        header = tk.Frame(main, bg=COLORS["bg"])
        header.pack(fill="x")
        tk.Label(
            header, text=self.profile["title"], bg=COLORS["bg"], fg=COLORS["fg"],
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

        self._build_connection_card(body)
        self._build_panels(body)
        self._build_log_card(main)

    def _build_connection_card(self, main):
        card = tk.Frame(
            main, bg=COLORS["panel"], highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        card.pack(fill="x")
        inner = tk.Frame(card, bg=COLORS["panel"], padx=14, pady=10)
        inner.pack(fill="x")

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
        self.port_entry = self._grid_entry(
            grid, self.port_var, 0, 3, width=10, sticky="w"
        )

        self._grid_label(grid, "用户名", 1, 0)
        self.username_entry = self._grid_entry(
            grid, self.username_var, 1, 1, padx=(4, 12)
        )
        self._grid_label(grid, "密码", 1, 2)
        password_frame = tk.Frame(grid, bg=COLORS["panel"])
        password_frame.grid(row=1, column=3, sticky="ew", padx=(4, 0), pady=4)
        password_frame.columnconfigure(0, weight=1)
        self.password_entry = tk.Entry(
            password_frame, textvariable=self.password_var, show="•",
            bg=COLORS["panel2"], fg=COLORS["fg"], insertbackground=COLORS["fg"],
            relief="flat", bd=0, highlightthickness=1,
            highlightbackground=COLORS["border"], highlightcolor=COLORS["accent"],
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
        key_frame.grid(
            row=2, column=1, columnspan=3, sticky="ew", padx=(4, 0), pady=4
        )
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
            activeforeground=COLORS["fg"], relief="flat", padx=8, pady=2,
        ).grid(row=0, column=1, padx=(6, 0))

        options = tk.Frame(inner, bg=COLORS["panel"])
        options.pack(fill="x", pady=(8, 0))
        tk.Checkbutton(
            options, text="自动检测", variable=self.auto_check_var,
            bg=COLORS["panel"], fg=COLORS["fg"], selectcolor=COLORS["panel2"],
            activebackground=COLORS["panel"], activeforeground=COLORS["fg"],
            highlightthickness=0, bd=0,
        ).pack(side="left")
        tk.Label(
            options, text="间隔", bg=COLORS["panel"], fg=COLORS["muted"],
            font=(self.font_family, 9),
        ).pack(side="left", padx=(12, 2))
        tk.Spinbox(
            options, from_=2, to=120, textvariable=self.interval_var, width=5,
            bg=COLORS["panel2"], fg=COLORS["fg"], insertbackground=COLORS["fg"],
            buttonbackground=COLORS["panel2"], relief="flat",
            highlightthickness=1, highlightbackground=COLORS["border"],
        ).pack(side="left")
        tk.Label(
            options, text="秒", bg=COLORS["panel"], fg=COLORS["muted"],
            font=(self.font_family, 9),
        ).pack(side="left", padx=(4, 0))
        tk.Checkbutton(
            options, text="记住密码", variable=self.remember_var,
            bg=COLORS["panel"], fg=COLORS["fg"], selectcolor=COLORS["panel2"],
            activebackground=COLORS["panel"], activeforeground=COLORS["fg"],
            highlightthickness=0, bd=0,
        ).pack(side="right", padx=(18, 0))
        tk.Button(
            options, text="保存设置", command=self.save_settings,
            bg=COLORS["panel2"], fg=COLORS["fg"], activebackground=COLORS["border"],
            activeforeground=COLORS["fg"], relief="flat", padx=12, pady=3,
        ).pack(side="right")

    def _build_panels(self, body):
        row = tk.Frame(body, bg=COLORS["bg"])
        row.pack(fill="both", expand=True, pady=(10, 0))
        row.columnconfigure(0, weight=1, uniform="col")
        row.columnconfigure(1, weight=1, uniform="col")
        row.rowconfigure(0, weight=1)
        for index in range(len(self.panels)):
            holder = tk.Frame(row, bg=COLORS["bg"])
            holder.grid(
                row=0, column=index, sticky="nsew",
                padx=(0, 6) if index == 0 else (6, 0),
            )
            self._build_panel_card(holder, index)

    def _build_panel_card(self, parent, index):
        panel = self.panels[index]
        card = tk.Frame(
            parent, bg=COLORS["panel"], highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        card.pack(fill="both", expand=True)
        inner = tk.Frame(card, bg=COLORS["panel"], padx=14, pady=10)
        inner.pack(fill="both", expand=True)

        title_row = tk.Frame(inner, bg=COLORS["panel"])
        title_row.pack(fill="x")
        self._section_title(title_row, panel["name"])
        tk.Label(
            title_row, text=os.path.join(self.workdir, panel["script"]),
            bg=COLORS["panel"], fg=COLORS["muted"], font=(self.font_family, 9),
        ).pack(side="right")

        buttons = tk.Frame(inner, bg=COLORS["panel"])
        buttons.pack(fill="x", pady=(10, 0))
        self.panel_start_buttons[index] = tk.Button(
            buttons, text="启动", command=lambda i=index: self.panel_start(i),
            width=8, bg=COLORS["accent_dim"], fg="#06120b",
            activebackground=COLORS["accent"], activeforeground="#06120b",
            disabledforeground="#5c6f62", relief="flat", padx=10, pady=4,
            font=(self.font_family, 11, "bold"),
        )
        self.panel_start_buttons[index].pack(side="left")
        self.panel_stop_buttons[index] = tk.Button(
            buttons, text="停止", command=lambda i=index: self.panel_stop(i),
            width=8, bg=COLORS["danger_dim"], fg="#ffffff",
            activebackground=COLORS["danger"], activeforeground="#ffffff",
            disabledforeground="#73524e", relief="flat", padx=10, pady=4,
            font=(self.font_family, 11, "bold"),
        )
        self.panel_stop_buttons[index].pack(side="left", padx=(10, 0))
        self.panel_detect_buttons[index] = tk.Button(
            buttons, text="检测状态", command=lambda i=index: self.panel_detect(i),
            bg=COLORS["panel2"], fg=COLORS["fg"], activebackground=COLORS["border"],
            activeforeground=COLORS["fg"], relief="flat", padx=10, pady=3,
        )
        self.panel_detect_buttons[index].pack(side="right")

        status_row = tk.Frame(inner, bg=COLORS["panel"])
        status_row.pack(fill="x", pady=(10, 0))
        tk.Label(
            status_row, text="运行状态:", bg=COLORS["panel"],
            fg=COLORS["muted"], font=(self.font_family, 10),
        ).pack(side="left")
        self.panel_status_labels[index] = tk.Label(
            status_row, textvariable=self.panel_status_vars[index],
            bg=COLORS["panel"], fg=COLORS["muted"],
            font=(self.font_family, 10, "bold"),
        )
        self.panel_status_labels[index].pack(side="left", padx=(6, 0))
        tk.Label(
            status_row, textvariable=self.panel_last_vars[index],
            bg=COLORS["panel"], fg=COLORS["muted"], font=(self.font_family, 9),
        ).pack(side="right")

        tk.Frame(inner, bg=COLORS["border2"], height=1).pack(
            fill="x", pady=(10, 0)
        )
        tk.Label(
            inner, text="启动命令", bg=COLORS["panel"], fg=COLORS["muted"],
            font=(self.font_family, 9),
        ).pack(anchor="w", pady=(8, 2))
        tk.Label(
            inner, text=panel["start_command"], bg=COLORS["panel"],
            fg=COLORS["muted"], font=(self.number_family, 9), anchor="w",
            justify="left", wraplength=500,
        ).pack(fill="x")

    def _build_log_card(self, main):
        card = tk.Frame(
            main, bg=COLORS["panel"], highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        card.pack(fill="both", expand=True, pady=(10, 0))
        inner = tk.Frame(card, bg=COLORS["panel"], padx=14, pady=10)
        inner.pack(fill="both", expand=True)

        title_row = tk.Frame(inner, bg=COLORS["panel"])
        title_row.pack(fill="x")
        self._section_title(title_row, "执行日志")
        self.log_text = scrolledtext.ScrolledText(
            inner, bg=COLORS["bg"], fg=COLORS["fg"], insertbackground=COLORS["fg"],
            relief="flat", wrap="word", height=7,
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

    def _toggle_password(self):
        self.show_password = not self.show_password
        self.password_entry.configure(show="" if self.show_password else "•")
        self.password_toggle.configure(text="隐藏" if self.show_password else "显示")

    def _browse_key(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="选择 SSH 私钥文件",
            filetypes=[("私钥文件", "*"), ("所有文件", "*")],
        )
        if path:
            self.key_file_var.set(path)

    def detect_now(self):
        if self.checking or self.command_busy:
            return
        try:
            settings = self._settings()
        except ValueError as exc:
            self._post_status("bad", "配置无效", str(exc))
            return
        threading.Thread(
            target=self._check_connection_worker, args=(settings,), daemon=True
        ).start()

    def _check_connection_worker(self, settings):
        if self.checking:
            return
        self.checking = True
        self.checking_since = time.monotonic()
        self._post_status("check", "正在检测连接...", "")
        started = time.monotonic()
        try:
            self.manager.connect(settings, force=True)
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
            self.checking_since = None

    def _auto_check_toggled(self):
        if self.auto_check_var.get() and not self.check_stop.is_set():
            self.root.after(200, self._auto_tick)

    def _auto_tick(self):
        if self.check_stop.is_set() or not self.auto_check_var.get():
            return
        if not self.command_busy:
            try:
                settings = self._settings()
            except ValueError:
                pass
            else:
                threading.Thread(
                    target=self._auto_check_worker, args=(settings,), daemon=True
                ).start()
        try:
            interval = max(2, int(self.interval_var.get()))
        except ValueError:
            interval = 5
        self.root.after(interval * 1000, self._auto_tick)

    def _auto_check_worker(self, settings):
        if self.checking:
            return
        self.checking = True
        self.checking_since = time.monotonic()
        try:
            started = time.monotonic()
            try:
                self.manager.connect(settings, force=False)
                elapsed = time.monotonic() - started
                self._post_status("ok", "已连接", f"耗时 {elapsed:.2f}s")
            except paramiko.AuthenticationException:
                self._post_status("bad", "无法连接", "认证失败，请检查用户名、密码或私钥")
            except (socket.timeout, socket.gaierror, paramiko.SSHException, OSError) as exc:
                message = str(exc).strip() or type(exc).__name__
                self._post_status("bad", "无法连接", message)
            except ValueError as exc:
                self._post_status("bad", "配置无效", str(exc))
            for index in range(len(self.panels)):
                self._detect(index, settings)
        finally:
            self.checking = False
            self.checking_since = None

    def panel_start(self, index):
        if self.command_busy:
            self._post_log("正在执行其他操作，请稍候", "muted")
            return
        try:
            settings = self._settings()
        except ValueError as exc:
            self._post_status("bad", "配置无效", str(exc))
            return
        self._set_command_busy(True)
        threading.Thread(
            target=self._panel_op_worker, args=("start", index, settings), daemon=True
        ).start()

    def panel_stop(self, index):
        if self.command_busy:
            self._post_log("正在执行其他操作，请稍候", "muted")
            return
        try:
            settings = self._settings()
        except ValueError as exc:
            self._post_status("bad", "配置无效", str(exc))
            return
        self._set_command_busy(True)
        threading.Thread(
            target=self._panel_op_worker, args=("stop", index, settings), daemon=True
        ).start()

    def panel_detect(self, index):
        if self.command_busy:
            self._post_log("正在执行其他操作，请稍候", "muted")
            return
        if self.checking:
            stuck = (
                self.checking_since is not None
                and time.monotonic() - self.checking_since > 30
            )
            if stuck:
                self.checking = False
                self.checking_since = None
                self._post_log("上一次检测超时，已重置，重新检测", "muted")
            else:
                self._post_log("正在检测中，请稍候", "muted")
                return
        try:
            settings = self._settings()
        except ValueError as exc:
            self._post_status("bad", "配置无效", str(exc))
            return
        threading.Thread(
            target=self._detect_worker, args=(index, settings), daemon=True
        ).start()

    def _detect_worker(self, index, settings):
        if self.checking:
            return
        self.checking = True
        self.checking_since = time.monotonic()
        try:
            self._detect(index, settings)
        finally:
            self.checking = False
            self.checking_since = None

    def _detect(self, index, settings):
        panel = self.panels[index]
        name = panel["name"]
        try:
            code, out, err = self.manager.run(
                settings, panel["detect_command"], timeout=20
            )
        except paramiko.AuthenticationException:
            self._post_panel_status(
                index, False, "idle", "未启动", "检测失败：认证失败"
            )
            self._post_log(f"{name} 检测失败：认证失败", "bad")
            return None
        except (socket.timeout, socket.gaierror, paramiko.SSHException, OSError) as exc:
            message = str(exc).strip() or type(exc).__name__
            self._post_panel_status(index, False, "idle", "未启动", f"检测失败：{message}")
            self._post_log(f"{name} 检测失败：{message}", "bad")
            return None
        except ValueError as exc:
            self._post_panel_status(index, False, "idle", "未启动", f"配置无效：{exc}")
            return None
        if code != 0:
            message = (err.strip() or out.strip() or f"退出码 {code}")[:120]
            self._post_panel_status(index, False, "idle", "未启动", f"检测失败：{message}")
            self._post_log(f"{name} 检测失败：{message}", "bad")
            return None
        nodes = [line.strip() for line in out.splitlines() if line.strip()]
        keyword = (panel.get("detect_keyword") or "").strip()
        if keyword:
            running = keyword.lower() in out.lower()
            reason = f"节点列表包含 “{keyword}”"
        else:
            running = bool(nodes)
            reason = f"发现 {len(nodes)} 个节点" if nodes else "没有发现节点"
        self._post_panel_status(
            index, running, "ok" if running else "idle",
            "已启动" if running else "未启动", reason,
        )
        return running

    def _panel_op_worker(self, kind, index, settings):
        label = "启动" if kind == "start" else "停止"
        panel = self.panels[index]
        name = panel["name"]
        command = panel["start_command"] if kind == "start" else panel["stop_command"]
        self._post_log(f"正在通过 SSH {label} {name}: {command}", "muted")
        try:
            code, out, err = self.manager.run(settings, command)
        except paramiko.AuthenticationException:
            self._post_status("bad", "无法连接", "认证失败，请检查用户名、密码或私钥")
            self._post_log(f"{name} {label}失败：认证失败", "bad")
        except (socket.timeout, socket.gaierror, paramiko.SSHException, OSError) as exc:
            message = str(exc).strip() or type(exc).__name__
            self._post_status("bad", "无法连接", message)
            self._post_log(f"{name} {label}失败：{message}", "bad")
        except ValueError as exc:
            self._post_status("bad", "配置无效", str(exc))
            self._post_log(f"{name} {label}失败：{exc}", "bad")
        else:
            tag = "ok" if code == 0 else "bad"
            self._post_log(f"{name} {label}命令执行完成，退出码 {code}", tag)
            if out.strip():
                self._post_log(out.rstrip(), "out")
            if err.strip():
                self._post_log(err.rstrip(), "bad")
            if code == 0:
                # 停止后 ROS 图刷新可能滞后约 10 秒（ros2 node list 的
                # 守护进程/DDS 发现），等久一点再检测，避免显示残留节点
                wait = 2.0 if kind == "start" else 12.0
                self._post_log(
                    f"等待 {int(wait)} 秒后自动检测 {name} 状态...", "muted"
                )
                time.sleep(wait)
                started = self._detect(index, settings)
                if kind == "start" and started is False:
                    log_path = "/tmp/" + panel["script"] + ".log"
                    self._post_log(
                        f"{name} 未检测到节点，读取启动日志 {log_path} 排查：", "bad"
                    )
                    try:
                        code2, out2, err2 = self.manager.run(
                            settings, "tail -n 25 " + shlex.quote(log_path)
                        )
                    except Exception:
                        pass
                    else:
                        if out2.strip():
                            self._post_log(out2.rstrip(), "out")
                        if err2.strip():
                            self._post_log(err2.rstrip(), "bad")
        finally:
            self.message_queue.put(("busy", False))

    def _set_command_busy(self, busy):
        self.command_busy = busy
        self._update_panel_buttons()
        self.detect_button.configure(state="disabled" if busy else "normal")

    def _update_panel_buttons(self):
        for index in range(len(self.panels)):
            running = self.panel_running[index]
            busy = self.command_busy
            self.panel_start_buttons[index].configure(
                state="normal" if not busy and running is not True else "disabled"
            )
            self.panel_stop_buttons[index].configure(
                state="normal" if not busy and running is True else "disabled"
            )
            self.panel_detect_buttons[index].configure(
                state="normal" if not busy else "disabled"
            )

    def _post_status(self, kind, text, detail=""):
        self.message_queue.put(("status", kind, text, detail))

    def _post_panel_status(self, index, running, kind, text, detail=""):
        self.message_queue.put(
            ("panel_status", index, running, kind, text, detail)
        )

    def _post_log(self, text, tag):
        self.message_queue.put(("log", text, tag))

    def _set_header_status(self, kind, text, detail=""):
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
        if kind == "ok" and not self.initial_checked:
            self.initial_checked = True
            self.root.after(400, self._check_all_once)

    def _check_all_once(self):
        if self.check_stop.is_set():
            return
        try:
            settings = self._settings()
        except ValueError:
            return
        if not self.command_busy and not self.checking:
            threading.Thread(
                target=self._auto_check_worker, args=(settings,), daemon=True
            ).start()

    def _poll_messages(self):
        try:
            while True:
                kind, *payload = self.message_queue.get_nowait()
                if kind == "status":
                    self._set_header_status(*payload)
                elif kind == "panel_status":
                    index, running, state_kind, text, detail = payload
                    if state_kind != "check":
                        self.panel_running[index] = running
                    self.panel_status_vars[index].set(text)
                    self.panel_status_labels[index].configure(
                        fg=PANEL_STATUS_COLORS.get(
                            state_kind, COLORS["muted"]
                        )
                    )
                    suffix = f" · {detail}" if detail else ""
                    self.panel_last_vars[index].set(
                        f"最后检测 {time.strftime('%H:%M:%S')}{suffix}"
                    )
                    self._update_panel_buttons()
                elif kind == "log":
                    self._append_log(*payload)
                elif kind == "busy":
                    self._set_command_busy(payload[0])
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
                "auto_check": bool(self.auto_check_var.get()),
                "check_interval": interval,
                "remember_password": bool(self.remember_var.get()),
                "ui_font": self.font_family,
                "number_font": self.number_family,
            }
        )
        self.config.pop("panels", None)
        if self.remember_var.get():
            self.config["password"] = self.password_var.get()
        else:
            self.config.pop("password", None)
        save_config(self.profile["config_path"], self.config)
        if not silent:
            self._append_log("设置已保存", "ok")

    def close(self):
        self.check_stop.set()
        self.save_settings(silent=True)
        self.manager.close()
        self.root.destroy()


if __name__ == "__main__":
    PerceptionApp().root.mainloop()
