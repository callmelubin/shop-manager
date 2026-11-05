# -*- coding: utf-8 -*-
"""内嵌 SID 抓取：调用交接包里的 mitmproxy 后端 exe，临时开系统代理，抓到后还原。"""
import atexit
import json
import os
import re
import socket
import subprocess
import time
import winreg


class CaptureError(Exception):
    pass


PORT = 18080
KEYS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def _read_proxy():
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEYS)
    out = {}
    for name, key, default in (("ProxyEnable", "enable", 0),
                               ("ProxyServer", "server", ""),
                               ("ProxyOverride", "override", "")):
        try:
            out[key] = winreg.QueryValueEx(k, name)[0]
        except OSError:
            out[key] = default
    winreg.CloseKey(k)
    return out


def _write_proxy(enable, server="", override=""):
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEYS, 0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, int(enable))
    if server is not None:
        winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, server)
    if override is not None:
        winreg.SetValueEx(k, "ProxyOverride", 0, winreg.REG_SZ, override)
    winreg.CloseKey(k)


_ACTIVE_PROXY = None


def _restore_on_exit():
    global _ACTIVE_PROXY
    if not _ACTIVE_PROXY:
        return
    try:
        _write_proxy(_ACTIVE_PROXY["enable"], _ACTIVE_PROXY["server"],
                     _ACTIVE_PROXY["override"])
    except Exception:  # noqa: BLE001
        pass
    _ACTIVE_PROXY = None


atexit.register(_restore_on_exit)


def cert_installed():
    try:
        p = subprocess.run(["certutil", "-store", "-user", "Root"],
                           capture_output=True, timeout=20)
        blob = (p.stdout or b"") + (p.stderr or b"")
        return b"mitmproxy" in blob.lower()
    except Exception:  # noqa: BLE001
        return False


def install_cert(pem):
    if not os.path.exists(pem):
        raise CaptureError("找不到 mitmproxy 证书文件")
    p = subprocess.run(["certutil", "-addstore", "-user", "Root", pem],
                       capture_output=True, timeout=30)
    if p.returncode != 0:
        msg = (p.stderr or p.stdout or b"").decode("gbk", "ignore")[:200]
        raise CaptureError(f"证书安装失败：{msg}")
    return cert_installed()


def port_free(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


def find_port(start=PORT, tries=40):
    for p in range(start, min(start + tries, 65535)):
        if port_free(p):
            return p
    raise CaptureError("找不到空闲端口")


def kill_backends(log=None):
    """清掉所有残留的 SID抓取后端.exe（PyInstaller 子进程也一起杀）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH", "/FI", "IMAGENAME eq SID抓取后端.exe"],
            capture_output=True, timeout=15)
        text = (out.stdout or b"").decode("utf-8", "ignore") or \
               (out.stdout or b"").decode("gbk", "ignore")
        pids = re.findall(r'"SID抓取后端\.exe","(\d+)"', text, re.I)
        for pid in pids:
            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"],
                           capture_output=True, timeout=10)
            if log:
                log(f"已清理残留抓包进程 PID {pid}")
        return len(pids)
    except Exception:  # noqa: BLE001
        return 0


class SidCapture:
    """start() → 开代理；poll() → 读抓到的 SID；stop() → 关代理并结束子进程。"""

    def __init__(self, app_dir, data_dir, log=print):
        self.app_dir = app_dir
        self.data_dir = data_dir
        self.log = log
        self.exe = os.path.join(app_dir, "tools", "sid", "SID抓取后端.exe")
        self.addon = os.path.join(app_dir, "tools", "sid", "sid_addon.py")
        self.pem = os.path.join(app_dir, "tools", "sid", "mitmproxy-ca-cert.pem")
        self.out = os.path.join(data_dir, "capture", "sid_capture.jsonl")
        self.debug = os.path.join(data_dir, "capture", "requests.log")
        os.makedirs(os.path.dirname(self.out), exist_ok=True)
        self.proc = None
        self.port = None
        self.old_proxy = None
        self.offset = 0
        self.backend_log = os.path.join(self.data_dir, "capture", "backend.log")

    def available(self):
        return os.path.exists(self.exe) and os.path.exists(self.addon)

    def start(self):
        if not self.available():
            raise CaptureError("抓取组件缺失（tools\\sid 不完整）")
        if self.proc:
            return
        if not cert_installed():
            raise CaptureError("mitmproxy 证书未安装")
        self.port = find_port()
        env = dict(os.environ)
        env["SID_CAPTURE"] = self.out
        self.debug = os.path.join(os.path.dirname(self.out), "requests.log")
        open(self.debug, "w", encoding="utf-8").close()
        env["SID_DEBUG"] = self.debug
        open(self.out, "a", encoding="utf-8").close()
        self.offset = os.path.getsize(self.out)
        self.log("启动抓包后端…")
        logf = open(self.backend_log, "ab")
        self.proc = subprocess.Popen(
            [self.exe, "--mitm", "-q", "-p", str(self.port), "-s", self.addon,
             "--set", "block_global=false", "--set", "connection_timeout=15"],
            env=env, stdout=logf, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._logf = logf
        ready = False
        for _ in range(24):
            if self.proc.poll() is not None:
                break
            if not port_free(self.port):
                ready = True
                break
            time.sleep(0.5)
        if self.proc.poll() is not None or not ready:
            rc = self.proc.poll()
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            self.proc = None
            tail = ""
            try:
                with open(self.backend_log, encoding="utf-8", errors="ignore") as f:
                    tail = f.read()[-500:]
            except OSError:
                pass
            raise CaptureError(f"抓包后端启动失败（退出码 {rc}）："
                               + (tail.strip() or "无输出"))
        self.old_proxy = _read_proxy()
        try:
            _write_proxy(1, f"127.0.0.1:{self.port}",
                         "<local>;127.*;10.*;172.16.*;192.168.*")
        except OSError as e:
            raise CaptureError(f"无法修改系统代理（权限不足）：{e}") from e
        global _ACTIVE_PROXY
        _ACTIVE_PROXY = dict(self.old_proxy)
        self.log(f"已开启系统代理 127.0.0.1:{self.port}")

    def seen(self):
        """抓包期间看到的微信请求条数（用于诊断代理/证书是否生效）。"""
        try:
            with open(self.debug, "r", encoding="utf-8", errors="ignore") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def tail(self, n=15):
        try:
            with open(self.debug, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            return [ln.rstrip() for ln in lines[-n:]]
        except OSError:
            return []

    def poll(self):
        """返回本次新抓到的所有信息（list[dict]），没有则空列表。"""
        if not os.path.exists(self.out):
            return []
        try:
            with open(self.out, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self.offset)
                chunk = f.read()
                self.offset = f.tell()
        except OSError:
            return []
        out = []
        for line in chunk.splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            sid = (obj.get("sid") or "").strip()
            if sid and re.match(r"^[A-Za-z0-9_\-]{30,64}$", sid):
                obj["sid"] = sid
                out.append(obj)
        return out

    def stop(self):
        global _ACTIVE_PROXY
        if self.old_proxy is not None:
            try:
                _write_proxy(self.old_proxy["enable"], self.old_proxy["server"],
                             self.old_proxy["override"])
                self.log("系统代理已还原")
            except Exception as e:  # noqa: BLE001
                self.log(f"还原代理失败：{e}")
            self.old_proxy = None
            _ACTIVE_PROXY = None
        if self.proc:
            pid = self.proc.pid
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    try:
                        self.proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
            self.proc = None
            self.log("抓包后端已停止")
