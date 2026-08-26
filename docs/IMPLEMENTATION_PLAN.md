# 通用 MeshDock 实施状态

## 已完成

- Provider-neutral 任务内核、Tripo、混元与 Compare；开发用 Mock 仅供自动化测试，不进入正式 UI 或 MCP；
- 文本、单图、多视图输入和安全参考图暂存；
- 生成任务持久化、恢复、重试、归档、本地取消、批量创建、并发限制和预算门；
- 候选预览、隔离 Collection 导入、显示控制、拒绝、删除和 review pack；
- Provider 后处理派生链：重拓扑、UV、纹理、分件、转换；
- Tripo rig-check/auto-rig/retarget 与混元 rigging/motion；
- 后处理任务持久化与恢复，诊断型任务不伪造模型候选；
- Blender 角色 Action 选择、时间轴预览与动画导出；
- 非破坏式静态归一化与常用 3D 格式导出；
- Blender 多账号凭据 UI、默认系统凭据库保存、账号切换与删除操作；
- macOS Keychain 与 Linux Secret Service 原生凭据后端；
- 供应商/模型动态输入约束、Blender 自适应控件和 MCP 预查询；
- 生成优先级、本地暂停/继续和候选重生成；
- 受认证 loopback bridge、完整方法白名单和高层 STDIO MCP；
- Blender extension 构建、校验和 Windows 安装脚本。

## 明确不做

- 项目仓库入库、项目命名或引擎发布；
- 模型质量审计、资产预算验收和通过/失败判定；
- 任意 Python、逐顶点遥控、任意文件读取或任意 URL 下载；
- MCP 读取/写入供应商凭据或替用户选择最终候选。

## 仍需真实环境验证

- 当前供应商账号权限下各模型名、参数组合、返回文件格式和实际计费；
- Blender 5.2 中各导入/导出 operator、骨架 Action 与动画烘焙；
- E 盘 Blender 的 extension 更新路径与已运行 MCP client 的配置刷新；
- 大模型文件、网络重试、供应商限流与 Blender 关闭时的恢复行为。

## 已知外部限制

- 未公开远端取消 API 的供应商只能执行本地取消，远端可能继续计费；
- 混元后处理依赖源任务仍可查询到临时输出，过期后需要重新生成或转换；
- 供应商没有远程暂停能力；插件暂停的是本地排队、轮询和下载，已提交的远端任务可能继续运行和计费。
