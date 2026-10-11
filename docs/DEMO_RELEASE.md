# 发布说明

本仓库是公开发布快照。固件容器读取、写入、版本策略和回退以公开版实现为准；XMP 读写规则与颜色拟合来自研究版，并随选定稳定提交同步。公开版不内置原厂固件、JPEG 样片或第三方 XMP；内置 3 张作者提供的 GR IV DNG，供首次校色初始化。默认采用本地模拟目标渲染，Windows 可选 Lightroom Classic 桥接。柔焦／柔光专项冻结，不随发布快照启用。

## 发布范围

当前发布分支为 `main`，首版 Git 标签为 `v0.1.0`。版本标签指向已提交的发布源码；工作目录中未提交的修改不属于该版本。`main` 基于 orphan 分支建立，没有继承旧滤镜实现的提交历史；本地完整版本分支保留作参考，不应发布。

GPL-2.0-only 适用于本次新增 [LICENSE](../LICENSE) 的 `main` 发布快照及其声明范围。旧 `v0.1.0` 标签不含此次 `LICENSE`，不移动该标签，也不将旧快照标为已包含本次许可。发布应选择已提交且包含 `LICENSE` 的当前 `main`，或从该提交创建的新标签。

发布到已配置的远端时，仅指定 `main`；如需带版本标签，另行推送指向上述已授权提交的新标签：

```sh
git push origin main
```

也可用 `git archive` 导出已提交的 `main` 快照或上述新标签对应、不含 Git 历史的源码 ZIP：

```sh
git archive --format=zip --output=gr-custom-tool-main-source.zip main
```

不要使用 `git push --all` 或 `git push --mirror`，也不要把整个工作目录连同 `.git`、本地环境或输入文件一起发布。`git archive` 导出选定提交中的源码、随包捐赠二维码和明确登记的 3 张默认 DNG，不包含用户固件、编辑图片或忽略的其他本地文件。

## GitHub 捐赠入口

计划仓库名为 `DoYitNow/gr-custom-tool`。已准备 `.github/FUNDING.yml`，使用 GitHub 支持的 `custom` 链接指向本仓库 README 的 `support` 区域；该区域包含免费声明、捐赠二维码和设备适配联系说明。仓库发布到该地址、默认分支为 `main`，并在仓库 Settings → General → Features 中启用 Sponsorships 后，仓库可显示 Sponsor 按钮。

这里使用自定义赞助页，不需要先开通 GitHub Sponsors 收款账号。原生 GitHub Sponsors 需要在支持的地区完成收款资格设置；目前官方地区列表不含中国大陆。仓库名称改变时，应同步更新 `.github/FUNDING.yml` 的地址。

配置与收款资格参考 [GitHub Sponsor 按钮说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/displaying-a-sponsor-button-in-your-repository)和 [GitHub Sponsors 地区说明](https://docs.github.com/en/sponsors/getting-started-with-github-sponsors/about-github-sponsors)。本地配置不表示已创建或发布 GitHub 仓库。

## 后续开发

产品改动仍在研究仓库的唯一完整核心中开发，再将选定稳定提交中的固件公共逻辑、XMP 规则和必要模块同步到当前发布配置。每次同步应保留本版功能范围和独立恢复标记；除已列明的 3 张内置 DNG 外，不把研究 Git 历史、其他私有样片或柔焦／柔光冻结专项合入本分支。

当前没有自动同步脚本或安装包。用户首次运行仍需自行准备 64 位 Python 3.12；生成需要 LLVM 工具链，校色还需要 NumPy、rawpy、OpenCV 和 Capstone，启动器负责安装并检查固定版本依赖，详见 [README](../README.md)。全新数据目录会使用仓库内置的 3 张 GR IV DNG（`R0000398.DNG` 为独立检查样片）；已有校色数据不会被覆盖，也可以改用自己的样片。理光公开页面的 JPEG 预览不能替代 DNG。Lightroom Classic 是 Windows 可选链路，需要用户自行安装并打开 Classic、在增效工具管理器启用 GR4 Firmware Bridge；程序不会自动启动 Classic。该桥接目前只有接口与离线验证，尚未完成真实 Lightroom 渲染。

当前发布验证以离线测试和文件结构检查为界，不写成实机通过。保留的测试文件不表示本次已运行或通过；完整版本此前的离线记录不能替代本版验证。发布须保留 [LICENSE](../LICENSE)、[使用协议](../web/terms.html)、[作者声明](../AUTHOR_STATEMENT.md)和 [第三方说明](../THIRD_PARTY_NOTICES.md)。
