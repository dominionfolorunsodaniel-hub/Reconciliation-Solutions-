"""Run this before every deploy:  python predeploy_check.py
Catches the mistakes that would crash or break the site: Python syntax errors, broken JavaScript in the page,
duplicate HTML ids, and the logic tests. Exit code 0 = safe to deploy."""
import collections, glob, json, os, py_compile, re, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ok = True


def step(name, passed, detail=""):
    global ok
    ok = ok and passed
    print(("PASS  " if passed else "FAIL  ") + name + (("  - " + detail) if detail else ""))


# 1. every Python file compiles
bad = []
for f in sorted(glob.glob(os.path.join(HERE, "*.py"))):
    try:
        py_compile.compile(f, doraise=True)
    except py_compile.PyCompileError as e:
        bad.append(os.path.basename(f) + ": " + str(e).splitlines()[-1])
step("Python files compile", not bad, "; ".join(bad))

# 2. page JavaScript parses (needs node) and ids are unique
html_path = os.path.join(HERE, "static", "index.html")
html = open(html_path, encoding="utf-8").read()
ids = collections.Counter(re.findall(r'\sid="([^"]+)"', html))
dups = sorted(k for k, v in ids.items() if v > 1)
step("No duplicate HTML ids", not dups, ", ".join(dups[:10]))
if shutil.which("node"):
    errs = []
    for i, m in enumerate(re.finditer(r"<script(?![^>]*\bsrc=)(?![^>]*type=\"application/ld\+json\")[^>]*>(.*?)</script>", html, re.S)):
        tmp = os.path.join(HERE, f".chk{i}.js")
        open(tmp, "w", encoding="utf-8").write(m.group(1))
        r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
        os.remove(tmp)
        if r.returncode:
            errs.append(f"script #{i}: " + r.stderr.strip().splitlines()[-1][:160])
    step("Page JavaScript parses", not errs, "; ".join(errs))
else:
    print("SKIP  JavaScript check (node not installed)")

# 3. JSON files are valid
for jf in glob.glob(os.path.join(HERE, "static", "*.json")):
    try:
        json.load(open(jf, encoding="utf-8")); step("Valid JSON: " + os.path.basename(jf), True)
    except Exception as e:  # noqa: BLE001
        step("Valid JSON: " + os.path.basename(jf), False, str(e))

# 4. logic tests (use a throw-away data folder, never real data)
r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", os.path.join(HERE, "tests")], capture_output=True, text=True, cwd=HERE)
step("Logic tests", r.returncode == 0, "" if r.returncode == 0 else r.stderr[-400:])

print("\nSAFE TO DEPLOY" if ok else "\nDO NOT DEPLOY - fix the FAIL lines above")
sys.exit(0 if ok else 1)
