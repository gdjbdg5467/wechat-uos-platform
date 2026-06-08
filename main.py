#!/usr/bin/env python3
"""WxPowerBot — 独立 WeChat UOS 多功能机器人入口。"""

import sys
import os

# 确保在项目目录
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from bot import WxPowerBot


def main():
    data_dir = os.environ.get("WXPOWERBOT_DATA_DIR", "/data")
    bot = WxPowerBot(data_dir=data_dir)
    bot.start()


if __name__ == "__main__":
    main()
