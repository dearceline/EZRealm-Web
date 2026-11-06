"""Original Chinese 0–12 menu plus option 13."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys

from . import __version__
from .core import Store, ValidationError, atomic_write
from .linux import Controller, SETTINGS, STATE, install_realm, schedule, settings, store, systemctl, uninstall

MENU = '''
          欢迎使用Realm一键转发脚本
------------ Realm版本v2.7.0 ------------

---------------------
 1. 安装 Realm
---------------------
 2. 添加 Realm 转发规则
 3. 查看 Realm 转发规则
 4. 修改 Realm 转发规则
 5. 删除 Realm 转发规则
---------------------
 6. 启动 Realm 服务
 7. 停止 Realm 服务
 8. 重启 Realm 服务
---------------------
 9. 卸载 Realm
---------------------
 10. 定时重启任务
---------------------
 11. 导出转发规则
 12. 导入转发规则
 13. Web管理面板
---------------------
 0. 退出脚本
---------------------'''

IMPORT_PROMPT = '''
                Realm转发规则导入
--------------------------------------------------
请按照以下格式粘贴转发规则：
序号|本地端口|远程IP:端口|备注

例如：
1|8080|192.168.1.1:8000|一号服务器
2|8443|192.168.1.2:9000|二号服务器
3|9000|[2001:db8::1]:8080|IPv6服务器

--------------------------------------------------
请在下方粘贴规则（粘贴完成后按Ctrl+D结束输入）：'''


def display(data):
    print("序号|本地端口|远程IP:端口|备注")
    for row in data["rules"]:
        print(f'{row["id"]}|{row["listen"].rsplit(":", 1)[1]}|{row["remote"]}|{row["note"]}')
        print(f'    监听 {row["listen"]}  协议 {row["protocol"].upper()}')
    if not data["rules"]:
        print("暂无转发规则")


def report(result):
    data = result["report"]
    for row in data["details"]:
        print(f'第 {row["line"]} 行：{row["message"]}')
    print(f'导入完成：成功 {data["success"]}，重复 {data["duplicate"]}，失败 {data["failed"]}')
    print("规则已保存。使用菜单 8 应用配置；未覆盖已有规则。")


def prompt_rule(existing=None):
    old = existing or {"listen": "0.0.0.0:8080", "remote": "", "protocol": "tcp+udp", "note": ""}
    result = {}
    for key, label in (("listen", "监听地址"), ("remote", "远程IP:端口（IPv6 使用 [地址]:端口）"),
                       ("protocol", "协议 tcp / udp / tcp+udp"), ("note", "备注（输入 - 清空）")):
        value = input(f'{label} [{old[key]}]：')
        result[key] = ("" if value == "-" else value) if key == "note" else value.strip()
        if not value:
            result[key] = old[key]
    return result


def panel_menu():
    import grp
    print("\n1. 安装/启动 Web管理面板\n2. 停止 Web管理面板\n3. 设置登录账号和密码\n4. 查看面板状态及连接方法\n0. 返回")
    choice = input("请选择：").strip()
    auth_path = SETTINGS.with_name("auth.json")
    if choice == "3" or (choice == "1" and not auth_path.exists()):
        from .web import credentials
        username = input("设置用户名 [admin]：").strip() or "admin"
        password = getpass.getpass("设置密码（至少12字符，无默认密码）：")
        if getpass.getpass("再次输入密码：") != password:
            raise ValidationError("两次密码不一致")
        atomic_write(auth_path, json.dumps(credentials(username, password)).encode(), 0o640)
        os.chown(auth_path, 0, grp.getgrnam("ezrealm-web").gr_gid)
        os.chmod(auth_path, 0o640)
        if systemctl("is-active", "ezrealm-web.service", check=False).stdout.strip() == "active":
            systemctl("restart", "ezrealm-web.service")
        print("账号已设置；原有登录会话已失效。")
    if choice == "1":
        store().load()
        systemctl("enable", "--now", "ezrealm-broker.service")
        systemctl("enable", "--now", "ezrealm-web.service")
    elif choice == "2":
        systemctl("disable", "--now", "ezrealm-web.service")
        systemctl("disable", "--now", "ezrealm-broker.service")
        print("Web 面板及代理已停止；Realm 转发服务不受影响。")
    if choice in ("1", "4"):
        print("面板状态：" + systemctl("is-active", "ezrealm-web.service", check=False).stdout.strip())
        print("请在你的电脑上建立 SSH 隧道（替换服务器地址）：")
        print("ssh -N -L 8765:127.0.0.1:8765 root@服务器地址")
        print("浏览器访问 http://127.0.0.1:8765 （无需开放服务器 8765 端口）")


def run_menu():
    controller = Controller()
    while True:
        print(MENU)
        status = controller.status()
        print("\nRealm 状态：" + ("已安装" if status["enabled"] not in ("not-found", "") else "未安装"))
        print("Realm 转发状态：" + ("启用" if status["active"] == "active" else "停止"))
        try:
            choice = input("请选择一个选项[0-13]：").strip()
            if choice == "0":
                return
            if choice == "1":
                install_realm(controller.store.config)
            elif choice in ("2", "3", "4", "5"):
                snapshot = controller.store.snapshot()
                if choice == "2":
                    controller.store.mutate("add", prompt_rule(), snapshot["revision"])
                    print("添加成功。使用菜单 8 应用配置。")
                else:
                    display(snapshot)
                    if choice in ("4", "5"):
                        number = input("请输入规则序号：").strip()
                        if not number.isdecimal() or not 1 <= int(number) <= len(snapshot["rules"]):
                            raise ValidationError("无效序号")
                        ident = int(number)
                        if choice == "4":
                            data = prompt_rule(snapshot["rules"][ident - 1])
                            controller.store.mutate("edit", {**data, "id": ident}, snapshot["revision"])
                        elif input("确认删除？输入 y：").strip().lower() == "y":
                            controller.store.mutate("delete", {"id": ident}, snapshot["revision"])
                        print("操作完成。使用菜单 8 应用配置。")
            elif choice in ("6", "7", "8"):
                print(controller.service({"6": "start", "7": "stop", "8": "restart"}[choice]))
            elif choice == "9":
                print("将卸载本项目安装的 Realm 服务与二进制，保留配置、备份。其他脚本安装的文件只停用。")
                if input("输入 UNINSTALL 确认：") == "UNINSTALL":
                    uninstall()
            elif choice == "10":
                schedule(input("每日重启时间 HH:MM（输入 off 关闭本项目任务）：").strip())
            elif choice == "11":
                kind = input("1. 四列转发规则  2. 完整配置 [1]：").strip()
                full = kind == "2"
                print(controller.store.export(full=full), end="")
                print("四列格式不包含协议/监听IP/高级参数；完整迁移请导出完整配置。")
                target = input("保存路径（留空仅显示）：").strip()
                if target:
                    destination = Path(target).expanduser()
                    if destination.exists():
                        raise ValidationError("文件已存在，未覆盖")
                    # Exclusive creation; export must never overwrite the live config.
                    with destination.open("x", encoding="utf-8", newline="\n") as handle:
                        handle.write(controller.store.export(full=full))
                    os.chmod(destination, 0o600)
            elif choice == "12":
                print(IMPORT_PROMPT, flush=True)
                text = sys.stdin.read(1024 * 1024 + 1)
                report(controller.store.mutate("import", text))
            elif choice == "13":
                panel_menu()
            else:
                print("请输入 0–13 之间的菜单编号")
        except EOFError:
            return
        except (ValidationError, OSError, ValueError) as exc:
            print("错误：" + str(exc))
        except KeyboardInterrupt:
            print("\n操作已取消")


def main():
    parser = argparse.ArgumentParser(description="EZRealm-Web 中文管理脚本")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", help="首次初始化时明确选择已有 TOML/JSON 配置")
    parser.add_argument("--import", dest="import_file", help="导入四列规则，- 表示标准输入直到 EOF")
    parser.add_argument("--export", dest="export_file", help="导出到新文件，- 表示标准输出")
    parser.add_argument("--full", action="store_true", help="导出完整配置")
    args = parser.parse_args()
    try:
        from .setup import initialize
        initialize(args.config)
        if args.import_file:
            text = sys.stdin.read(1024 * 1024 + 1) if args.import_file == "-" else Path(args.import_file).read_text("utf-8-sig")
            report(store().mutate("import", text))
        elif args.export_file:
            content = store().export(full=args.full)
            if args.export_file == "-":
                print(content, end="")
            else:
                with open(args.export_file, "x", encoding="utf-8", newline="\n") as handle:
                    handle.write(content)
                os.chmod(args.export_file, 0o600)
        else:
            run_menu()
    except (ValidationError, OSError, ValueError) as exc:
        print("错误：" + str(exc), file=sys.stderr)
        raise SystemExit(1)
