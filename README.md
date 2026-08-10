# SSH 远程控制

独立的 SSH 远程控制面板：默认检测 `192.168.44.100` 是否能连上，自动重连，
点击“启动 / 停止”即可在远程主机上执行固定命令。

## 运行

```bash
cd ssh_control
./run.sh
```

也可以手动运行：

```bash
cd ssh_control
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python ssh_control.py
```

程序会优先使用项目内的 `.venv` 环境；如果 `.venv` 不存在，才回退到本机
`vendor/` 目录加 `/usr/bin/python3` 的启动方式。

## 在 Linux 其他电脑上配置环境

`vendor/` 目录只适配当前这台机器，换电脑后不要依赖它，用下面的方式重新
配置：

```bash
cd ssh_control
./setup.sh
./run.sh
```

Debian / Ubuntu 如果提示缺少 tkinter 或 venv：

```bash
sudo apt install python3-tk python3-venv
```

其他发行版需要确保系统里有 Python 3.12+、Tk 和 venv 组件。

## 功能

- 主机、端口、用户名、密码或私钥填好后会自动检测连接状态
- 界面上显示 `192.168.44.100` 是否已连接，也可以点“立即检测”手动检查
- “启动”执行 `cd /opt/ac_ws && sudo ./robot_service.sh`
- “停止”执行 `cd /opt/ac_ws && sudo ./robot_service.sh stop`
- 命令里包含 `sudo` 时，程序会自动把保存的密码通过 stdin 输入
- 检查 `/opt/ac_ws/logs/start_all.pid` 自动检测服务是否已启动
- 拖入或选择的文件会上传到 `/opt/ac_ws/models/`
- 可以读取 `/opt/ac_ws/config/robot.yaml` 中 `robot`、`run_gait`、
  `camera_perceptied` 下的 4 个模型 `name`，修改后保存，注释会保留
- 每个配置 name 后面有“参数”按钮，弹窗显示该配置节的原始 YAML 文本，
  可直接整段复制替换后保存
- 勾选“记住密码”后，密码会保存在 `~/.ssh_control.json`，该文件权限为 600
- 连接日志和命令输出显示在下方日志区

## 打包成可执行文件（可选）

```bash
cd ssh_control
PYTHONPATH="$PWD/vendor" pyinstaller --onefile --noconsole ssh_control.py
```
