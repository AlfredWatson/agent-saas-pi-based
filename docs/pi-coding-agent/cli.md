# pi-coding-agent CLI 命令参考

本文基于本机已安装的 `@earendil-works/pi-coding-agent` **0.82.1** 的 `pi --help` 及各子命令的 `--help` 输出整理。以下是该版本内置的全部子命令；扩展可以额外注册参数（主帮助明确以 `--plan` 为例），因此扩展安装和启用情况不同，实际可用参数也可能不同。

## 基本调用

```text
pi [全局选项] [@文件 ...] [消息 ...]
```

- 不带子命令时，`pi` 启动交互式编码助手；可附带一个或多个初始消息。
- `@文件` 会把文件作为初始消息上下文传入，例如 `pi @prompt.md "审查这个需求"`。
- `-p` / `--print` 使其非交互执行：处理消息后退出。
- `-h` / `--help` 显示帮助；`-v` / `--version` 显示版本。

## 内置子命令

| 命令 | 用法 | 说明 |
| --- | --- | --- |
| `install` | `pi install <source> [-l]` | 安装扩展包，并将来源写入设置。`source` 可为 npm 来源、Git 来源、HTTPS/SSH 仓库 URL 或本地路径。`-l` / `--local` 写入项目设置 `.pi/settings.json`；不加时使用全局设置。 |
| `remove` | `pi remove <source> [-l]` | 从设置中移除指定扩展包及其来源。`-l` / `--local` 表示从项目设置 `.pi/settings.json` 中移除。 |
| `uninstall` | `pi uninstall <source> [-l]` | `remove` 的别名，行为和参数完全相同。 |
| `update` | `pi update [source\|self\|pi] [选项]` | 更新 pi 自身、已安装扩展包或模型目录。无目标时只更新 pi；`self` 是 `pi` 的别名。 |
| `list` | `pi list` | 列出用户级和项目级设置中已安装的扩展包。 |
| `config` | `pi config [-l]` | 打开终端交互界面，启用或禁用扩展包提供的资源。默认从全局设置 `~/.pi/agent/settings.json` 打开；`-l` 从项目覆盖设置 `.pi/settings.json` 打开。在界面中可按 `Tab` 在两个作用域之间切换。 |

### `install` 来源示例

```bash
pi install npm:@foo/bar
pi install git:github.com/user/repo
pi install https://github.com/user/repo
pi install ./local/path
```

### `update` 的目标和选项

| 写法 | 效果 |
| --- | --- |
| `pi update` 或 `pi update --self` | 仅更新 pi（默认行为）。 |
| `pi update self` 或 `pi update pi` | 仅更新 pi。 |
| `pi update --extensions` | 仅更新已安装的扩展包。 |
| `pi update --models` | 仅刷新模型目录。 |
| `pi update --all` | 同时更新 pi 和所有扩展包。 |
| `pi update <source>` 或 `pi update --extension <source>` | 仅更新一个指定扩展包。 |
| `pi update --force` | 即使当前版本已经最新，也重新安装 pi。 |

## 项目本地设置的信任选项

`install`、`remove`、`uninstall`、`update`、`list` 和 `config` 支持下列选项，用于决定该次调用是否读取项目本地文件：

| 选项 | 说明 |
| --- | --- |
| `-a`、`--approve` | 信任并使用项目本地文件。 |
| `-na`、`--no-approve` | 忽略项目本地文件。 |

## 常用全局选项

以下不是子命令，但会影响默认的交互式/非交互式 `pi` 调用：

| 类别 | 选项 | 说明 |
| --- | --- | --- |
| 模型 | `--provider <name>`、`--model <pattern>`、`--models <patterns>`、`--thinking <level>`、`--api-key <key>` | 选择提供方、模型或模型循环列表，设定思考级别或直接提供 API 密钥。模型可写成 `provider/id`，也可附加 `:thinking`。 |
| 提示词 | `--system-prompt <text>`、`--append-system-prompt <text>` | 替换或追加系统提示词；后者可重复使用，也可读取文件内容。 |
| 输出 | `--mode <text\|json\|rpc>`、`-p` / `--print` | 选择输出模式，或以非交互方式执行后退出。 |
| 会话 | `-c` / `--continue`、`-r` / `--resume`、`--session <path\|id>`、`--session-id <id>`、`--fork <path\|id>`、`--session-dir <dir>`、`--no-session`、`-n` / `--name <name>`、`--export <file>` | 继续、选择、创建、分叉、命名、存放或导出会话。 |
| 工具 | `-nt` / `--no-tools`、`-nbt` / `--no-builtin-tools`、`-t` / `--tools <names>`、`-xt` / `--exclude-tools <names>` | 禁用全部/内置工具，或设置工具白名单、黑名单。内置工具名为 `read`、`bash`、`edit`、`write`、`grep`、`find`、`ls`。 |
| 扩展资源 | `-e` / `--extension <path>`、`-ne` / `--no-extensions`、`--skill <path>`、`-ns` / `--no-skills`、`--prompt-template <path>`、`-np` / `--no-prompt-templates`、`--theme <path>`、`--no-themes` | 显式加载或禁用自动发现的扩展、技能、提示模板和主题。 |
| 上下文与启动 | `-nc` / `--no-context-files`、`--offline`、`--verbose`、`-a` / `--approve`、`-na` / `--no-approve` | 禁止加载 `AGENTS.md`/`CLAUDE.md`，禁用启动网络操作，强制输出启动详情，或控制项目本地文件是否被信任。 |
| 模型查询 | `--list-models [search]` | 列出可用模型；可提供可选的模糊搜索词。 |

> 使用 `pi <command> --help` 可查看某个子命令的实时帮助；这也能反映后续升级或已加载扩展带来的变化。
