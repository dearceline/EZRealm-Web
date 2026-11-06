# 验证说明

本项目将可重复测试放在 `tests/`，以实际输出作为通过依据。

1. 配置测试：四列导入、IPv6 归一化、中文/首尾空格备注、重复/端口冲突、无效行、无成功行不改写、旧配置字段与注释保留、备份、JSON、导出往返、乐观锁。
2. HTTP 测试：登录/退出、过期会话、CSRF、Origin、DNS rebinding Host 检查、CSP 与其他响应头、登录限流、禁止公网绑定。
3. 服务逻辑：已有服务的配置路径识别、固定服务动作、重启失败后的配置恢复。
4. 真实转发：设置 `REALM_BINARY` 后使用独立临时配置启动官方 v2.7.0，实际传输 TCP 和 UDP 数据，覆盖 IPv4、IPv6、TCP-only、UDP-only 和 TOML/JSON。验证 `remark` 备注字段不会阻止官方内核加载配置。
5. Linux PTY：进入选项 12、粘贴多行、发送真实终端 Ctrl+D、检查统计，再进入菜单 3，验证 EOF 没有关闭菜单输入。
6. 一次性 Linux 安装：执行实际 `realm.sh --import -`，检查旧配置字节备份、原有 stdin 保留、SHA256 校验安装、systemd 启动、非 root Web 用户、登录、Unix 代理修改、应用配置以及重跑安装不改写现有配置。
7. 缺少 Python：在不包含 Python 的官方 `debian:12-slim` 容器中运行实际 `realm.sh --version`，验证从 Debian 软件源自动安装依赖；另用隔离的 PATH 和模拟 apt 执行真实前置流程，验证已有合适解释器时不调用 apt、识别版本化 Python、软件源更新/安装失败时停止，以及安装过程不消耗规则输入。模拟测试不修改真实系统软件包。

本地执行：

```bash
python -m pip install -e .
REALM_BINARY=/path/to/realm python -m unittest discover -s tests -v
python tools/build_bundle.py
bash -n realm.sh
node --check ezrealm/static/app.js
```

`systemd_smoke.py` 仅允许在 `GITHUB_ACTIONS=true` 的一次性 runner 中运行，它会安装测试服务，不应在生产服务器执行。未提供实际二进制、没有 IPv6 或非 Linux 时，相应测试会明确显示 skipped，不代表该测试通过。

桌面开发环境已使用官方 v2.7.0 Windows 程序验证真实 TCP/UDP 和 IPv6；Linux 与 systemd 的验证结果请查看仓库对应提交的 Actions。测试不代表已经在用户 VPS 上部署，也不代表对未知 Gist 原脚本的逐项兼容审计。
