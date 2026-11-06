"""Shared validation and lossless configuration editing. No shell execution."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

import tomlkit


class ValidationError(ValueError):
    pass


class Conflict(ValidationError):
    pass


def port(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,5}", str(value)):
        raise ValidationError("端口必须是 1–65535 的整数")
    number = int(value)
    if not 1 <= number <= 65535:
        raise ValidationError("端口必须是 1–65535 的整数")
    return number


def address(value, *, local=False):
    if not isinstance(value, str):
        raise ValidationError("地址必须是字符串")
    match = re.fullmatch(r"\[([^\[\]]+)\]:([0-9]+)|([^:\s\[\]]+):([0-9]+)", value.strip())
    if not match:
        raise ValidationError("地址格式应为 IPv4:端口、域名:端口或 [IPv6]:端口")
    host, number = (match[1], match[2]) if match[1] else (match[3], match[4])
    try:
        ip = ipaddress.ip_address(host)
        if bool(match[1]) != (ip.version == 6):
            raise ValidationError("IPv6 地址必须放在方括号内")
        host = f"[{ip.compressed}]" if ip.version == 6 else str(ip)
    except ValueError as exc:
        if isinstance(exc, ValidationError):
            raise
        if local or match[1] or re.fullmatch(r"[0-9.]+", host):
            raise ValidationError("无效的 IP 地址") from exc
        if len(host) > 253 or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", x)
                                  for x in host.rstrip(".").split(".")):
            raise ValidationError("无效的远程域名") from exc
        host = host.lower().rstrip(".")
    return f"{host}:{port(number)}"


def protocols(endpoint, global_network=None):
    network = dict(global_network or {})
    network.update(endpoint.get("network", {}))
    return (set() if network.get("no_tcp", False) else {"tcp"}) | (
        {"udp"} if network.get("use_udp", False) else set())


def note(endpoint):
    for key in ("remark", "remarks", "note", "name"):
        if key in endpoint:
            return str(endpoint[key])
    if hasattr(endpoint, "as_string"):
        found = re.search(r"#\s*(?:备注|remark|note)\s*[:：]\s*([^\r\n]*)", endpoint.as_string(), re.I)
        if found:
            return found[1]
    return ""


def rule(data):
    if not isinstance(data, dict):
        raise ValidationError("规则必须是对象")
    mode = data.get("protocol", "tcp+udp")
    if mode not in ("tcp", "udp", "tcp+udp"):
        raise ValidationError("协议必须为 tcp、udp 或 tcp+udp")
    remark = data.get("note", "")
    if not isinstance(remark, str) or len(remark) > 512 or any(ord(x) < 32 or ord(x) == 127 for x in remark):
        raise ValidationError("备注最多 512 字符，不得含换行或控制字符")
    endpoint = {"listen": address(data.get("listen", ""), local=True),
                "remote": address(data.get("remote", "")), "remark": remark,
                "network": {"no_tcp": mode == "udp", "use_udp": mode != "tcp"}}
    return endpoint


def check_conflict(candidate, endpoints, network, exclude=None):
    # Conservative by port: avoids IPv4/IPv6 wildcard and dual-stack surprises.
    number = port(candidate["listen"].rsplit(":", 1)[1])
    for index, existing in enumerate(endpoints):
        if index == exclude:
            continue
        if port(str(existing.get("listen", "")).rsplit(":", 1)[-1]) == number:
            if protocols(candidate, network) & protocols(existing, network):
                same = (address(existing["listen"], local=True) == candidate["listen"] and
                        address(existing["remote"]) == candidate["remote"] and
                        protocols(existing, network) == protocols(candidate, network))
                raise Conflict("重复规则" if same else "监听端口及协议冲突")


@contextlib.contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stat = path.stat() if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=".ezrealm-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            if os.name != "nt":
                os.fchmod(handle.fileno(), stat.st_mode & 0o777 if stat else mode)
                if stat:
                    os.fchown(handle.fileno(), stat.st_uid, stat.st_gid)
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Store:
    def __init__(self, config, state):
        self.config = Path(config)
        self.state = Path(state)
        self.lock = self.state / "config.lock"

    def load(self):
        if self.config.is_symlink() or not self.config.is_file():
            raise ValidationError("配置文件不存在或是符号链接；请先在 SSH 中安装/选择配置")
        raw = self.config.read_bytes()
        if len(raw) > 4 * 1024 * 1024:
            raise ValidationError("配置文件超过 4 MiB")
        try:
            text = raw.decode("utf-8-sig")
            doc = json.loads(text) if self.config.suffix == ".json" else tomlkit.parse(text)
            if not isinstance(doc, dict) or not isinstance(doc.get("endpoints", []), list):
                raise ValueError("endpoints 必须是数组")
            for item in doc.get("endpoints", []):
                address(item["listen"], local=True)
                address(item["remote"])
                if not protocols(item, doc.get("network")):
                    raise ValueError("存在同时禁用 TCP/UDP 的规则")
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError(f"原配置无法安全编辑：{exc}；文件未修改") from exc
        return raw, doc

    def snapshot(self):
        with file_lock(self.lock):
            raw, doc = self.load()
            return self.view(raw, doc)

    def view(self, raw, doc):
        rows = []
        for index, endpoint in enumerate(doc.get("endpoints", []), 1):
            rows.append({"id": index, "listen": endpoint["listen"], "remote": endpoint["remote"],
                         "note": note(endpoint),
                         "protocol": "+".join(sorted(protocols(endpoint, doc.get("network"))))})
        return {"revision": hashlib.sha256(raw).hexdigest(), "rules": rows,
                "config": str(self.config)}

    def _save(self, old, doc):
        new = (json.dumps(doc, ensure_ascii=False, indent=2) + "\n" if self.config.suffix == ".json"
               else tomlkit.dumps(doc)).encode("utf-8")
        if new == old:
            return
        backup = self.state / "backups" / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}{self.config.suffix}"
        atomic_write(backup, old)
        # A byte-for-byte recovery copy, created before the first managed edit.
        if not (self.state / "applied").exists():
            atomic_write(self.state / "applied", old)
        atomic_write(self.config, new)

    def mutate(self, operation, data, revision=None):
        with file_lock(self.lock):
            raw, doc = self.load()
            if revision is not None and revision != hashlib.sha256(raw).hexdigest():
                raise Conflict("配置已被其他会话修改，请刷新后重试")
            endpoints = doc.get("endpoints")
            if endpoints is None:
                doc["endpoints"] = [] if self.config.suffix == ".json" else tomlkit.aot()
                endpoints = doc["endpoints"]
            report = None
            if operation == "import":
                report = self._import(data, endpoints, doc.get("network", {}))
            elif operation in ("add", "edit", "delete"):
                index = None
                if operation != "add":
                    ident = data.get("id")
                    if isinstance(ident, bool) or not isinstance(ident, int) or not 1 <= ident <= len(endpoints):
                        raise ValidationError("无效的规则序号")
                    index = ident - 1
                if operation == "delete":
                    del endpoints[index]
                else:
                    candidate = rule(data)
                    check_conflict(candidate, endpoints, doc.get("network", {}), index)
                    if operation == "add":
                        endpoints.append(candidate)
                    else:
                        # Keep transports, timeouts, load balancing and unknown fields.
                        existing = endpoints[index]
                        existing["listen"] = candidate["listen"]
                        existing["remote"] = candidate["remote"]
                        existing["remark"] = candidate["remark"]
                        if "network" not in existing:
                            existing["network"] = {}
                        existing["network"].update(candidate["network"])
            else:
                raise ValidationError("未知操作")
            # No-op/invalid imports must not rewrite or back up the configuration.
            if operation != "import" or report["success"]:
                self._save(raw, doc)
            result = self.view(*self.load())
            if report is not None:
                result["report"] = report
            return result

    def _import(self, text, endpoints, network):
        if not isinstance(text, str) or len(text.encode("utf-8")) > 1024 * 1024:
            raise ValidationError("导入内容最多 1 MiB")
        report = {"success": 0, "duplicate": 0, "failed": 0, "details": []}
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                # Exactly four fields; notes remain unchanged, including whitespace.
                fields = line.split("|")
                if len(fields) != 4 or not re.fullmatch(r"[1-9][0-9]*", fields[0].strip()):
                    raise ValidationError("应为 序号|本地端口|远程IP:端口|备注（备注不得含 |）")
                candidate = rule({"listen": f"0.0.0.0:{port(fields[1].strip())}",
                                  "remote": fields[2].strip(), "note": fields[3]})
                check_conflict(candidate, endpoints, network)
                endpoints.append(candidate)
                status, message = "success", "导入成功"
            except Conflict as exc:
                status, message = ("duplicate" if str(exc) == "重复规则" else "failed"), str(exc)
            except ValidationError as exc:
                status, message = "failed", str(exc)
            report[status] += 1
            report["details"].append({"line": number, "status": status, "message": message})
        return report

    def export(self, full=False):
        with file_lock(self.lock):
            raw, doc = self.load()
            if full:
                return raw.decode("utf-8-sig")
            lines = []
            for row in self.view(raw, doc)["rules"]:
                if "|" in row["note"] or "\n" in row["note"] or "\r" in row["note"]:
                    raise ValidationError("备注含分隔符/换行，请使用完整配置导出")
                lines.append(f'{row["id"]}|{row["listen"].rsplit(":", 1)[1]}|{row["remote"]}|{row["note"]}')
            return "\n".join(lines) + ("\n" if lines else "")
