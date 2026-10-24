"""Run the FastAPI website and Telegram bot in one Render Docker service.

The website process owns the container lifetime. If the bot exits, it is restarted.
If the website exits, the container exits so Render can restart the service.

Crash helper: before starting, every .py file is checked for broken/cut-off code, and if the
website or bot quits, its last output lines are repeated in a clearly marked banner so the
real error is easy to find in the Render logs.
"""
import collections
import glob
import os
import py_compile
import signal
import subprocess
import sys
import threading
import time

children = {}
stopping = False
tails = {"web": collections.deque(maxlen=60), "bot": collections.deque(maxlen=40)}


def banner(title, lines):
    print("\n" + "=" * 64, flush=True)
    print(title, flush=True)
    for line in lines:
        print("   " + line, flush=True)
    print("=" * 64 + "\n", flush=True)


def preflight():
    problems = []
    for path in sorted(glob.glob("*.py")):
        try:
            py_compile.compile(path, doraise=True)
        except py_compile.PyCompileError as e:
            problems.append(f"{path}: {e.msg.strip()}")
        except Exception as e:  # noqa: BLE001
            problems.append(f"{path}: {e}")
    if problems:
        banner("[preflight] BROKEN PYTHON FILE(S) - a file was probably copied incompletely:", problems)
    else:
        print("[preflight] all Python files look OK", flush=True)


def stop_all(*_args):
    global stopping
    stopping = True
    for process in list(children.values()):
        if process.poll() is None:
            process.terminate()


signal.signal(signal.SIGTERM, stop_all)
signal.signal(signal.SIGINT, stop_all)


def pump(name, process):
    for raw in iter(process.stdout.readline, b""):
        text = raw.decode(errors="replace").rstrip()
        tails[name].append(text)
        print(f"[{name}] {text}", flush=True)


def start(name, command):
    print(f"[supervisor] starting {name}: {' '.join(command)}", flush=True)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    threading.Thread(target=pump, args=(name, process), daemon=True).start()
    return process


preflight()
children["bot"] = start("bot", [sys.executable, "bot.py"])
children["web"] = start("web", [sys.executable, "web.py"])

try:
    while not stopping:
        web = children["web"]
        if web.poll() is not None:
            time.sleep(0.5)  # let the last output lines arrive
            banner(f"[supervisor] WEBSITE CRASHED (exit code {web.returncode}). Last lines it printed:", list(tails["web"])[-25:] or ["(it printed nothing)"])
            print(f"[supervisor] web exited with code {web.returncode}; stopping service", flush=True)
            break
        bot = children["bot"]
        if bot.poll() is not None:
            time.sleep(0.5)
            banner(f"[supervisor] BOT STOPPED (exit code {bot.returncode}). Last lines it printed:", list(tails["bot"])[-15:] or ["(it printed nothing)"])
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
