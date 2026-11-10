# Smoked Salmon 简体中文版本

本分支提供中文网页界面、交互提示、常见错误说明和命令行帮助。程序版本以原项目的版本信息为准。

网页包含概览、上传、转种、频谱、检查、标签整理、转码、搜索、工具和任务页面，使用简体中文显示。

## 安装和启动

先按照 [原项目说明](README.md) 安装 Python、uv、音频工具及项目依赖，并填写自己的配置文件。账号配置应保存在本地，不要提交到 GitHub。

要让命令行帮助、程序提示和日志使用中文，请在启动前设置 `SALMON_LANGUAGE=zh-CN`。

PowerShell：

```powershell
$env:SALMON_LANGUAGE = 'zh-CN'
uv run salmon --help
uv run salmon web
```

Linux 或 macOS：

```sh
SALMON_LANGUAGE=zh-CN uv run salmon --help
SALMON_LANGUAGE=zh-CN uv run salmon web
```

命令行设为 `SALMON_LANGUAGE=en` 或不设置该变量时，沿用英文显示。网页界面固定为中文。

## 翻译范围

- 网页导航、表单、按钮、选项说明、任务状态及交互问题。
- 程序运行提示、主要检查结果、常见连接错误及命令行帮助。
- 命令名称、配置键、API 字段、站点代码和音频格式值保留原文。
- 艺术家、专辑和曲目名称，以及用于上传的发行描述、音频标签和站点参数保留原有内容。

音乐商店或站点返回的资料、第三方工具的原始输出、部分底层异常及上游更新说明可能仍包含英文。

## 维护汉化

Python 翻译表位于 `src/salmon/data/zh-CN.json`，网页使用 `webui/src/lib/zh-CN.json`。修改词条时请同步这两份文件，并保留原文和译文中的 `ZXQ...QXZ` 动态值占位符。

前端源码修改后需要重新构建：

```sh
cd webui
npm ci
npm run check
npm run build
```

构建结果保存在 `src/salmon/webui/static/`，随本分支一并提交。相关回归测试位于 `tests/test_localization.py`。

请保留本分支中的汉化改动，再合并上游更新；直接覆盖源码可能移除本地化内容。实际账号配置、日志、音乐和电脑上的安装工具不包含在本分支中。
