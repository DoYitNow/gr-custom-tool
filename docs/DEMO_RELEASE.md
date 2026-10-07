# 发布说明

本仓库当前发布配置从本地完整编辑器核心截取，只提供裁切比例和关机画面。滤镜入口右侧显示“开发中”标签，本分支源码不包含滤镜或校色实现。

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

不要使用 `git push --all` 或 `git push --mirror`，也不要把整个工作目录连同 `.git`、本地环境或输入文件一起发布。`git archive` 导出选定提交中的源码和随包捐赠二维码，不包含用户固件、编辑图片或忽略的本地文件。

## GitHub 捐赠入口

计划仓库名为 `DoYitNow/gr-custom-tool`。已准备 `.github/FUNDING.yml`，使用 GitHub 支持的 `custom` 链接指向本仓库 README 的 `support` 区域；该区域包含免费声明、捐赠二维码和设备适配联系说明。仓库发布到该地址、默认分支为 `main`，并在仓库 Settings → General → Features 中启用 Sponsorships 后，仓库可显示 Sponsor 按钮。

这里使用自定义赞助页，不需要先开通 GitHub Sponsors 收款账号。原生 GitHub Sponsors 需要在支持的地区完成收款资格设置；目前官方地区列表不含中国大陆。仓库名称改变时，应同步更新 `.github/FUNDING.yml` 的地址。

配置与收款资格参考 [GitHub Sponsor 按钮说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/displaying-a-sponsor-button-in-your-repository)和 [GitHub Sponsors 地区说明](https://docs.github.com/en/sponsors/getting-started-with-github-sponsors/about-github-sponsors)。本地配置不表示已创建或发布 GitHub 仓库。

## 后续开发

产品改动仍在旧研发仓库的唯一完整核心中开发，再将裁切、关机画面及必要公共模块的相关改动同步到当前发布配置。每次同步应保留本版功能范围和独立恢复标记；不把完整滤镜实现或旧研发 Git 历史合入本分支。

当前没有自动同步脚本或安装包。用户首次运行仍需自行准备 64 位 Python 3.12；生成需要 LLVM 工具链，启动器负责安装本版 Python 依赖，详见 [README](../README.md)。

当前功能拆分尚未运行端到端、编译生成或硬件验证。保留的测试文件不表示本次已运行或通过；完整版本此前的离线记录不能替代本版验证。启动空白页面也不等于编辑、生成或设备功能已经验证。发布须保留 [LICENSE](../LICENSE)、[使用协议](../web/terms.html)、[作者声明](../AUTHOR_STATEMENT.md)和 [第三方说明](../THIRD_PARTY_NOTICES.md)。
