"""双击或执行此脚本启动 Boss 求职助手（Windows / Edge Web 版）桌面控制台。"""

import os
import sys
from pathlib import Path

from boss_assistant.gui import main


if __name__ == "__main__":
    # 打包为 exe 双击运行时，把工作目录固定到 exe 所在目录（项目根），
    # 保证 data/、config/、resume_inbox/ 等相对路径与命令行启动行为一致。
    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).resolve().parent)
    main()
