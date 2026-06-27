#!/usr/bin/env python3
"""WxWeChatBot - 独立版微信机器人入口"""
import sys, os, signal, json, yaml
from pathlib import Path

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def load_config():
    config_path = Path(__file__).parent / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f)
    # Try env vars as fallback
    return {"data_dir": os.getenv("WECHAT_DATA_DIR", "./state")}

def main():
    config = load_config()
    data_dir = Path(config.get("data_dir", "./state"))
    data_dir.mkdir(parents=True, exist_ok=True)
    
    from bot import BotCore
    bot = BotCore(config=config, data_dir=data_dir)
    
    def signal_handler(sig, frame):
        print("\n正在关闭机器人...")
        bot.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        bot.start()
    except KeyboardInterrupt:
        bot.stop()

if __name__ == "__main__":
    main()
