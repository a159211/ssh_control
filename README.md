# SSH 远程控制

独立的 SSH 远程控制面板，分为两个完全独立的界面：

- 运控界面（`ssh_control.py` / `./run.sh`）：默认检测 `192.168.44.100`，
  点击“启动 / 停止”执行运控代码 `cd /opt/ac_ws && sudo ./robot_service.sh`
- 感知界面（`perception_control.py` / `./run_perception.sh`）：独立的窗口与
  配置（`~/.ssh_control_perception.json`），进入 `/home/ac/Documents/AC/work_ws`
  通过两个独立面板分别执行 `bash bringup1.sh` / `bash bringup2.sh`（运行
  ros2 launch），各自用 `ros2 node list` 查询节点判断是否已启动

两个界面互不影响，可以同时打开；SSH 连接、自动检测、状态检测、启动/停止
命令等功能完全一致。感知界面不包含“参数调节”和“文件上传”两块（这两块仅
运控界面提供）。

## 运行

```bash
cd ssh_control
./run.sh
```

感知界面：

```bash
cd ssh_control
./run_perception.sh
```

也可以手动运行：

```bash
cd ssh_control
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python ssh_control.py
```

感知界面手动运行：

```bash
cd ssh_control
.venv/bin/python perception_control.py
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
- 自动检测连接时同步刷新运行状态：读取
  `/opt/ac_ws/logs/start_all.pid` 中的 PID 判断运控程序是否还在运行，
  即使程序在界面外被关闭也会自动更新为“未启动”
- 运控界面显示电量：按检测间隔读取 `/opt/ac_ws/logs/bms_driver.log` 中
  最新的 SOC 百分比（兼容 `current battery soc` / `battery soc` / `soc` /
  `电量` 等常见写法，并自动去掉日志里的 ANSI 颜色转义）显示进度条；
  读取失败时电量旁会直接显示具体原因（文件不存在、权限不足、日志里
  没有电量行等），并把实际读到的日志内容输出到下方日志区
- 界面上显示 `192.168.44.100` 是否已连接，也可以点“立即检测”手动检查
- “启动”执行 `cd /opt/ac_ws && sudo ./robot_service.sh`
- “停止”执行 `cd /opt/ac_ws && sudo ./robot_service.sh stop`
- 感知界面：`FastDDS`（执行 `bash bringup1.sh`）和 `相机节点`（执行
  `bash bringup2.sh`）两个面板，各自带“启动 / 停止 / 检测状态”，启动命令为
  进入 `/home/ac/Documents/AC/work_ws` 后台运行对应脚本，日志写到
  `/tmp/bringup1.sh.log` / `/tmp/bringup2.sh.log`
- 感知状态检测：每个面板通过 SSH 执行 `ros2 node list`，有关键字时按关键字
  匹配，没配置关键字时只要有节点就视为已启动。FastDDS 面板默认按节点
  `depth_image_bridge_node` 检测，相机节点面板默认按 `camera_container` 检测
  （bringup2 运行后会多出
  `/camera/camera`、`/camera/camera_container`、`/launch_ros_7067` 三个节点）；
  如需调整关键字或脚本名，改 `ssh_control.py` 里感知配置的 `panels` 即可
- 各面板的启动/停止/检测命令与检测关键字由代码按工作目录和脚本名自动生成，
  不再读取配置文件里保存的旧值，避免旧配置覆盖新逻辑；启动用
  `(setsid bash -c 'echo $$ > /tmp/bringupN.sh.pid; exec bash bringupN.sh'
  </dev/null >/tmp/bringupN.sh.log 2>&1 &)` 让脚本成为独立进程组并记录 PID
  （后台任务放在立刻退出的内层子 shell 里，避免占用 SSH 通道导致按钮阻塞），
  停止时按 PID 杀掉整个进程组（ros2 节点是脚本的子进程，只杀脚本自身会留下
  孤儿节点），再以 `pkill -f 'bash [b]ringupN.sh'` 和按检测关键字的
  `pkill -f '[d]epth_image_bridge_node'` / `pkill -f '[c]amera_container'`
  兜底；停止命令静默执行，不再打印各种 rc/进程列表，停止是否成功由界面
  后台检测（停止成功/停止超时）确认；点击启动/停止会立即显示
  “启动中…/停止中…”，命令执行完按钮即恢复可用，后台每 2 秒检测一次
  （停止检测自动覆盖 ROS 图约 10 秒的刷新滞后），确认成功或超时
  （启动 30 秒 / 停止 40 秒，相机节点停止单独配置为 90 秒）前一直保持
  “启动中…/停止中…”，不再中途先显示“未启动”
- 命令里包含 `sudo` 时，程序会自动把保存的密码通过 stdin 输入
- 拖入或选择的文件会上传到 `/opt/ac_ws/models/`
- 可以读取 `/opt/ac_ws/config/robot.yaml` 中 `robot`、`run_gait`、
  `camera_perceptied` 下的 4 个模型 `name`，修改后保存，注释会保留；
  程序打开时会自动读取一次远端配置，无需手动点“读取配置”
- 每个配置 name 后面有“参数”按钮，弹窗显示该配置节的原始 YAML 文本，
  可直接整段复制替换后保存
- 勾选“记住密码”后，密码会保存在 `~/.ssh_control.json`，该文件权限为 600
- 连接日志和命令输出显示在下方日志区

## 打包成可执行文件（可选）

```bash
cd ssh_control
PYTHONPATH="$PWD/vendor" pyinstaller --onefile --noconsole ssh_control.py
```
