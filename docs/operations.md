# 运维与恢复

## 文件与服务

| 位置 | 用途 |
| --- | --- |
| `/opt/ezrealm-web/releases/<载荷摘要>/` | 只读应用代码与 tomlkit，root 所有 |
| `/usr/local/bin/ezrealm` | SSH 管理入口 |
| `/usr/local/bin/realm` | 仅新装时创建的官方 v2.7.0 转发程序 |
| `/etc/ezrealm-web/manager.json` | 选中的配置路径与安装记录，root 可读写 |
| `/etc/ezrealm-web/auth.json` | 密码哈希，root/面板组读取 |
| `/var/lib/ezrealm-web/backups/` | 每次修改前的原配置、失败应用记录 |
| `/var/lib/ezrealm-web/applied` | 上次成功启动/重启时的配置快照 |
| `/run/ezrealm-web/control.sock` | 本机专用配置代理通道 |

`realm.service` 是转发服务，已有服务保持原定义。`ezrealm-broker.service` 是配置代理，`ezrealm-web.service` 是低权限 Web 界面。`ezrealm-restart.timer/service` 是本项目独立每日任务。

## 检查状态

```bash
systemctl status realm ezrealm-broker ezrealm-web --no-pager
journalctl -u realm -u ezrealm-broker -u ezrealm-web -n 100 --no-pager
systemctl list-timers ezrealm-restart.timer
```

新创建的 Realm 服务使用 `Restart=on-failure`，默认配置同时允许 TCP/UDP。新装不会自动启动转发；从菜单 6 启动。已有服务使用原来的启动策略。

## 恢复配置

先停止 Web 面板和 Realm，防止恢复过程中有并发修改：

```bash
systemctl stop ezrealm-web ezrealm-broker realm
cat /etc/ezrealm-web/manager.json
ls -lt /var/lib/ezrealm-web/backups/
```

确认 `manager.json` 中的实际配置路径，将要恢复的备份复制到该文件。恢复前另存当前文件，不要直接复制到一个未经核实的猜测路径。保持原文件的所有者与读写权限；TOML 对应 TOML，JSON 对应 JSON。

恢复后，将 `/var/lib/ezrealm-web/applied` 同步为该已知可用配置，再启动 Realm 并查看日志。成功后按需启动代理和面板。每次备份不会自动删除；可在离机备份之后人工清理不再需要的历史文件。

应用失败时自动恢复的是上一次被本管理器确认启动成功的配置；首次编辑以编辑前文件建立基线。该机制不能得知外部脚本曾经加载到 Realm 内存中的旧内容。接管前请确认磁盘配置与现有服务一致；管理期间避免其他脚本同时改写配置。

## 升级

重新下载新版本的 `realm.sh` 并运行。应用写入新的摘要目录；配置、账号、备份、已有 Realm 二进制保留。正在运行的面板/代理重启以加载新代码并使会话失效，Realm 本身不会因为管理程序升级而重启。

若首次初始化因配置识别失败退出，使用 `--config /绝对路径/config.toml` 明确选择。已有 `manager.json` 时指定不同路径会拒绝；需要切换时先停用管理服务、备份配置和状态，root 手动更新路径并重新建立对应恢复基线，然后重新运行脚本以更新服务的路径权限。

## 密码与端口

菜单 13 → 3 可重设用户名/密码，随后面板重启，旧会话失效。管理员遗失密码时使用 SSH root 重新设置，不提供 Web 找回密码入口。

面板服务固定回环端口 8765，通常 SSH 本地也使用 8765。如果电脑端口被占用，可用 `-L 18765:127.0.0.1:8765`，浏览器访问 `http://127.0.0.1:18765`。不要打开 VPS 防火墙上的 8765，不要通过服务器公网地址访问。转发规则使用的 TCP/UDP 端口则由管理员根据实际需要配置防火墙；脚本不会自动修改防火墙。

## 卸载范围

菜单 9 停止管理面板和代理，关闭本项目的重启 timer，停止并禁用 Realm。对于本项目安装且哈希匹配的二进制，删除它及本项目生成的 Realm unit；配置和管理程序保留。对于原有安装，只停用服务，不删除原脚本管理的文件。需要恢复时，新装使用菜单 1，原有安装使用菜单 6。
