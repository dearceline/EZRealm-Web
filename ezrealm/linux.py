"""Fixed-purpose Linux service operations and conservative legacy discovery."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request

from .core import Store, ValidationError, atomic_write, file_lock

SETTINGS = Path("/etc/ezrealm-web/manager.json")
STATE = Path("/var/lib/ezrealm-web")
SOCKET = "/run/ezrealm-web/control.sock"


def systemctl(*args, check=True):
    try:
        process = subprocess.run(["/usr/bin/systemctl", *args], capture_output=True,
                                 text=True, timeout=40)
    except subprocess.TimeoutExpired as exc:
        raise ValidationError("systemd 操作超时，请检查服务日志") from exc
    if check and process.returncode:
        raise ValidationError("systemd 操作失败：" + process.stderr.strip()[:500])
    return process


def settings():
    if not SETTINGS.is_file():
        raise ValidationError("请先以 root 运行 realm.sh 完成初始化")
    return json.loads(SETTINGS.read_text("utf-8"))


def store():
    return Store(settings()["config"], STATE)


def discovery():
    """Never guess a service's config from a different installed file."""
    loaded = systemctl("show", "realm.service", "-p", "LoadState", "--value", check=False).stdout.strip()
    if loaded == "loaded":
        command = systemctl("show", "realm.service", "-p", "ExecStart", "--value").stdout
        match = re.search(r"argv\[\]=(.*?)\s*;", command)
        if not match:
            raise ValidationError("现有 realm.service 的 ExecStart 无法识别，请手动使用 --config 指定实际配置")
        argv = shlex.split(match[1])
        paths = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg in ("-c", "--config")]
        paths += [arg.split("=", 1)[1] for arg in argv if arg.startswith("--config=")]
        if len(paths) != 1 or not Path(paths[0]).is_absolute() or not Path(paths[0]).is_file():
            raise ValidationError("现有服务未使用单个绝对路径配置文件；请手动迁移，不会替换服务")
        return paths[0]
    candidates = [p for p in ("/etc/realm/config.toml", "/etc/realm/config.json",
                  "/usr/local/realm/config.toml", "/usr/local/realm/config.json",
                  "/opt/realm/config.toml", "/root/realm/config.toml") if Path(p).is_file()]
    if len(candidates) > 1:
        raise ValidationError("发现多个配置，请通过 --config 明确选择：" + ", ".join(candidates))
    return candidates[0] if candidates else "/etc/realm/config.toml"


class Controller:
    def __init__(self, data=None):
        self.store = data or store()

    def status(self):
        return {"active": systemctl("is-active", "realm.service", check=False).stdout.strip(),
                "enabled": systemctl("is-enabled", "realm.service", check=False).stdout.strip()}

    def service(self, action):
        if action not in ("start", "stop", "restart"):
            raise ValidationError("无效的服务操作")
        with file_lock(self.store.lock):
            if action == "stop":
                systemctl("stop", "realm.service")
                return self.status()
            raw, _ = self.store.load()
            checkpoint = self.store.state / "applied"
            previous = checkpoint.read_bytes() if checkpoint.exists() else raw
            try:
                systemctl(action, "realm.service")
                time.sleep(1)
                if self.status()["active"] != "active":
                    raise ValidationError("Realm 未能保持运行")
            except ValidationError as exc:
                atomic_write(self.store.state / "backups" / f"failed-{time.time_ns()}{self.store.config.suffix}", raw)
                atomic_write(self.store.config, previous)
                recovered = systemctl("restart", "realm.service", check=False).returncode == 0
                raise ValidationError(f"应用失败，已恢复上次配置；恢复服务{'成功' if recovered else '失败，请检查日志'}：{exc}") from exc
            atomic_write(checkpoint, raw)
            return self.status()

    def dispatch(self, request):
        if not isinstance(request, dict):
            raise ValidationError("请求必须是对象")
        action = request.get("action")
        if action == "list":
            return {**self.store.snapshot(), "service": self.status()}
        if action in ("add", "edit", "delete", "import"):
            if not isinstance(request.get("revision"), str):
                raise ValidationError("缺少配置版本，请刷新")
            return self.store.mutate(action, request.get("data"), request["revision"])
        if action == "service":
            return self.service(request.get("data"))
        if action == "export":
            return {"text": self.store.export(full=bool(request.get("full")))}
        raise ValidationError("不允许的操作")


def download(url, path, limit):
    with urllib.request.urlopen(url, timeout=60) as response, open(path, "wb") as handle:
        count = 0
        while chunk := response.read(65536):
            count += len(chunk)
            if count > limit:
                raise ValidationError("下载文件超出预期限额")
            handle.write(chunk)


def install_realm(config):
    loaded = systemctl("show", "realm.service", "-p", "LoadState", "--value", check=False).stdout.strip()
    if loaded == "loaded":
        print("检测到已有 realm.service，保留服务、二进制及配置。")
        return
    arch = {"x86_64": "x86_64-unknown-linux-musl", "aarch64": "aarch64-unknown-linux-musl"}.get(platform.machine())
    if not arch:
        raise ValidationError("自动安装仅支持 Linux x86_64 / aarch64")
    asset = "realm-" + arch
    base = "https://github.com/zhboner/realm/releases/download/v2.7.0/"
    with tempfile.TemporaryDirectory(prefix="ezrealm-download-") as temp:
        archive = Path(temp) / (asset + ".tar.gz")
        checksum = Path(temp) / (asset + ".sha256")
        download(base + archive.name, archive, 100 * 1024 * 1024)
        download(base + checksum.name, checksum, 4096)
        expected = checksum.read_text().split()[0]
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected) or hashlib.sha256(archive.read_bytes()).hexdigest() != expected.lower():
            raise ValidationError("官方 SHA256 校验失败，未安装")
        with tarfile.open(archive) as bundle:
            members = [m for m in bundle.getmembers() if m.isfile() and Path(m.name).name == "realm"]
            if len(members) != 1 or members[0].size > 100 * 1024 * 1024:
                raise ValidationError("Realm 发布包结构异常")
            binary = bundle.extractfile(members[0]).read()
        destination = Path("/usr/local/bin/realm")
        if destination.exists():
            raise ValidationError("/usr/local/bin/realm 已存在，未覆盖；请先核对已有安装")
        atomic_write(destination, binary, 0o755)
    if not config.exists():
        atomic_write(config, b'endpoints = []\n\n[network]\nno_tcp = false\nuse_udp = true\n')
    Store(config, STATE).load()
    atomic_write(Path("/etc/systemd/system/realm.service"), f'''# Managed by EZRealm-Web
[Unit]
Description=Realm TCP/UDP forwarding
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=/usr/local/bin/realm -c "{config}"
Restart=on-failure
RestartSec=3
LimitNOFILE=1048576
NoNewPrivileges=true
ProtectHome=read-only
ProtectSystem=strict
PrivateTmp=true
[Install]
WantedBy=multi-user.target
'''.encode(), 0o644)
    systemctl("daemon-reload")
    systemctl("enable", "realm.service")
    manager = settings()
    manager["realm_binary_sha256"] = hashlib.sha256(binary).hexdigest()
    atomic_write(SETTINGS, json.dumps(manager).encode())
    print("Realm v2.7.0 已安装，使用菜单 6 启动。")


def uninstall():
    """Remove only files installed by this project; keep all forwarding data."""
    systemctl("disable", "--now", "ezrealm-web.service", check=False)
    systemctl("disable", "--now", "ezrealm-broker.service", check=False)
    schedule("off")
    systemctl("disable", "--now", "realm.service")
    unit = Path("/etc/systemd/system/realm.service")
    binary = Path("/usr/local/bin/realm")
    if not unit.is_file() or not unit.read_text().startswith("# Managed by EZRealm-Web\n"):
        print("已有 Realm 服务已停止并禁用；原安装文件由其他脚本管理，已保留。")
        return
    expected = settings().get("realm_binary_sha256")
    if not binary.is_file() or hashlib.sha256(binary.read_bytes()).hexdigest() != expected:
        raise ValidationError("服务已停止，但 Realm 二进制与安装记录不符，未删除任何文件")
    atomic_write(STATE / "backups" / f"uninstalled-{time.time_ns()}.service", unit.read_bytes())
    unit.unlink()
    binary.unlink()
    systemctl("daemon-reload")
    print("Realm 服务和本项目安装的二进制已卸载；配置、备份和管理脚本已保留。菜单 1 可重新安装。")


def schedule(value):
    timer = Path("/etc/systemd/system/ezrealm-restart.timer")
    if value == "off":
        systemctl("disable", "--now", "ezrealm-restart.timer", check=False)
        print("已关闭本项目的定时重启任务")
        return
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValidationError("时间格式必须为 HH:MM，例如 04:00")
    atomic_write(timer, f'''[Unit]
Description=Daily Realm restart
[Timer]
OnCalendar=*-*-* {value}:00
Persistent=false
[Install]
WantedBy=timers.target
'''.encode(), 0o644)
    entry = Path(__file__).resolve().parent.parent / "entry.py"
    import sys
    atomic_write(Path("/etc/systemd/system/ezrealm-restart.service"), f'''[Unit]
Description=Scheduled Realm restart (only if running)
[Service]
Type=oneshot
ExecStart={Path(sys.executable).resolve()} {entry} scheduled
'''.encode(), 0o644)
    systemctl("daemon-reload")
    systemctl("enable", "--now", "ezrealm-restart.timer")
    print("已设置每日 " + value + " 重启（服务器本地时区）；原有 cron 任务未改动。")
