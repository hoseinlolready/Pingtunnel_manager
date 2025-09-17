#!/usr/bin/env python3
import os, sys, json, stat, platform, tempfile, urllib.request, zipfile, subprocess, shutil, time, signal
from getpass import getpass
from pathlib import Path
from datetime import datetime

INSTALL_DIR = Path("/opt/pingtunnel")
BIN_DIR = INSTALL_DIR / "bin"
CONF_DIR = INSTALL_DIR / "conf"
LOG_DIR = Path("/var/log/pingtunnel")
RUNNER = INSTALL_DIR / "run_pingtunnel.py"
SYMLINK = Path("/usr/local/bin/pingtunnel")
SYSTEMD_UNIT = "pingtunnel.service"
UNIT_PATH = Path("/etc/systemd/system") / SYSTEMD_UNIT
PIDFILE = Path("/var/run/pingtunnel.pid")
LOG_FILE = LOG_DIR / "pingtunnel.log"
LOGROTATE_PATH = Path("/etc/logrotate.d/pingtunnel")
DEFAULT_MEMORY_MB = 512

URLS = {
    "amd64": "https://github.com/esrrhs/pingtunnel/releases/download/2.8/pingtunnel_linux_amd64.zip",
    "arm64": "https://github.com/esrrhs/pingtunnel/releases/download/2.8/pingtunnel_linux_arm64.zip",
    "386":  "https://github.com/esrrhs/pingtunnel/releases/download/2.8/pingtunnel_linux_386.zip",
}

def now(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log(s):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"{now()} {s}\n")
    except Exception:
        pass
    print(f"{now()} {s}")

def run(cmd, check=False):
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)
        return r
    except subprocess.CalledProcessError as e:
        return e

def detect_arch():
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"): return "amd64"
    if m in ("aarch64", "arm64"): return "arm64"
    if m in ("i386", "i486", "i586", "i686", "x86"): return "386"
    if "arm" in m: return "arm64"
    return None

def download_with_retries(url, dest_path, tries=3, timeout=30):
    last_err = None
    for attempt in range(1, tries+1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent":"curl/7.64.1"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            with open(dest_path, "wb") as f:
                f.write(data)
            return True
        except Exception as e:
            last_err = e
            log(f"download attempt {attempt} failed: {e}")
            time.sleep(1 + attempt)
    raise RuntimeError(f"download failed: {last_err}")

def safe_extract_zip(zip_path, dest_dir):
    with zipfile.ZipFile(zip_path, "r") as z:
        members = z.namelist()
        z.extractall(dest_dir)
    return members

def find_pingtunnel_binary(search_dir):
    for p in search_dir.rglob("*"):
        if p.is_file() and "pingtunnel" in p.name and p.stat().st_size > 10000:
            return p
    return None

def write_json(path, data, mode=0o600):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, mode)

def read_json(path):
    with open(path) as f:
        return json.load(f)

def create_systemd_unit(runner_path):
    unit = f"""[Unit]
Description=pingtunnel monitor
After=network.target

[Service]
Type=simple
ExecStart={sys.executable} {runner_path} --run
Restart=on-failure
RestartSec=5
LimitNOFILE=65536
WorkingDirectory={INSTALL_DIR}
StandardOutput=append:{LOG_FILE}
StandardError=append:{LOG_FILE}

[Install]
WantedBy=multi-user.target
"""
    UNIT_PATH.write_text(unit)
    run(["systemctl", "daemon-reload"])

def create_logrotate():
    s = f"""{LOG_DIR}/*.log {{
    daily
    rotate 14
    compress
    missingok
    notifempty
    create 0640 root root
    sharedscripts
    postrotate
        systemctl try-restart {SYSTEMD_UNIT} > /dev/null 2>&1 || true
    endscript
}}
"""
    try:
        LOGROTATE_PATH.write_text(s)
    except Exception:
        pass

def install_flow():
    if os.geteuid() != 0:
        sys.exit("run installer as root")
    arch = detect_arch()
    if not arch:
        arch = input("arch (amd64/arm64/386): ").strip()
    if arch not in URLS:
        sys.exit("unsupported arch")
    url = URLS[arch]
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    tmpzip = tempfile.mktemp(prefix="pt_", suffix=".zip")
    log(f"downloading {url}")
    download_with_retries(url, tmpzip, tries=5)
    log("extracting")
    members = safe_extract_zip(tmpzip, BIN_DIR)
    os.unlink(tmpzip)
    binpath = find_pingtunnel_binary(BIN_DIR)
    if not binpath:
        sys.exit("could not find binary in archive")
    binpath.chmod(binpath.stat().st_mode | stat.S_IXUSR)
    default_mode = "server"
    mode = input(f"mode (server/client) [{default_mode}]: ").strip() or default_mode
    if mode not in ("server","client"): mode = default_mode
    password = getpass("password (hidden, Enter for changeme): ") or "changeme"
    server_ip = input("server ip (client mode) [127.0.0.1]: ").strip() or "127.0.0.1"
    port = input("tunnel port [4000]: ").strip() or "4000"
    try:
        port = int(port)
    except:
        port = 4000
    mem = input(f"memory limit MB (0=disabled) [{DEFAULT_MEMORY_MB}]: ").strip()
    try:
        mem = int(mem) if mem != "" else DEFAULT_MEMORY_MB
    except:
        mem = DEFAULT_MEMORY_MB
    auto_start = input("enable autostart (systemd) (y/N): ").strip().lower() == "y"
    conf = {
        "mode": mode,
        "password": password,
        "server_ip": server_ip,
        "port": port,
        "memory_mb": mem,
        "auto_start": auto_start,
        "extra_args": "-nolog 1 -noprint 1",
        "binary": str(binpath.resolve()),
        "arch": arch,
        "installed_at": now()
    }
    write_json(CONF_DIR / "config.json", conf)
    with open(RUNNER, "w") as f:
        f.write(RUNNER_SCRIPT)
    os.chmod(RUNNER, 0o700)
    try:
        if SYMLINK.exists() or SYMLINK.is_symlink():
            SYMLINK.unlink()
        SYMLINK.symlink_to(RUNNER)
    except Exception as e:
        log(f"symlink error: {e}")
    create_systemd_unit(RUNNER)
    create_logrotate()
    if conf["auto_start"]:
        run(["systemctl", "enable", SYSTEMD_UNIT])
        run(["systemctl", "start", SYSTEMD_UNIT])
    log("install complete")
    print("use: pingtunnel start|stop|restart|status|logs|config|edit|update|uninstall")

RUNNER_SCRIPT = r"""#!/usr/bin/env python3
import os,sys,json,subprocess,time,signal,shutil
from pathlib import Path
from datetime import datetime
INSTALL_DIR=Path("/opt/pingtunnel")
BIN_DIR=INSTALL_DIR/"bin"
CONF=INSTALL_DIR/"conf"/"config.json"
LOG_DIR=Path("/var/log/pingtunnel")
LOG_FILE=LOG_DIR/"pingtunnel.log"
PIDFILE=Path("/var/run/pingtunnel.pid")
UNIT="pingtunnel.service"
def now(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def log(s):
    try:
        LOG_DIR.mkdir(parents=True,exist_ok=True)
        with open(LOG_FILE,"a") as f: f.write(f"{now()} {s}\n")
    except:
        pass
    print(f"{now()} {s}")
def load_conf():
    if not CONF.exists(): sys.exit("config missing")
    return json.load(open(CONF))
def find_bin(conf):
    p = conf.get("binary")
    if p and Path(p).exists(): return Path(p)
    for x in BIN_DIR.rglob("*"):
        if x.is_file() and "pingtunnel" in x.name:
            return x
    raise SystemExit("binary not found")
def build_args(conf, binp):
    a=[str(binp)]
    if conf.get("mode")=="server":
        a += ["-mode","server","-password",conf["password"],"-port",str(conf["port"])]
    else:
        a += ["-mode","client","-password",conf["password"],"-server",conf["server_ip"],"-port",str(conf["port"])]
    extra = conf.get("extra_args","-nolog 1 -noprint 1")
    if isinstance(extra,str): a += extra.split()
    return a
def apply_systemd_mem(conf):
    m = int(conf.get("memory_mb",0) or 0)
    d = Path("/etc/systemd/system") / (UNIT + ".d")
    f = d / "memory.conf"
    if m>0:
        d.mkdir(parents=True,exist_ok=True)
        f.write_text("[Service]\nMemoryLimit=%dM\n" % m)
        subprocess.run(["systemctl","daemon-reload"])
    else:
        if f.exists(): f.unlink(); subprocess.run(["systemctl","daemon-reload"])
def enforce_mem_and_exec(args, mem_mb):
    mem = int(mem_mb or 0)
    if mem>0:
        try:
            import resource
            pid = os.fork()
            if pid>0:
                return pid
            else:
                maxbytes = mem * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (maxbytes, maxbytes))
                os.execv(args[0], args)
        except Exception as e:
            log(f"setrlimit failed: {e}, trying prlimit")
            pr = shutil.which("prlimit")
            if pr:
                cmd = [pr, f"--as={mem*1024*1024}", "--"] + args
                return subprocess.Popen(cmd).pid
            else:
                return subprocess.Popen(args).pid
    else:
        return subprocess.Popen(args).pid
def monitor_loop():
    conf = load_conf()
    binp = find_bin(conf)
    apply_systemd_mem(conf)
    args = build_args(conf, binp)
    log("monitor loop starting")
    while True:
        log("starting child")
        try:
            mem = conf.get("memory_mb",0) or 0
            if os.path.exists("/run/systemd/system") or shutil.which("systemctl"):
                p = subprocess.Popen(args)
                pid = p.pid
                log(f"child pid={pid}")
                r = p.wait()
                if r<0:
                    log(f"child killed by signal {-r}")
                else:
                    log(f"child exited {r}")
            else:
                pid = enforce_mem_and_exec(args, mem)
                log(f"child pid={pid}")
                _, status = os.waitpid(pid, 0)
                if os.WIFSIGNALED(status):
                    sig = os.WTERMSIG(status)
                    log(f"child signaled {sig}")
                else:
                    code = os.WEXITSTATUS(status)
                    log(f"child exited {code}")
        except Exception as e:
            log("monitor exception: "+str(e))
        time.sleep(3)
def is_systemd_available():
    return os.path.exists("/run/systemd/system") or shutil.which("systemctl")
def start_service():
    if is_systemd_available():
        subprocess.run(["systemctl","start",UNIT])
        log("systemd start requested")
        return
    if PIDFILE.exists():
        try:
            pid = int(PIDFILE.read_text().strip())
            os.kill(pid,0)
            print("already running")
            return
        except Exception:
            PIDFILE.unlink()
    cmd = [sys.executable, str(Path(__file__)), "--run"]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
    PIDFILE.write_text(str(p.pid))
    log(f"started background pid={p.pid}")
def stop_service():
    if is_systemd_available():
        subprocess.run(["systemctl","stop",UNIT])
        log("systemd stop requested")
        return
    if PIDFILE.exists():
        try:
            pid = int(PIDFILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
            try:
                os.kill(pid,0)
                os.kill(pid, signal.SIGKILL)
            except:
                pass
            PIDFILE.unlink()
            log("stopped background")
        except Exception as e:
            log("stop error: "+str(e))
def status():
    if is_systemd_available():
        r = subprocess.run(["systemctl","status",UNIT,"--no-pager"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print(r.stdout.decode() or r.stderr.decode())
        return
    if PIDFILE.exists():
        try:
            pid = int(PIDFILE.read_text().strip())
            os.kill(pid,0)
            print("running pid",pid)
            return
        except Exception:
            print("stale pidfile")
    print("not running")
def logs(n=200):
    if LOG_FILE.exists():
        lines = open(LOG_FILE).read().splitlines()[-n:]
        print("\n".join(lines))
    else:
        print("no logs yet")
def config_print():
    print(json.dumps(load_conf(), indent=2))
def edit_config():
    editor = os.environ.get("EDITOR","nano")
    subprocess.run([editor, str(CONF)])
def update_binary():
    conf = load_conf()
    arch = conf.get("arch")
    urls = {
        "amd64":"https://github.com/esrrhs/pingtunnel/releases/download/2.8/pingtunnel_linux_amd64.zip",
        "arm64":"https://github.com/esrrhs/pingtunnel/releases/download/2.8/pingtunnel_linux_arm64.zip",
        "386":"https://github.com/esrrhs/pingtunnel/releases/download/2.8/pingtunnel_linux_386.zip"
    }
    url = urls.get(arch)
    if not url: print("unknown arch"); return
    import urllib.request, tempfile, zipfile, os
    tmp = tempfile.mktemp(suffix=".zip")
    print("downloading",url)
    urllib.request.urlretrieve(url, tmp)
    with zipfile.ZipFile(tmp) as z:
        z.extractall(BIN_DIR)
    os.unlink(tmp)
    b = find_bin(load_conf())
    b.chmod(b.stat().st_mode | 0o111)
    print("updated binary to",b)
def uninstall():
    stop_service()
    try:
        if UNIT:
            subprocess.run(["systemctl", "disable", UNIT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except:
        pass
    for p in [str(INSTALL_DIR), str(SYMLINK), str(PIDFILE), str(LOG_FILE), str(Path("/etc/logrotate.d/pingtunnel"))]:
        try:
            path = Path(p)
            if path.is_symlink(): path.unlink()
            elif path.is_file(): path.unlink()
            elif path.is_dir(): shutil.rmtree(path)
        except:
            pass
    try:
        if Path("/etc/systemd/system").exists():
            d = Path("/etc/systemd/system") / UNIT
            if d.exists(): d.unlink()
            subprocess.run(["systemctl","daemon-reload"])
    except:
        pass
    print("uninstalled")
if __name__=="__main__":
    import json, shutil
    if len(sys.argv)>1:
        cmd = sys.argv[1]
        if cmd=="--run":
            monitor_loop()
        elif cmd=="start":
            start_service()
        elif cmd=="stop":
            stop_service()
        elif cmd=="restart":
            stop_service(); time.sleep(1); start_service()
        elif cmd=="status":
            status()
        elif cmd=="logs":
            n = int(sys.argv[2]) if len(sys.argv)>2 else 200
            logs(n)
        elif cmd=="config":
            config_print()
        elif cmd=="edit":
            edit_config()
        elif cmd=="update":
            update_binary()
        elif cmd=="uninstall":
            uninstall()
        else:
            print("commands: start stop restart status logs config edit update uninstall")
    else:
        print("commands: start stop restart status logs config edit update uninstall")
"""

if __name__ == "__main__":
    try:
        install_flow()
    except Exception as e:
        log("installer failed: " + str(e))
        raise
