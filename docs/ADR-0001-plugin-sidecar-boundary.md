# ADR-0001：单 Blender 插件 + 本机 MCP Sidecar

- 状态：Accepted
- 日期：2026-08-24

## 决策

采用一个 Blender 插件，内部包含 provider-neutral core、Tripo/Hunyuan adapters、Blender 操作和凭据保险库；另附一个无 provider key 的本机 STDIO MCP sidecar。两者通过 loopback 上的随机会话令牌认证。

## 原因

Blender 是资产上下文、视觉检查、选择、撤销和人工批准的权威；MCP 是受限的高层编排入口。拆成两个 provider 插件会复制 UI、任务状态和处理逻辑；脱离 Blender 的万能 MCP 又会失去 DCC 上下文并扩大攻击面。

## 后果

- provider schema 漂移被限制在 adapter 内；
- MCP 不能读取 provider key，也不返回下载 URL；
- Blender 必须提供线程安全的主线程调度和生命周期管理；
- 首次工程量高于纯插件，但人工和 AI 共用同一任务记录与候选选择门。
