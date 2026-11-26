# 在远端 Antigravity 中使用 Mac

如果 agy 已经在开发机上运行，只想让它调用 Mac 上的几个操作，安装 **MCP 工具桥**。如果要从 Paseo 打开当前 Mac 项目，让远端 agy 直接编辑文件、使用图片附件，安装 **工作区启动器**。两者可以单独使用，SSH 连接都从 Mac 发起。下文用 `devbox` 代表你的 SSH 别名。

## 给已有 agy 添加 Mac 工具

在 Mac 上进入本仓库，选一个真实存在的项目目录作为默认工作目录。例子用固定工具清单，只发布 `mac_info` 和 `project_path`，分别运行 `uname -sm` 和 `pwd`。工具名和参数由 Mac 管理。开发机需要 Python 3.9+，SSH 服务需要允许绑定在 loopback 的反向端口转发。

```sh
PROJECT_DIR="$PWD"
AGY_DIR="$HOME/.local/share/paseo-codex-remote-bridge/antigravity-remote"
python3 scripts/install.py --agent antigravity --ssh-host devbox \
  --profile antigravity-remote --port 18740 --default-cwd "$PROJECT_DIR" \
  --actions-file examples/mac-actions.json --install-dir "$AGY_DIR"
```

如果是自己的专用远端账号，且需要通用命令和文件读写，创建实例时可以省略 `--actions-file`。通用工具按 Mac 登录用户的文件权限运行，`--default-cwd` 只设置初始目录。共享远端账号使用固定工具清单。为第二台 Mac 选择另一个端口、profile 和 `--mcp-server-name`。

把配置合并程序复制到开发机，再通过 SSH 标准输入发送含凭证的配置片段。程序保留已登记的其它 MCP 服务。遇到同名条目会退出，先确认旧实例可以替换，再在最后一条命令的远端 `--merge-config` 参数之后加 `--replace`。

```sh
ssh devbox 'umask 077; mkdir -p "$HOME/.local/share/paseo-mac-tools"'
scp "$AGY_DIR/antigravity_mcp.py" devbox:.local/share/paseo-mac-tools/
ssh devbox 'python3 "$HOME/.local/share/paseo-mac-tools/antigravity_mcp.py" --merge-config "$HOME/.gemini/config/mcp_config.json"' \
  < "$AGY_DIR/remote-mcp_config.json"
```

在 Mac 上启用两个生成的服务。首次安装时选择两端都空闲的端口。将开发机的 SSH `GatewayPorts` 保持为 `no` 或 `clientspecified`，以便隧道监听 `127.0.0.1`。

```sh
DOMAIN="gui/$(id -u)"
PREFIX="local.paseo-codex-remote-bridge.antigravity-remote"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCH_DIR"
for service in mcp-server ssh-tunnel; do
  cp -n "$AGY_DIR/$service.plist" "$LAUNCH_DIR/$PREFIX.$service.plist" &&
    launchctl bootstrap "$DOMAIN" "$LAUNCH_DIR/$PREFIX.$service.plist" || break
done
launchctl print "$DOMAIN/$PREFIX.mcp-server"
launchctl print "$DOMAIN/$PREFIX.ssh-tunnel"
ssh devbox 'agy mcp list'
```

用开发机上实际安装的 agy 路径运行最后一条命令。新开一个 agy 会话，请它调用 `mac_info` 与 `project_path`。返回内容应包含 `Darwin` 和所选 Mac 项目的绝对路径。通用模式用 `exec` 工具执行 `uname -sm && pwd`。工具桥沿用现有 agy 启动方式。

### 更新 MCP 工具桥

会话空闲后在 Mac 更新实例并重新加载这个实例的 MCP 进程。

```sh
python3 scripts/install.py --update --install-dir "$AGY_DIR"
launchctl kickstart -k "$DOMAIN/$PREFIX.mcp-server"
```

更新保留 token、plist 和 `actions.json`。停用时先结束依赖它的会话，用 `launchctl bootout "$DOMAIN/$PREFIX.ssh-tunnel"` 和 `launchctl bootout "$DOMAIN/$PREFIX.mcp-server"` 卸载两个服务，再移走这两个实例的 LaunchAgent plist，并从开发机 MCP 配置删除对应的服务名。

## 用远端 agy 打开 Mac 工作区

Mac 需要 `/usr/bin/sandbox-exec` 和 `/usr/libexec/sftp-server`。Linux 开发机需要 Python 3.9+、已登录的 agy、可用的 `/dev/fuse`、支持通过标准输入输出连接的 SSHFS（`-o passive` 或 `-o slave`）、`fusermount3` 和能为该用户建立命名空间的 `bwrap`。本机 SFTP 只提供当前项目，开发机通过 Mac 发起的 SSH 通道挂载它，每个会话有自己的挂载点。Mac 的项目文件访问限于选中的工作区。Linux 上的 agy 仍可访问自己在开发机的账号目录。

在 Mac 生成一个独立的启动器目录。远端二进制在其它位置时用 `--remote-agy-bin` 和 `--remote-sshfs-bin` 指定。本机有旧 agy 会话需要续接时，使用 `--local-agy-bin` 指定本机 CLI。

```sh
DIR="$HOME/.local/share/paseo-agy-workspace"
python3 scripts/install_agy_workspace.py --ssh-host devbox --install-dir "$DIR"
```

把生成的 Linux helper 安装到远端。先写入临时文件，再替换同一目录下的旧版本。首次安装和以后更新使用同一条命令。

```sh
ssh devbox 'set -eu; umask 077; d="$HOME/.local/share/paseo-agy-workspace"; mkdir -p "$d"; t=$(mktemp "$d/.upload.XXXXXX"); trap '\''rm -f "$t"'\'' EXIT; cat > "$t"; mv "$t" "$d/remote_workspace.py"' \
  < "$DIR/remote_workspace.py"
"$DIR/agy-workspace" --bridge-check
"$DIR/agy-workspace" --version
```

先在远端以将要运行会话的 SSH 用户确认 agy 已经登录。Mac 上保留 Paseo 原有 Antigravity provider 的设置，仅把 `$DIR/provider.json` 中的 `agents.providers.antigravity.command` 合入 `~/.paseo/config.json`。这一步会改变以后启动 Antigravity 的命令，保存原值以便恢复。然后运行 `paseo reload` 并从一个具体的 Mac 项目目录新建 Antigravity 会话。

让它读一个项目里的文件，写一个临时文件并在 Mac 核对内容，再上传一张带文字的测试图，要求直接读图。项目内的读写和附件都成功后，结束会话。续接已在 Mac 保存的旧原生会话需要本机 agy；新远端会话使用开发机账号。用完可恢复原 provider 的 `command` 并再次 `paseo reload`。

### 更新工作区启动器

```sh
python3 scripts/install_agy_workspace.py --update --install-dir "$DIR"
```

再次执行上面的 helper 上传和 `--bridge-check`，然后用新会话验收。这个启动器不需要注册常驻 launchd 服务。
