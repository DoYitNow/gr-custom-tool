# 校色数值链路

公开版的 XMP 解析、Look/Profile 支持和颜色拟合规则沿用研究版。程序先检查 XMP 是否包含完整颜色数据，再用已登记的 GR IV DNG 建立目标，最后生成可写入已核验固件布局的候选颜色资源。全新数据目录会自动登记随包的 3 张作者提供的 GR IV DNG，`R0000398.DNG` 为独立检查样片；已有登记不被覆盖。

`gr4_editor/assets/calibration/acr3-default-tone.json` 保存 DNG SDK ACR3 forward tone 的固定数值基线；它的来源和许可见 [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) 及 [`licenses/adobe-dng-sdk`](../licenses/adobe-dng-sdk/)。`constructor-gamma.json` 是研究所得的数值基线，其来源和公开再分发权利需要按第三方材料说明核定。

默认“本地模拟”使用 rawpy/LibRaw 解码 DNG、OpenCV 做近似目标渲染、NumPy 进行拟合；它不等同于 Lightroom、Adobe Camera Raw 或相机实测。Windows 可选“Lightroom Classic”使用 `gr4_editor/lightroom_bridge.py` 的插件请求和回执，让 Classic 导出当前 XMP 对应的目标 JPEG，再送入同一软件域拟合。程序不会自动启动 Classic，需用户安装并打开 Classic、在增效工具管理器启用 GR4 Firmware Bridge。当前尚未完成真实 Lightroom 渲染验收。

报告中的 `fitted_offline` 是共同的软件拟合完成状态，具体目标来源以任务的 `render_engine` 为准；即使目标来自 Classic，拟合后的机内效果仍是待验证的候选。仓库不附 JPEG 或 XMP 样片，也不宣称通过实机验证。使用流程见[校色说明](CALIBRATION.md)。
