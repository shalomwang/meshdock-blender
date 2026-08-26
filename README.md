# MeshDock

在 Blender 里直接使用 Tripo、混元等的模型，也让 Codex、Claude Code、OpenCode 等 Agent 能参与同一套 3D 工作流。

让 LLM 从零开始通过代码或逐点逐面操作完成建模，现阶段通常既慢，精度也不理想。使用 AI 3D 服务的网页端往往能更快得到可用的基础模型，但用户需要反复切换浏览器、上传参考图、等待任务、下载文件，再回到 Blender 导入和整理。对于 Agent 来说，这段流程几乎是断开的。

MeshDock 把这些步骤接回 Blender：普通用户不必离开 Blender 就能提交生成、比较候选、调用供应商后处理并导入结果；Agent 则可以通过受限的本机 MCP 调用同一套高层任务。再配合 Blender MCP，Agent 可以继续检查场景、调整变换、整理 Collection、处理材质或执行确定性的清理工作，而不需要自己低效地“捏”出整个模型。

简单来说：

- AI 3D 服务负责生成更好的基础模型；
- MeshDock 负责连接供应商、任务、文件和 Blender；
- Agent 负责编排流程，并可借助 Blender MCP 操作场景；
- 用户仍然负责审阅结果和决定最终使用哪个候选。

![MeshDock 工作流：用户通过 Blender 面板操作，Agent 分别通过 MeshDock MCP 和 Blender MCP 完成生成编排与场景处理](docs/images/meshdock-workflow.svg)

## 为什么做这个项目

MeshDock 想解决的不是“让聊天机器人代替建模师”，而是两个更实际的问题：

1. **减少手工搬运。** 不再为了每次生成来回打开网页、下载和导入文件，模型会进入本机 staging，并可直接导入独立的 Blender Collection。
2. **补上 Agent 工作流缺口。** Agent 能调用文本、单图和多视图生成，查询任务、导入候选和调用后处理；配合 Blender MCP 后，还能在 Blender 中继续检查和整理结果。

MeshDock 只向 Agent 暴露生成、查询、导入、处理等高层资产任务，不提供读取密钥、任意 Python、逐顶点建模或替用户批准候选的万能入口。

两类 MCP 各自负责不同的事情：

```text
                         ┌─ MeshDock MCP ── 生成 / 查询 / 导入 / 供应商后处理
Codex / Claude Code ─────┤
OpenCode / 其他 Agent    └─ Blender MCP ─── 场景检查 / 调整 / 清理
                                      │
用户 ─────────────── MeshDock 面板 ────┤
                                      ▼
                                   Blender
                                      │
                         Tripo / Hunyuan / TokenHub
```

MeshDock 自带的是受限的高层资产 MCP sidecar；通用 Blender MCP 是可以并行使用的配套能力，并不由 MeshDock 取代。供应商密钥始终由 Blender 插件和系统凭据库管理，不会交给 MCP Client。Blender 仍然是场景状态、视觉审阅和人工批准的权威。

## 主要能力

- 文本、单图和多视图生成，视角数、格式和尺寸随供应商及模型动态变化；
- Tripo 中国大陆版与国际版、混元直连、TokenHub 中国站与国际站独立账号和端点；
- 多候选、费用预估与上限、优先级、暂停、恢复、重试和本地 staging；
- 候选隔离导入、显示控制、审阅图、拒绝、删除和人工选择；
- 供应商支持范围内的重拓扑、UV、纹理、分件、格式转换、绑定和动画；
- 模型/模式相关的低模与面数控制，提交前由 adapter 再次校验；
- GLB、glTF、FBX、OBJ、STL 和 USD 导出；
- Windows Credential Manager、macOS Keychain 与 Linux Secret Service；
- 高层 MCP 工具白名单和工具审批友好的只读/写入注解；
- 简体中文与英文 Blender UI。

## 供应商支持

| 服务     | 生成               | 后处理                                   |
| -------- | ------------------ | ---------------------------------------- |
| Tripo    | 文本、单图、多视图 | 重拓扑、纹理、分件、转换、绑定、动画     |
| 混元     | 文本、单图、多视图 | —                                        |
| TokenHub | 文本、单图、多视图 | 重拓扑、UV、纹理、分件、转换、绑定、动画 |

精确模型、输入视角和格式限制见 [供应商兼容矩阵](docs/PROVIDER_COMPATIBILITY.md)。供应商接口和计费可能变化，生成前请核对界面中的预估与官方控制台余额。

## 安装

要求：

- Blender 5.2 或更高版本；
- MCP sidecar 使用 Python 3.11 或更高版本；
- 使用云端供应商时需要对应账号及网络连接。

### 使用 AI Agent 安装与配置（最便捷）

1. 使用任意一款 AI Agent （Codex、Claude Code、OpenCode等）。
2. 将仓库地址（https://github.com/shalomwang/meshdock-blender）发送给你的 Agent，并要求它进行安装和配置。

### 从 Release 安装 Blender 扩展

1. 下载 `meshdock-0.9.6.zip`。
2. 在 Blender 打开 `Edit > Preferences > Extensions`。
3. 选择 `Install from Disk` 并安装 ZIP。
4. 在 3D View 按 `N`，打开 `MeshDock` 页签。

### 从源码安装

Windows 可同时构建 Blender extension 并安装当前用户的 MCP sidecar：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\install_windows.ps1
```

也可以指定 Blender：

```powershell
.\tools\install_windows.ps1 `
  -BlenderPath "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"
```

只构建扩展：

```powershell
python tools/build_extension.py
```

## 快速开始

1. 打开 `View3D > MeshDock`，点击“管理账号”。
2. 选择服务，输入 Secret Key，可选填写便于辨认的备注。
3. 新账号默认保存到系统凭据库并启用；管理器会显示所有账号，并提供启用、停用和删除操作。
4. 依次选择生成方式、账号和模型；已停用、不兼容的账号或模型不会显示。
5. 输入提示词或添加参考图，检查质量、面数和预计费用后生成。
6. 在 Blender 中导入并比较候选，需要时执行供应商后处理。
7. 由用户选定候选后再导出。

如果系统凭据库不可用，账号会自动退化为仅本次 Blender 会话有效，并在界面中明确提示。

## 凭据与安全边界

密钥不会写入 `.blend`、任务 JSON、仓库、MCP 配置或日志。系统凭据库之外的本地配置只保存随机账号 ID、供应商和备注。

也支持由 Blender 进程读取固定白名单环境变量：

```text
TRIPO_API_KEY
TRIPO_CN_API_KEY
TRIPO_GLOBAL_API_KEY
HUNYUAN_3D_API_KEY
HUNYUAN_DIRECT_API_KEY
TOKENHUB_API_KEY
TOKENHUB_CN_API_KEY
TOKENHUB_INTL_API_KEY
TOKENHUB_GLOBAL_API_KEY
```

建议只在启动 Blender 的专用脚本中注入变量，不要写入仓库、`.blend` 或 MCP 配置。MCP 的 `provider_status` 只返回配置状态布尔值，不返回账号 ID、备注、来源、变量名或密钥。

同一 OS 用户下的无限制 Shell 权限无法与该用户的环境变量和凭据形成强隔离。需要更高安全等级时，应把凭据代理放在独立 OS 账户或受 ACL 保护的本机服务中。

## MCP 配置

安装 sidecar 后，将 [示例配置](docs/codex-mcp.example.toml) 复制到可信 MCP client 的配置中。先启动 Blender，再启动 MCP client。

```toml
[mcp_servers.meshdock]
command = "python"
args = ["-m", "meshdock.sidecar"]
enabled = true
```

推荐对生成、重试、导入、后处理和导出工具保留人工审批。sidecar 不提供 `execute_python`、任意文件读取、凭据读写或 `select_candidate`。

## 项目边界

本项目不负责：

- 特定游戏或影视项目的资产入库；
- 模型质量审计或项目预算验收；
- 任意 Python、逐顶点遥控或任意 URL 下载；
- 替用户决定最终美术候选；
- 在供应商未提供远程取消接口时终止已经提交的远端计费任务。

## 开发

```powershell
python -m compileall -q meshdock
python -m pytest
python tools/build_extension.py
```

真实供应商测试可能产生费用，不属于默认测试。开发用确定性 provider 只存在于内部测试路径，不会出现在正式 Blender UI、运行时能力列表或 MCP 公共 schema 中。

架构决策见 [ADR-0001](docs/ADR-0001-plugin-sidecar-boundary.md)，当前实现状态见 [IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)。

## License

MeshDock 采用 [MIT License](LICENSE)。
