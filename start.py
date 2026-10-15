"""Run the FastAPI website and Telegram bot in one Render Docker service.

The website process owns the container lifetime. If the bot exits, it is restarted.
If the website exits, the container exits so Render can restart the service.
"""
import os
import signal
import subprocess
import sys
import time

children = {}
stopping = False

def stop_all(*_args):
    global stopping
    stopping = True
    for process in list(children.values()):
        if process.poll() is None:
            process.terminate()

signal.signal(signal.SIGTERM, stop_all)
signal.signal(signal.SIGINT, stop_all)

def start(name, command):
    print(f"[supervisor] starting {name}: {' '.join(command)}", flush=True)
    return subprocess.Popen(command, env=os.environ.copy())

children["bot"] = start("bot", [sys.executable, "bot.py"])
children["web"] = start("web", [sys.executable, "web.py"])

try:
    while not stopping:
        web = children["web"]
        if web.poll() is not None:
            print(f"[supervisor] web exited with code {web.returncode}; stopping service", flush=True)
            break
        bot = children["bot"]
        if bot.poll() is not None:
            print(f"[supervisor] bot exited with code {bot.returncode}; restarting in 5 seconds", flush=True)
            time.sleep(5)
            if not stopping:
                children["bot"] = start("bot", [sys.executable, "bot.py"])
        time.sleep(1)
finally:
    stop_all()
    for process in list(children.values()):
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

sys.exit(children["web"].returncode if children["web"].returncode is not None else 1)
