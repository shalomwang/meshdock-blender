# 服务商兼容矩阵

本表描述 Mesh Dock 0.10.0 的适配器支持范围，不代表平台提供的全部功能。界面和任务提交共用输入约束；不支持的参数会在排队前被拒绝。

## 生成输入

| 服务与模型 | 文字 | 单图 | 多视图 | 视图入口 |
| --- | --- | --- | --- | --- |
| Tripo CN / Global：V3.1、V3.0、V2.5、P1 | 支持 | 正面图 | 2–4 张，正面必需 | 正面、左、背面、右 |
| 混元 Direct 3.0 | 支持 | 正面图 | 2–4 张，正面必需 | 正面、左、右、背面 |
| 混元 Direct 3.1 | 支持 | 正面图 | 2–8 张，正面必需 | 3.0 的四视图，加顶、底、左前、右前 |
| TokenHub 混元 3.0 / 3.1 | 支持 | 随对应模型 | 随对应模型 | 随对应模型 |
| TokenHub Tripo 3.1 / P1 | 支持 | 未启用 | 未启用 | — |

Tripo 接受 PNG、JPEG、WebP。混元多视图接受 PNG、JPEG；单图也可用 WebP。Tripo 图片每边至少 128 像素；混元多视图要求每边大于 128、小于 5000 像素，原文件合计不超过 6 MB。

TokenHub Tripo 暂只开放文字输入，避免提交未明确验证的图片请求。MCP 的 Compare 功能使用两个适配器共同支持的输入范围。

## 界面与处理

- 选择平台、模型或输入方式后，可用账号、视图和参数会重新计算；图片草稿保留，仅提交兼容视图。
- Tripo V2.5 不显示 V3 专用的几何、分件和质量控件。绑定模型不同，可用角色类型也不同。
- 混元 3.1 不显示 LowPoly 和 Sketch 类型；TokenHub Tripo 不显示混元参数。
- 后处理只列出当前来源、格式和账号支持的操作。动作生成要求服务商返回的绑定结果。
- 自建或修改后的网格需要支持本地 GLB 上传的服务。上传快照不会保留完整 Blender 材质节点、动画或骨架。

这些限制由适配器实现，模拟测试不等于真实账号或计费验证。服务商更新接口后，插件可能需要升级。

## 凭据与 MCP

账号按 Tripo CN、Tripo Global、混元 Direct、TokenHub CN 和 TokenHub Global 分开管理。新密钥默认存入系统凭据库；不可用时回退到当前 Blender 会话。任务提交后绑定所选账号，异步执行期间切换界面不会换用另一个密钥。

MCP 客户端应先调用 `get_generation_constraints` 或 `get_process_capabilities` 再提交任务。提交时仍会重复校验。MCP 不返回凭据、账号备注、服务商下载地址或任意本地路径。

## 官方参考

- [Tripo 多视图生成](https://developers.tripo3d.ai/en/docs/generation-multiview-to-model/standard)
- [腾讯混元专业版任务](https://cloud.tencent.com/document/product/1804/123447)
- [TokenHub 混元 API](https://cloud.tencent.com/document/product/1823/130082)
- [TokenHub Tripo API](https://cloud.tencent.com/document/product/1823/136143)

提交前请核对平台的账号权限和最新价格。费用预估不能代替服务商账单。
