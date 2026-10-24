# 第三方依赖

微信适配依赖 [Hello-Mr-Crab/pywechat](https://github.com/Hello-Mr-Crab/pywechat) 的 `pyweixin` 包，安装包名为 `pywechat127`。固定源码提交：`8589baa049bb91d3a500602c167f07b2f8397a13`，源目录为 `src/`。

该项目上游 [LICENSE](https://github.com/Hello-Mr-Crab/pywechat/blob/8589baa049bb91d3a500602c167f07b2f8397a13/LICENSE) 为 GNU LGPL 2.1 文本。本项目通过普通 Python 导入调用它，未复制或修改其源代码。安装时保留上游包和源码来源，发布打包版本时应同时履行其许可证要求。

`httpx` 使用 BSD-3-Clause，`python-dotenv` 使用 BSD-3-Clause。其余 Windows 自动化依赖随 `pywechat127` 安装，许可证以对应软件包的发行文件为准。

本地 OCR 使用 [RapidOCR](https://github.com/RapidAI/RapidOCR) 的 `rapidocr-onnxruntime==1.4.4`（Apache-2.0）及发行包自带的三个 ONNX 模型；本项目不复制模型进 Git 仓库、不使用网络 OCR。推理运行时固定 `onnxruntime==1.20.1`（MIT），避免本机更新版本导入崩溃。图像定位使用 OpenCV（Apache-2.0），数组处理使用 NumPy（BSD-3-Clause），截图使用 Pillow（HPND）。模型与依赖的具体许可文件以安装的原发行包为准。
