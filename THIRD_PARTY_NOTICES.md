# 第三方材料与许可

本版提供裁切比例和关机画面功能，不包含滤镜校色实现、Gamma 参数、Adobe SDK 色调或样片。本次 `main` 发布快照中，作者有权授权的原创代码、界面和文档采用 [GPL-2.0-only](LICENSE)，授权范围及例外见[作者声明](AUTHOR_STATEMENT.md)。非商业使用及免费、退款表述是作者自愿倡议，不得限制 GPL 已授予的权利，也不能替代第三方许可证。

## 运行时组件

启动器按照 [requirements.txt](requirements.txt)从官方 PyPI 安装依赖到本机 `.venv`；仓库不附其二进制或整个环境。组件、轮子中附带的本机库及其通知仍依各自许可证，不由作者的非商业声明重新许可。

| 组件 | 实际用途与上游许可参考 |
| --- | --- |
| Pillow | 裁切图标与关机图片处理；[MIT-CMU](https://github.com/python-pillow/Pillow/blob/main/LICENSE)，轮子所含图片库另有许可 |
| Unicorn | 执行原生 ARM 软件路径；[2.1.4 上游 GPLv2 声明](https://github.com/unicorn-engine/unicorn/blob/2.1.4/README.md)与 [COPYING](https://github.com/unicorn-engine/unicorn/blob/2.1.4/COPYING) |

本版仍实际使用 Unicorn。项目原创代码采用 GPL-2.0-only，不对该组件已有的 GPLv2 权利附加作者的非商业限制。由用户自行通过 pip 安装，不代表后续打包和组合分发的全部许可问题自动消失；本次选定项目许可不等于完成全部第三方权利审查，也不重新授权组件及其附带内容。

生成工具链由用户另行准备，不随源码提供。LLVM/Clang/LLD 的许可见 [LLVM 官方许可政策](https://llvm.org/docs/DeveloperPolicy.html#license)。采用已有 NDK 的 LLVM 组件时，应核对其随附开源许可；[NDK 下载条款](https://developer.android.com/ndk/downloads)第 3.5 条将这些开源组件交由各自开源许可证约束，本项目不替用户接受 SDK 整体条款。

若以后发布附带 Python 环境、wheel、工具链或独立可执行文件的安装包，应另行随包提供实际组件的完整许可、通知和必要的源码/分发资料，不能只复制这张概要表。移除校色模块不等于全部第三方权利与发布许可已经处理完毕。

## 用户输入与输出

仓库不包含原厂 BIN、图片素材或旧研究记录。原生目录、图标、文字及需要保留的资源从用户输入读取；用户自行选择的材料及生成候选仅保存到其本地数据目录。

裁切和关机功能会将项目的原生 C 代码编译后写入生成候选。输出中包含的 GPL 项目代码仍受对应许可约束，分发这部分代码须履行适用义务；项目许可不自动授予第三方固件和素材的权利，也不保证完整修改固件可以公开分发或与其中的 GPL 代码兼容分发。

程序能读取或生成文件不代表使用者拥有其修改、公开上传、销售或再分发权利。有关使用边界及异常反馈请阅读 [作者声明](AUTHOR_STATEMENT.md)和 [使用协议](web/terms.html)。
