# SSH 远程控制

基于 Python 和 Tkinter 的运控面板，通过 SSH 连接机器人，支持服务启停、
运行状态与开机自启动状态查看、电量显示、模型上传和远程参数编辑。

## 环境配置

需要带桌面环境的 Linux、Python 3（建议 3.12+），以及可联网的依赖安装环境。

### 1. 安装系统依赖

Ubuntu / Debian：

```bash
sudo apt update
sudo apt install python3 python3-tk python3-venv fonts-noto-cjk fontconfig
fc-cache -fv
```

`fonts-noto-cjk` 提供中文字体，避免缺少字体时出现中文锯齿或显示异常；
`fc-cache -fv` 用于刷新字体缓存。如果程序已打开，安装完成后请关闭并重新启动。

其他发行版安装对应的 Python、Tk、venv、Noto CJK 字体和 fontconfig 组件，
安装后同样执行 `fc-cache -fv`。

### 2. 安装项目依赖

进入项目目录，执行初始化脚本：

```bash
cd /你的项目路径/ssh_control
bash setup.sh
```

脚本会创建 `.venv` 虚拟环境，并安装 `requirements.txt` 中的依赖。
换电脑后请重新执行此步骤，不要直接使用原电脑的 `.venv` 或 `vendor/`。

### 3. 启动程序

```bash
bash run.sh
```

后续启动只需执行这条命令，无需重复安装依赖。
如果安装中文字体并重启程序后仍显示异常，请检查系统缩放和 Tk 字体渲染设置。

## 使用说明

在界面填写主机地址（默认 `192.168.44.100`）、SSH 用户名、密码或私钥，
点击“立即检测”确认连接。远端需要已安装 SSH 服务及 `test_all.sh` 对应的
systemd 服务；执行启停时，登录账号需要相应的 sudo 权限。

- **启停服务**：分别执行 `sudo systemctl start test_all.sh` 和
  `sudo systemctl stop test_all.sh`。
- **状态检测**：自动刷新服务运行状态和开机自启动配置，也能识别开机已启动的
  服务；自启动状态仅显示，不提供开关。
- **电量显示**：读取 `/opt/ac_ws/logs/bms_driver.log` 中的 SOC。
- **文件上传**：选择或拖入文件，上传至 `/opt/ac_ws/models/`，显示进度条；
  上传期间不能选择或拖入其他文件。
- **远程配置**：读取和保存 `/opt/ac_ws/config/robot.yaml` 中的模型名称，
  点击“参数”可编辑对应配置节；名称输入框支持粘贴替换选中文字。

连接设置保存在 `~/.ssh_control.json`。勾选“记住密码”后，密码会以明文保存在
该文件中，文件权限为 `600`。操作结果和错误信息可在界面下方日志区查看。
