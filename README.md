# Paseo 远程账号与工具桥

在 Mac 上使用 Paseo，模型请求从已登录的 SSH 开发机发出。按你希望远端会话怎样访问 Mac，选择下面的一种接法。安装器只生成实例文件，接入步骤由你在自己的两端执行。

| 你要做什么 | 怎么接 | 从哪里开始 |
| --- | --- | --- |
| 用开发机的 Codex 账号处理 Mac 项目和图片 | 远端运行 Codex app-server，本机运行 exec-server，通过反向 SSH 隧道连接 | [Codex 安装与验收](docs/setup.md) |
| 用开发机的 Claude Code 账号处理 Mac 项目 | 远端运行 Claude，本地工具调用通过同一条 SSH 转回 Mac | [安装 Claude](#安装-claude) |
| 让已有的 Antigravity 会话调用 Mac 上指定的命令 | Mac 提供 MCP 工具，远端 agy 通过反向隧道调用 | [给已有 agy 添加 Mac 工具](docs/antigravity.md#给已有-agy-添加-mac-工具) |
| 从 Paseo 启动远端 Antigravity，并直接编辑当前 Mac 项目 | 每个会话单独挂载选中的项目，支持 Paseo 图片附件 | [用远端 agy 打开 Mac 工作区](docs/antigravity.md#用远端-agy-打开-mac-工作区) |

如果你还需要从远端 Codex 调用 **Paseo 自身的 MCP 工具**，或希望同一个聊天在额度耗尽后切换账号，见[宿主与插件兼容说明](docs/paseo-integrations.md)。

## 开始前

Mac 需要 Python 3.9+、OpenSSH 和 Paseo。开发机需要对应的 CLI 和已登录的账号。在 Mac 上把开发机配置成自己的 SSH 别名。以下示例使用 `devbox`。先确认无交互连接成功，再从仓库根目录执行安装命令。

```sh
python3 --version
ssh -T -o BatchMode=yes devbox 'echo ok'
```

每个实例用独立的安装目录和 profile。源码可以分享。实例目录里的 token、`bridge.json`、provider 片段、远端环境配置、真实会话记录和 SSH 凭据只保存在自己的机器上。

## 安装 Claude

这里用同一个 SSH 用户下的第二个账号 `claude-work` 举例。先在开发机登录并确认显示的是预期账号。下面两条命令里的 `CLAUDE_CONFIG_DIR` 都在开发机上展开。

```sh
ssh -t devbox 'CLAUDE_CONFIG_DIR="$HOME/.claude-work" "$HOME/.local/bin/claude" auth login'
ssh -T devbox 'CLAUDE_CONFIG_DIR="$HOME/.claude-work" "$HOME/.local/bin/claude" auth status'
```

在 Mac 上生成实例。把 CLI 路径换成各自机器的实际路径。

```sh
CLAUDE_DIR="$HOME/.local/share/paseo-codex-remote-bridge/claude-work"
python3 scripts/install.py --agent claude --ssh-host devbox --profile claude-work \
  --remote-claude-bin '~/.local/bin/claude' --remote-claude-config-dir '~/.claude-work' \
  --local-claude-bin "$HOME/.local/bin/claude" --install-dir "$CLAUDE_DIR"
"$CLAUDE_DIR/claude-remote" auth status
```

启动器的检查结果应与远端直接查询的账号一致。把生成的 `provider.json` 中的 `agents.providers.claude-work` 合并到 Mac 的 `~/.paseo/config.json`。保留片段中的 `env.CLAUDE_CONFIG_DIR`，它指向 **Mac 安装目录**，供 Paseo 读取同步回来的会话历史。Claude 使用一条 SSH 连接转发工具调用，无须注册 launchd 服务。

```sh
paseo reload
paseo provider models claude-work --json
```

在 Mac 项目中用 `claude-work` 开一个聊天，让它读一个只有这个项目才有的文件，核对内容。首轮给它一个临时标记。用 `paseo ls` 找到这个测试会话的 ID，待它空闲后运行 `paseo agent reload <测试会话ID>`，再让它复述标记，核对旧消息和新回复。该命令只重载所选会话的进程。模型列表只检查 provider 可见性。

使用默认远端登录时可以省略 `--remote-claude-config-dir`。同一 SSH 主机再加一个账号时，换远端配置目录、本机 profile 和安装目录。会话工作目录默认在所选远端配置目录的 `paseo-mac-workspaces` 下。`--remote-session-root` 可单独改变工作目录，保存登录与读取历史仍使用所选账号的配置目录。这个桥在 Mac 执行 Bash、Read、Write、Edit、NotebookEdit 和 TaskStop。

## 更新与验证

从本仓库运行离线回归，随后更新指定实例。Codex、Claude 和 Antigravity MCP 工具桥都使用以下命令，把 `INSTANCE_DIR` 设为原安装目录。工作区启动器有自己的[更新步骤](docs/antigravity.md#更新工作区启动器)。

```sh
python3 -m unittest discover -s tests -v
python3 scripts/install.py --update --install-dir "$INSTANCE_DIR"
```

更新保留实例配置、连接凭证和固定工具清单。等原会话空闲后重连，新的进程才会使用新代码。Antigravity MCP 的常驻进程需要按[服务更新步骤](docs/antigravity.md#更新-mcp-工具桥)重新加载。更换 SSH 主机或账号时，新建实例并单独验收。

离线测试在临时目录和测试进程中检查安装、协议和账号隔离。实际接入还需在自己的 Paseo 会话中验证项目文件、附件或所选工具。Codex 的图片转换覆盖 app-server 的 `turn/start` 和 `turn/steer`，支持 PNG、JPEG、GIF 和 WebP。直接调用 `codex exec --image` 走原 CLI 路径。

这是独立集成项目。协议细节见 [Codex app-server 文档](https://developers.openai.com/codex/app-server/)。
