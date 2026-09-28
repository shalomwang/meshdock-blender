# 开发 Mesh Dock

Mesh Dock 是 Blender 扩展；可选的本机 MCP sidecar 通过受认证的 loopback bridge 访问同一任务服务。Blender 负责场景操作和凭据，sidecar 只暴露明确列出的高层方法，不提供任意 Python、文件读取或密钥读取。

## 环境

- Blender 5.2 或更高版本，用于扩展安装与界面验证。
- Python 3.11 或更高版本，用于测试、构建和可选的 MCP sidecar。

```powershell
python -m pip install -e .
python -m pytest
python tools/build_extension.py
```

构建产物是 `dist/meshdock-<version>.zip` 和同名的 `.sha256` 文件。ZIP 只包含扩展的 Python 源码、`blender_manifest.toml` 和 `LICENSE`。不要将工作目录、测试截图、暂存模型或本地凭据加入发行包。

## 代码位置

| 路径 | 作用 |
| --- | --- |
| `meshdock/blender/` | Blender 工作区、N 面板、操作符与场景导入 |
| `meshdock/core/` | 任务状态、输入约束、暂存与恢复 |
| `meshdock/providers/` | 各服务商适配器 |
| `meshdock/credentials/` | 系统凭据库与会话凭据 |
| `meshdock/bridge/`、`meshdock/sidecar/` | 本机桥接与 MCP |
| `tests/` | 无付费请求的单元和服务测试 |
| `tools/` | 构建及隔离 Blender 冒烟测试 |

供应商输入和后处理的具体限制在 [兼容矩阵](PROVIDER_COMPATIBILITY.md) 中。增加模型或参数时，同步更新适配器约束、界面和任务提交校验。

常用的 Blender 测试脚本：

```powershell
blender --background --factory-startup --python-exit-code 1 --python tools/blender_smoke_test.py
blender --factory-startup --enable-event-simulate --python tools/blender_scene_workbench_smoke_test.py
blender --factory-startup --enable-event-simulate --python tools/blender_workflow_smoke_test.py
blender --factory-startup --enable-event-simulate --python tools/blender_modified_result_smoke_test.py
```

将 `blender` 替换为本机可执行文件路径。GUI 脚本使用独立进程和模拟传输，结果与截图保存在忽略的 `build/` 目录；退出码正常时仍需检查对应的 `result.json`。

## 发布前验证

1. 运行测试并检查 `git diff --check`。
2. 构建扩展，核对 ZIP 清单、版本号和 SHA-256。
3. 使用独立 Blender 实例安装产物并检查工作区；不要覆盖正在使用的实例。
4. 核对 README、使用说明、兼容矩阵、MCP 示例和 release notes 中的链接与版本。
5. 查看将提交的文件清单，确认没有密钥、日志、模型、截图缓存或本地配置。

模拟测试不能证明真实账号权限、服务商实时价格、全部远端参数组合或大模型的网络表现。实际接入测试可能计费，发布说明应明确指出验证边界。
