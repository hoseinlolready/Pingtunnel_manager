#!/usr/bin/env python3
import os, sys, json, time, platform, tempfile, zipfile, urllib.request, shutil, subprocess, signal
from pathlib import Path

# Paths and constants
INSTALL_DIR = Path("/opt/pingtunnel")
BIN_DIR = INSTALL_DIR / "bin"
CONF_DIR = INSTALL_DIR / "conf"
LOG_DIR = Path("/var/log/pingtunnel")
RUNNER_PATH = INSTALL_DIR / "run_pingtunnel.py"
CONFIG_PATH = CONF_DIR / "config.json"
SYMLINK = Path("/usr/local/bin/pingtunnel")
SYSTEMD_UNIT = "pingtunnel.service"
UNIT_PATH = Path("/etc/systemd/system") / SYSTEMD_UNIT
PID_FILE = Path("/run/pingtunnel.pid")

URLS = {
    "x86_64": "https://github.com/esrrhs/pingtunnel/releases/download/2.8/pingtunnel_linux_amd64.zip",
    "amd64":  "https://github.com/esrrhs/pingtunnel/releases/download/2.8/pingtunnel_linux_amd64.zip",
    "aarch64":"https://github.com/esrrhs/pingtunnel/releases/download/2.8/pingtunnel_linux_arm64.zip",
    "arm64":  "https://github.com/esrrhs/pingtunnel/releases/download/2.8/pingtunnel_linux_arm64.zip",
    "i386":   "https://github.com/esrrhs/pingtunnel/releases/download/2.8/pingtunnel_linux_386.zip",
    "i686":   "https://github.com/esrrhs/pingtunnel/releases/download/2.8/pingtunnel_linux_386.zip",
}

# Helpers
def is_root():
    return os.geteuid() == 0

def die(msg="error"):
    print(msg, file=sys.stderr)
    sys.exit(1)

def detect_download_url():
    m = platform.machine().lower()
    if m in URLS:
        return URLS[m]
    if "arm" in m:
        return URLS.get("arm64")
    return URLS.get("x86_64")

def ensure_dirs():
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)

def download_file(url, dest, tries=4):
    last = None
    for i in range(1, tries+1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent":"curl/7.68.0"})
            with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
                while True:
                    chunk = r.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
            return
        except Exception as e:
            last = e
            time.sleep(1 + i)
    raise last

def safe_extract(zip_path, target_dir):
    with zipfile.ZipFile(zip_path, "r") as z:
        for member in z.namelist():
            resolved = (target_dir / member).resolve()
            if not str(resolved).startswith(str(target_dir.resolve())):
                raise Exception("zip contains unsafe path: " + member)
        z.extractall(target_dir)

def find_pingtunnel_binary():
    for p in BIN_DIR.rglob("*"):
        if p.is_file() and "pingtunnel" in p.name.lower():
            try:
                p.chmod(p.stat().st_mode | 0o111)
            except:
                pass
            return p
    return None

# Runner template
RUNNER_TEMPLATE = r'''#!/usr/bin/env python3
import os, sys, json, time, subprocess, shutil, signal, tempfile, zipfile, urllib.request
from pathlib import Path
INSTALL_DIR = Path("__INSTALL_DIR__")
BIN_DIR = INSTALL_DIR / "bin"
CONF = INSTALL_DIR / "conf" / "config.json"
LOG_DIR = Path("__LOG_DIR__")
LOG_FILE = LOG_DIR / "pingtunnel.log"
UNIT = "__UNIT__"
PID_FILE = Path("/run/pingtunnel.pid")
URLS = __URLS_JSON__

def now(): return time.strftime("%Y-%m-%d %H:%M:%S")
def log(s):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"{now()} {s}\n")
    except:
        pass
    print(f"{now()} {s}")

def load_conf():
    if not CONF.exists():
        raise SystemExit("config missing: " + str(CONF))
    return json.load(open(CONF))

def find_bin(conf):
    b = conf.get("binary")
    if b:
        p = Path(b)
        if p.exists():
            return p
    for x in BIN_DIR.rglob("*"):
        if x.is_file() and "pingtunnel" in x.name.lower():
            try:
                x.chmod(x.stat().st_mode | 0o111)
            except:
                pass
            return x
    raise SystemExit("pingtunnel binary not found in " + str(BIN_DIR))

def build_args(conf, binpath):
    args = [str(binpath)]
    t = conf.get("type", "server").lower()
    key = conf.get("key", "")
    tcp = str(conf.get("tcp", 1))
    if t == "server":
        args += ["-type", "server", "-tcp", "1", "-key", key, "-noprint", "1", "-nolog", "1"]
    else:
        lport = conf.get("l_port")
        if lport:
            args += ["-type", "client", "-l", ":" + str(lport)]
        else:
            args += ["-type", "client"]
        server = conf.get("server")
        if server:
            args += ["-s", str(server)]
        args += ["-tcp", "1", "-key", key, "-noprint", "1", "-nolog", "1"]
    return args

def apply_systemd_mem(conf):
    m = int(conf.get("memory_mb", 0) or 0)
    dropin = Path("/etc/systemd/system") / (UNIT + ".d")
    memfile = dropin / "memory.conf"
    if m > 0:
        dropin.mkdir(parents=True, exist_ok=True)
        memfile.write_text("[Service]\nMemoryLimit=%dM\n" % m)
        subprocess.run(["systemctl","daemon-reload"])
    else:
        if memfile.exists():
            try:
                memfile.unlink()
                subprocess.run(["systemctl","daemon-reload"])
            except:
                pass

def monitor_loop():
    conf = load_conf()
    binpath = find_bin(conf)
    apply_systemd_mem(conf)
    args = build_args(conf, binpath)
    log("monitor loop starting")
    while True:
        log("starting: " + " ".join(args))
        try:
            with open(LOG_FILE, "ab") as out:
                p = subprocess.Popen(args, stdout=out, stderr=out)
            log("child pid=%d" % p.pid)
            rc = p.wait()
            if rc < 0:
                log("child killed by signal %d" % (-rc))
            else:
                log("child exited %d" % rc)
        except Exception as e:
            log("launch error: " + str(e))
        time.sleep(3)

def run_once():
    conf = load_conf()
    binpath = find_bin(conf)
    args = build_args(conf, binpath)
    log("running: " + " ".join(args))
    with open(LOG_FILE, "ab") as out:
        p = subprocess.Popen(args, stdout=out, stderr=out)
        try:
            p.wait()
        except KeyboardInterrupt:
            try:
                p.terminate()
            except:
                pass

def is_systemd_available():
    return shutil.which("systemctl") is not None and Path("/run/systemd/system").exists()

def start():
    if is_systemd_available():
        subprocess.run(["systemctl","start",UNIT])
        log("systemctl start requested")
        return
    # start background monitor
    cmd = [sys.executable, str(Path(__file__)), "--run"]
    with open(LOG_FILE, "a") as out:
        p = subprocess.Popen(cmd, stdout=out, stderr=out, preexec_fn=os.setsid)
    try:
        PID_FILE.write_text(str(p.pid))
    except:
        pass
    log("background monitor started pid=%d" % p.pid)

def stop():
    if is_systemd_available():
        subprocess.run(["systemctl","stop",UNIT])
        log("systemctl stop requested")
        return
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
            except:
                pass
            PID_FILE.unlink()
            log("background monitor stopped")
        except Exception as e:
            log("stop error: " + str(e))
    else:
        subprocess.run(["pkill","-f","pingtunnel"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("pkill fallback used")

def status():
    if is_systemd_available():
        subprocess.run(["systemctl","status",UNIT,"--no-pager"])
        return
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)
            print("monitor running pid", pid)
            return
        except:
            print("stale pidfile")
    subprocess.run(["pgrep","-fl","pingtunnel"])

def logs(n=200):
    if LOG_FILE.exists():
        subprocess.run(["tail","-n", str(n), str(LOG_FILE)])
    else:
        print("no logs yet at", LOG_FILE)

def edit():
    editor = os.environ.get("EDITOR","nano")
    subprocess.run([editor, str(CONF)])

def update():
    urlmap = URLS
    m = platform.machine().lower()
    url = urlmap.get(m)
    if not url:
        if "arm" in m:
            url = urlmap.get("arm64")
        else:
            url = urlmap.get("x86_64")
    tmp = tempfile.mktemp(suffix=".zip")
    log("downloading update " + str(url))
    urllib.request.urlretrieve(url, tmp)
    safe_extract(tmp, BIN_DIR)
    os.remove(tmp)
    b = find_bin(load_conf())
    b.chmod(b.stat().st_mode | 0o111)
    log("binary updated to " + str(b))

def uninstall():
    stop()
    os.system("systemctl disable pingtunnel.service")
    os.system("rm -rf /var/log/pingtunnel")
    os.system("rm -rf /opt/pingtunnel")
    if is_systemd_available():
        subprocess.run(["systemctl","disable",UNIT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            dr = Path("/etc/systemd/system") / (UNIT + ".d")
            if dr.exists():
                for f in dr.iterdir():
                    f.unlink()
                dr.rmdir()
        except:
            pass
    try:
        if Path("__INSTALL_DIR__").exists():
            shutil.rmtree(Path("__INSTALL_DIR__"))
    except Exception as e:
        log("remove error: " + str(e))
    try:
        if Path("__LOG_DIR__").exists():
            shutil.rmtree(Path("__LOG_DIR__"))
    except:
        pass
    try:
        u = Path("__UNIT_PATH__")
        if u.exists():
            u.unlink()
            subprocess.run(["systemctl","daemon-reload"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except:
        pass
    try:
        sl = Path("__SYMLINK__")
        if sl.exists() or sl.is_symlink():
            sl.unlink()
    except:
        pass
    log("uninstall finished")

def safe_extract(zipfile_path, target_dir):
    with zipfile.ZipFile(zipfile_path, "r") as z:
        for name in z.namelist():
            resolved = (target_dir / name).resolve()
            if not str(resolved).startswith(str(target_dir.resolve())):
                raise Exception("unsafe zip")
        z.extractall(target_dir)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd == "--run":
            monitor_loop()
        elif cmd == "start":
            start()
        elif cmd == "stop":
            stop()
        elif cmd == "restart":
            stop(); time.sleep(1); start()
        elif cmd == "status":
            status()
        elif cmd == "logs":
            logs(int(sys.argv[2]) if len(sys.argv) > 2 else 200)
        elif cmd == "edit":
            edit()
        elif cmd == "update":
            update()
        elif cmd == "--uninstall":
            uninstall()
        elif cmd == "uninstall":
            uninstall()
        else:
            print("commands: --run start stop restart status logs edit update uninstall")
    else:
        while True:
            print("Pingtunnel menu: 1)start 2)stop 3)restart 4)status 5)logs 6)edit 7)update 8)uninstall 9)exit")
            c = input("choice: ").strip()
            if c == "1": start()
            elif c == "2": stop()
            elif c == "3": stop(); time.sleep(1); start()
            elif c == "4": status()
            elif c == "5": logs()
            elif c == "6": edit()
            elif c == "7": update()
            elif c == "8": uninstall()
            elif c == "9": break
            else: print("invalid")
'''

def write_runner():
    s = RUNNER_TEMPLATE.replace("__INSTALL_DIR__", str(INSTALL_DIR))
    s = s.replace("__LOG_DIR__", str(LOG_DIR))
    s = s.replace("__UNIT__", SYSTEMD_UNIT)
    s = s.replace("__URLS_JSON__", json.dumps(URLS))
    s = s.replace("__UNIT_PATH__", str(UNIT_PATH))
    s = s.replace("__SYMLINK__", str(SYMLINK))
    s = s.replace("__LOG_DIR__", str(LOG_DIR))
    s = s.replace("__INSTALL_DIR__", str(INSTALL_DIR))
    open(RUNNER_PATH, "w").write(s)
    try:
        RUNNER_PATH.chmod(0o700)
    except:
        pass

def write_systemd_unit():
    content = f"""[Unit]
Description=pingtunnel runner
After=network.target

[Service]
Type=simple
ExecStart={sys.executable} {RUNNER_PATH} --run
Restart=on-failure
RestartSec=5
WorkingDirectory={INSTALL_DIR}
StandardOutput=append:{LOG_DIR}/pingtunnel.log
StandardError=append:{LOG_DIR}/pingtunnel.log

[Install]
WantedBy=multi-user.target
"""
    UNIT_PATH.write_text(content)
    subprocess.run(["systemctl", "daemon-reload"], check=False)

def create_symlink_to_runner():
    try:
        if SYMLINK.exists() or SYMLINK.is_symlink():
            SYMLINK.unlink()
        SYMLINK.symlink_to(RUNNER_PATH)
    except Exception as e:
        print("symlink error:", e)

def interactive_config():
    cfg = {}
    t = input("Type (server/client) [server]: ").strip().lower() or "server"
    cfg["type"] = "server" if t not in ("server","client") else t
    if cfg["type"] == "client":
        lp = input("Forward port for -l (port only, e.g. 4000): ").strip() or "4000"
        try:
            cfg["l_port"] = int(lp)
        except:
            cfg["l_port"] = int(4000)
        server = input("Server IP (-s) or host: ").strip()
        cfg["server"] = server or "127.0.0.1"
    key = input("Connection key/password (-key) be number [123456]: ").strip() or "123456"
    cfg["key"] = key
    tcp = input("Use TCP? (-tcp) 1=yes 0=no [1]: ").strip() or "1"
    try:
        cfg["tcp"] = int(tcp)
    except:
        cfg["tcp"] = 1
    mem = input("Memory limit for systemd in MB (0 to disable) [500]: ").strip() or "500"
    try:
        cfg["memory_mb"] = int(mem)
    except:
        cfg["memory_mb"] = 0
    aut = input("Enable systemd autostart? (y/N) [N]: ").strip().lower()
    cfg["autostart"] = aut == "y"
    cfg["installed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    print("Saved config to", CONFIG_PATH)

def apply_memory_dropin(mem_mb):
    dropin_dir = Path("/etc/systemd/system") / (SYSTEMD_UNIT + ".d")
    if mem_mb and mem_mb > 0:
        dropin_dir.mkdir(parents=True, exist_ok=True)
        (dropin_dir / "memory.conf").write_text("[Service]\nMemoryLimit=%dM\n" % mem_mb)
        subprocess.run(["systemctl","daemon-reload"], check=False)
    else:
        try:
            f = dropin_dir / "memory.conf"
            if f.exists():
                f.unlink()
                subprocess.run(["systemctl","daemon-reload"], check=False)
        except:
            pass

def install_flow():
    if not is_root():
        die("run installer as root")
    ensure_dirs()
    url = detect_download_url()
    if not url:
        die("unsupported arch")
    tmp = tempfile.mktemp(suffix=".zip")
    print("Downloading pingtunnel:", url)
    download_file(url, tmp)
    print("Extracting...")
    safe_extract(tmp, BIN_DIR)
    try:
        os.remove(tmp)
    except:
        pass
    binp = find_pingtunnel_binary()
    if not binp:
        die("could not find pingtunnel binary after extract")
    try:
        binp.chmod(0o755)
    except:
        pass
    write_runner()
    if not CONFIG_PATH.exists():
        interactive_config()
    else:
        print("Config already exists at", CONFIG_PATH)
    create_symlink_to_runner()
    write_systemd_unit()
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        apply_memory_dropin(cfg.get("memory_mb", 0))
    except:
        pass
    if cfg.get("autostart"):
        if shutil.which("systemctl"):
            subprocess.run(["systemctl", "enable", SYSTEMD_UNIT], check=False)
            subprocess.run(["systemctl", "start", SYSTEMD_UNIT], check=False)
            print("Service enabled and started via systemd")
        else:
            print("systemd not found; autostart skipped")
    else:
        print("Installation complete. Use 'pingtunnel start' to run the runner.")

def uninstall_flow():
    if not is_root():
        die("run as root")
    # try to stop
    if shutil.which("systemctl"):
        subprocess.run(["systemctl","stop", SYSTEMD_UNIT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["systemctl","disable", SYSTEMD_UNIT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        dr = Path("/etc/systemd/system") / (SYSTEMD_UNIT + ".d")
        if dr.exists():
            for f in dr.iterdir():
                f.unlink()
            dr.rmdir()
            subprocess.run(["systemctl","daemon-reload"], check=False)
    except:
        pass
    try:
        if UNIT_PATH.exists():
            UNIT_PATH.unlink()
            subprocess.run(["systemctl","daemon-reload"], check=False)
    except:
        pass
    try:
        if SYMLINK.exists() or SYMLINK.is_symlink():
            SYMLINK.unlink()
    except:
        pass
    try:
        if RUNNER_PATH.exists():
            RUNNER_PATH.unlink()
    except:
        pass
    try:
        if INSTALL_DIR.exists():
            shutil.rmtree(INSTALL_DIR)
    except:
        pass
    try:
        if LOG_DIR.exists():
            shutil.rmtree(LOG_DIR)
    except:
        pass
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except:
        pass
    print("Uninstalled")

def menu_install_prompt():
    if not is_root():
        die("run as root")
    print("Pingtunnel installer — interactive setup")
    install_flow()
    print("Done.")

def main_menu():
    if not is_root():
        die("run as root")
    while True:
        print("""
Pingtunnel setup panel (CLI)
1) Install / Setup (asks config & installs)
2) Edit config
3) Start
4) Stop
5) Restart
6) Status
7) Logs
8) Uninstall
9) Exit
""")
        c = input("choice: ").strip()
        if c == "1":
            install_flow()
        elif c == "2":
            if CONFIG_PATH.exists():
                subprocess.run([str(RUNNER_PATH), "edit"])
            else:
                print("no config, run install first")
        elif c == "3":
            if RUNNER_PATH.exists():
                subprocess.run([str(RUNNER_PATH), "start"])
            else:
                print("not installed")
        elif c == "4":
            if RUNNER_PATH.exists():
                subprocess.run([str(RUNNER_PATH), "stop"])
            else:
                print("not installed")
        elif c == "5":
            if RUNNER_PATH.exists():
                subprocess.run([str(RUNNER_PATH), "restart"])
            else:
                print("not installed")
        elif c == "6":
            if RUNNER_PATH.exists():
                subprocess.run([str(RUNNER_PATH), "status"])
            else:
                print("not installed")
        elif c == "7":
            if RUNNER_PATH.exists():
                subprocess.run([str(RUNNER_PATH), "logs", "200"])
            else:
                print("not installed")
        elif c == "8":
            yn = input("Are you sure? This will remove everything (yes/no): ").strip().lower()
            if yn == "yes":
                uninstall_flow()
                break
        elif c == "9":
            break
        else:
            print("invalid")

# Entry
if __name__ == "__main__":
    if len(sys.argv) > 1:
        a = sys.argv[1].lower()
        if a in ("install","setup"):
            install_flow()
        elif a == "uninstall":
            uninstall_flow()
        elif a in ("start","stop","restart","status","logs","edit","update"):
            if RUNNER_PATH.exists():
                subprocess.run([str(RUNNER_PATH), a] + sys.argv[2:])
            else:
                print("runner not found; install first")
        else:
            print("unknown argument")
    else:
        # run interactive installer/panel that asks config by default
        menu_install_prompt()
