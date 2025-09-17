#!/usr/bin/env python3
import os
import sys
import json
import time
import shutil
import signal
import platform
import tempfile
import zipfile
import urllib.request
import subprocess
from pathlib import Path

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

def is_root():
    return os.geteuid() == 0

def die(msg="error"):
    print(msg, file=sys.stderr)
    sys.exit(1)

def detect_url():
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

def download_zip(url, dest, tries=4):
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

def safe_extract(zipfile_path, target_dir):
    with zipfile.ZipFile(zipfile_path, "r") as z:
        for name in z.namelist():
            dest = (target_dir / name).resolve()
            if not str(dest).startswith(str(target_dir.resolve())):
                raise Exception("unsafe zip")
        z.extractall(str(target_dir))

def find_binary():
    for p in BIN_DIR.rglob("*"):
        if p.is_file() and "pingtunnel" in p.name.lower():
            try:
                p.chmod(p.stat().st_mode | 0o111)
            except Exception:
                pass
            return p
    return None

RUNNER_CONTENT = r'''#!/usr/bin/env python3
import os, sys, json, subprocess, time, signal, shutil
from pathlib import Path

INSTALL_DIR = Path("{install_dir}")
BIN_DIR = INSTALL_DIR / "bin"
CONF = INSTALL_DIR / "conf" / "config.json"
LOG_DIR = Path("{log_dir}")
LOG_FILE = LOG_DIR / "pingtunnel.log"
UNIT = "{systemd_unit}"
PID_FILE = Path("/run/pingtunnel.pid")

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
    raise SystemExit("pingtunnel binary not found")

def build_args(conf, binpath):
    args = [str(binpath)]
    mode = conf.get("mode","server")
    if mode == "server":
        args += ["-mode","server"]
    else:
        args += ["-mode","client"]
    if conf.get("listen"):
        args += ["-l", str(conf.get("listen"))]
    else:
        if mode == "client":
            args += ["-server", str(conf.get("server_ip","127.0.0.1")), "-port", str(conf.get("port",4000))]
        else:
            # server default uses -port
            args += ["-port", str(conf.get("port",4000))]
    if conf.get("password"):
        args += ["-password", str(conf.get("password"))]
    extra = conf.get("extra_args")
    if extra:
        if isinstance(extra, list):
            args += extra
        else:
            args += str(extra).split()
    if "-nolog" not in args and "nolog" in conf:
        args += ["-nolog", str(conf.get("nolog",1))]
    if "-noprint" not in args and "noprint" in conf:
        args += ["-noprint", str(conf.get("noprint",1))]
    return args

def apply_systemd_memory(conf):
    m = int(conf.get("memory_mb",0) or 0)
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

def monitor():
    conf = load_conf()
    binp = find_bin(conf)
    apply_systemd_memory(conf)
    args = build_args(conf, binp)
    log("monitor loop started")
    while True:
        log("launch: " + " ".join(args))
        try:
            p = subprocess.Popen(args)
            log("started pid=%d" % p.pid)
            rc = p.wait()
            if rc < 0:
                log("killed by signal %d" % (-rc))
            else:
                log("exited %d" % rc)
        except Exception as e:
            log("launch error: " + str(e))
        time.sleep(3)

def is_systemd():
    return shutil.which("systemctl") is not None

def start():
    if is_systemd():
        subprocess.run(["systemctl","start",UNIT])
        log("systemctl start requested")
        return
    cmd = [sys.executable, str(Path(__file__)), "--run"]
    with open(LOG_FILE, "a") as out:
        p = subprocess.Popen(cmd, stdout=out, stderr=out, preexec_fn=os.setsid)
    try:
        PID_FILE.write_text(str(p.pid))
    except:
        pass
    log("background monitor started pid=%d" % p.pid)

def stop():
    if is_systemd():
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
    if is_systemd():
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

def logs(lines=200):
    if LOG_FILE.exists():
        subprocess.run(["tail","-n", str(lines), str(LOG_FILE)])
    else:
        print("no logs yet at", LOG_FILE)

def edit():
    editor = os.environ.get("EDITOR","nano")
    subprocess.run([editor, str(CONF)])

def update():
    url_map = {url_map}
    m = platform.machine().lower()
    url = url_map.get(m)
    if not url:
        if "arm" in m:
            url = url_map.get("arm64")
        else:
            url = url_map.get("x86_64")
    tmp = tempfile.mktemp(suffix=".zip")
    log("downloading update " + str(url))
    urllib.request.urlretrieve(url, tmp)
    with zipfile.ZipFile(tmp, "r") as z:
        z.extractall(BIN_DIR)
    os.remove(tmp)
    b = find_bin(load_conf())
    b.chmod(b.stat().st_mode | 0o111)
    log("binary updated " + str(b))

def uninstall():
    stop()
    if is_systemd():
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
        if Path("{install_dir}").exists():
            shutil.rmtree(Path("{install_dir}"))
    except Exception as e:
        log("remove error: " + str(e))
    try:
        if Path("{log_dir}").exists():
            shutil.rmtree(Path("{log_dir}"))
    except:
        pass
    try:
        u = Path("{unit_path}")
        if u.exists():
            u.unlink()
            subprocess.run(["systemctl","daemon-reload"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except:
        pass
    try:
        sl = Path("{symlink}")
        if sl.exists() or sl.is_symlink():
            sl.unlink()
    except:
        pass
    log("uninstall finished")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        a = sys.argv[1].lower()
        if a == "--run":
            monitor()
        elif a == "start":
            start()
        elif a == "stop":
            stop()
        elif a == "restart":
            stop(); time.sleep(1); start()
        elif a == "status":
            status()
        elif a == "logs":
            logs(int(sys.argv[2]) if len(sys.argv) > 2 else 200)
        elif a == "edit":
            edit()
        elif a == "update":
            update()
        elif a == "uninstall":
            uninstall()
        else:
            print("commands: --run start stop restart status logs edit update uninstall")
    else:
        while True:
            print("\\nrun_pingtunnel menu: 1)start 2)stop 3)restart 4)status 5)logs 6)edit 7)update 8)uninstall 9)exit")
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
'''.replace("{install_dir}", str(INSTALL_DIR)).replace("{log_dir}", str(LOG_DIR)).replace("{systemd_unit}", SYSTEMD_UNIT).replace("{unit_path}", str(UNIT_PATH)).replace("{symlink}", str(SYMLINK)).replace("{url_map}", json.dumps(URLS))

def write_runner():
    RUNNER_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNNER_PATH.write_text(RUNNER_CONTENT)
    RUNNER_PATH.chmod(0o700)

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

def create_symlink():
    try:
        if SYMLINK.exists() or SYMLINK.is_symlink():
            SYMLINK.unlink()
        SYMLINK.symlink_to(RUNNER_PATH)
    except Exception as e:
        print("symlink error:", e)

def create_default_config_interactive():
    cfg = {}
    print("Config setup:")
    mode = input("Mode (server/client) [server]: ").strip().lower() or "server"
    cfg["mode"] = "server" if mode not in ("server","client") else mode
    if cfg["mode"] == "client":
        server_ip = input("Server IP or host [127.0.0.1]: ").strip() or "127.0.0.1"
        cfg["server_ip"] = server_ip
    listen = input("Listen (format ip:port) leave blank to use server_ip+port: ").strip()
    if listen:
        cfg["listen"] = listen
    port = input("Tunnel port [4000]: ").strip() or "4000"
    try:
        cfg["port"] = int(port)
    except:
        cfg["port"] = 4000
    password = input("Password (leave blank for 'changeme'): ").strip() or "changeme"
    cfg["password"] = password
    mem = input("Memory limit in MB (0 to disable) [512]: ").strip()
    try:
        cfg["memory_mb"] = int(mem) if mem != "" else 512
    except:
        cfg["memory_mb"] = 512
    nolog = input("Disable logging to disk? nolog (1=yes,0=no) [1]: ").strip() or "1"
    noprint = input("Disable printing? noprint (1=yes,0=no) [1]: ").strip() or "1"
    try:
        cfg["nolog"] = int(nolog)
    except:
        cfg["nolog"] = 1
    try:
        cfg["noprint"] = int(noprint)
    except:
        cfg["noprint"] = 1
    extra = input("Extra args (space-separated) [empty]: ").strip()
    cfg["extra_args"] = extra
    cfg["installed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    print("config saved to", CONFIG_PATH)

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
        die("run as root")
    ensure_dirs()
    url = detect_url()
    if not url:
        die("arch not supported")
    tmp = tempfile.mktemp(suffix=".zip")
    print("downloading:", url)
    download_zip(url, tmp)
    print("extracting...")
    safe_extract(tmp, BIN_DIR)
    os.remove(tmp)
    binp = find_binary()
    if not binp:
        die("binary not found after extract")
    binp.chmod(0o755)
    write_runner()
    create_default = not CONFIG_PATH.exists()
    if create_default:
        create_default_config_interactive()
    else:
        print("config already exists at", CONFIG_PATH)
    create_symlink()
    write_systemd_unit()
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        apply_memory_dropin(cfg.get("memory_mb", 0))
    except Exception:
        pass
    if shutil.which("systemctl"):
        subprocess.run(["systemctl","enable", SYSTEMD_UNIT], check=False)
        subprocess.run(["systemctl","start", SYSTEMD_UNIT], check=False)
        print("installed and started service")
    else:
        print("installed. systemd not available; use 'pingtunnel start' to run the runner")

def uninstall_flow():
    if not is_root():
        die("run as root")
    # if pingtunnel binary contains its own uninstall command, try it first
    try:
        b = find_binary()
        if b:
            # try to run 'pingtunnel uninstall' if supported; best-effort
            try:
                subprocess.run([str(b), "uninstall"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            except Exception:
                pass
    except Exception:
        pass
    try:
        if shutil.which("systemctl"):
            subprocess.run(["systemctl","stop", SYSTEMD_UNIT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["systemctl","disable", SYSTEMD_UNIT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except:
        pass
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
    print("uninstalled")

def show_panel_menu():
    if not is_root():
        die("run as root")
    while True:
        print("""
Pingtunnel Panel (CLI)
1) Install / Setup
2) Configure / Reconfigure
3) Start
4) Stop
5) Restart
6) Status
7) View Logs
8) Edit Config
9) Update Binary
10) Uninstall
11) Exit
""")
        c = input("choice: ").strip()
        if c == "1":
            install_flow()
        elif c == "2":
            create_default_config_interactive()
            try:
                cfg = json.loads(CONFIG_PATH.read_text())
                apply_memory_dropin(cfg.get("memory_mb", 0))
            except:
                pass
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
            if RUNNER_PATH.exists():
                subprocess.run([str(RUNNER_PATH), "edit"])
            else:
                print("not installed")
        elif c == "9":
            if RUNNER_PATH.exists():
                subprocess.run([str(RUNNER_PATH), "update"])
            else:
                print("not installed")
        elif c == "10":
            yn = input("Are you sure? This will remove everything. (yes/no): ").strip().lower()
            if yn == "yes":
                uninstall_flow()
                break
        elif c == "11":
            break
        else:
            print("invalid")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd in ("install","setup"):
            install_flow()
        elif cmd == "uninstall":
            uninstall_flow()
        elif cmd in ("start","stop","restart","status","logs","edit","update"):
            if RUNNER_PATH.exists():
                subprocess.run([str(RUNNER_PATH), cmd] + sys.argv[2:])
            else:
                print("runner not found; install first")
        else:
            print("unknown arg. use no args to open panel.")
    else:
        show_panel_menu()
