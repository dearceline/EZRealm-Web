"""Install immutable application files and service definitions; preserve user data."""
import grp
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .core import Store, ValidationError, atomic_write
from .linux import SETTINGS, STATE, discovery, systemctl


def initialize(config=None):
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValidationError("安装与 SSH 管理需要 Linux root 权限")
    if sys.version_info < (3, 11):
        raise ValidationError("需要 Python 3.11 或更高版本（推荐 Debian 12 / Ubuntu 24.04）")
    if not Path("/run/systemd/system").is_dir():
        raise ValidationError("需要使用 systemd 的 Linux 系统")
    SETTINGS.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if SETTINGS.exists():
        selected = json.loads(SETTINGS.read_text("utf-8"))["config"]
        if config is not None and str(Path(config).absolute()) != selected:
            raise ValidationError("已选择其他配置；请先备份并手动更新 /etc/ezrealm-web/manager.json")
    else:
        selected = config or discovery()
        path = Path(selected)
        if not path.is_absolute() or any(x in str(path) for x in ('"', '\n', '\r', '\\', '%')):
            raise ValidationError("配置必须是无特殊字符的绝对路径")
        if path.exists():
            Store(path, STATE).load()  # Validate before adopting; never rewrite.
        atomic_write(SETTINGS, (json.dumps({"config": str(path)}, ensure_ascii=False) + "\n").encode())
    try:
        gid = grp.getgrnam("ezrealm-web").gr_gid
    except KeyError:
        subprocess.run(["/usr/sbin/groupadd", "--system", "ezrealm-web"], check=True)
        gid = grp.getgrnam("ezrealm-web").gr_gid
    import pwd
    try:
        account = pwd.getpwnam("ezrealm-web")
        if account.pw_uid == 0 or account.pw_gid != gid:
            raise ValidationError("已有 ezrealm-web 账号权限异常，请检查后重试")
    except KeyError:
        subprocess.run(["/usr/sbin/useradd", "--system", "--gid", "ezrealm-web", "--no-create-home",
                        "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin", "ezrealm-web"], check=True)
    os.chown(SETTINGS.parent, 0, gid)
    os.chmod(SETTINGS.parent, 0o750)
    entry = Path(__file__).resolve().parent.parent / "entry.py"
    python = Path(sys.executable).resolve()
    units = {
        "ezrealm-broker.service": f'''[Unit]
Description=EZRealm restricted configuration broker
After=network.target
[Service]
Type=simple
ExecStart={python} {entry} broker
User=root
Group=ezrealm-web
RuntimeDirectory=ezrealm-web
RuntimeDirectoryMode=0750
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
ReadWritePaths={STATE} -{Path(selected).parent}
RestrictAddressFamilies=AF_UNIX
Restart=on-failure
[Install]
WantedBy=multi-user.target
''',
        "ezrealm-web.service": f'''[Unit]
Description=EZRealm loopback management panel
Requires=ezrealm-broker.service
After=ezrealm-broker.service
[Service]
Type=simple
ExecStart={python} {entry} web
User=ezrealm-web
Group=ezrealm-web
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictAddressFamilies=AF_UNIX AF_INET
CapabilityBoundingSet=
Restart=on-failure
[Install]
WantedBy=multi-user.target
'''}
    for name, content in units.items():
        atomic_write(Path("/etc/systemd/system") / name, content.encode(), 0o644)
    launcher = f'#!/bin/sh\nexec "{python}" "{entry}" cli "$@"\n'
    atomic_write(Path("/usr/local/bin/ezrealm"), launcher.encode(), 0o755)
    systemctl("daemon-reload")
    # A code upgrade invalidates sessions and reconnects the broker to the new code.
    if systemctl("is-active", "ezrealm-web.service", check=False).stdout.strip() == "active":
        systemctl("restart", "ezrealm-broker.service")
        systemctl("restart", "ezrealm-web.service")
    return selected
