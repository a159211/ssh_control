#!/usr/bin/env python3
"""SSH 远程控制面板（感知）。

独立的感知代码启动界面：进入 /home/ac/Documents/AC/work_ws，
通过两个面板分别运行 bash bringup1.sh / bash bringup2.sh（ros2 launch），
并用 `ros2 node list` 查询节点来判断各自是否已启动。

配置保存在 ~/.ssh_control_perception.json，与运控界面互不影响。
"""

from perception_ui import PerceptionApp

if __name__ == "__main__":
    PerceptionApp().root.mainloop()
