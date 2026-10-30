<!-- markdownlint-disable MD033 MD041 -->
<p align="center">
  <img alt="LOGO" src="https://cdn.jsdelivr.net/gh/MaaAssistantArknights/design@main/v1/icons/maa-logo_512x512.png" width="256" height="256" />
</p>

<div align="center">

# MaaSquad

</div>

MaaSquad 是基于 [MaaFramework](https://github.com/MaaXYZ/MaaFramework) 开发的自动化助手，由 [MaaPracticeBoilerplate](https://github.com/MaaXYZ/MaaPracticeBoilerplate) 模板创建。

> **MaaFramework** 是基于图像识别技术、运用 [MAA](https://github.com/MaaAssistantArknights/MaaAssistantArknights) 开发经验去芜存菁、完全重写的新一代自动化黑盒测试框架。
> 低代码的同时仍拥有高扩展性，旨在打造一款丰富、领先、且实用的开源库，助力开发者轻松编写出更好的黑盒测试程序，并推广普及。

## 即刻开始

请阅读 [如何开发](./docs/zh_cn/develop/how_to_develop.md)。

提交改动前，请阅读 [PR 规范](./docs/zh_cn/develop/pull_request_guidelines.md)。

## Web 调用

MaaSquad 内置可开关的 HTTP API、WebSocket 与网页控制台，可在本地通过接口执行 `interface.json` 中编排好的任务：

```bash
pip install -r service/requirements.txt
python service/main.py
```

详见 [Web 服务](./docs/zh_cn/develop/web_service.md)。

## 本地测试环境

可以直接在 Linux 实例上启动测试环境：除了 Linux 原生环境，还会用 Wine 搭建一个运行 Windows 版 MaaFramework 的「Windows 测试机」，测试桌面端（Win32）控制器与 Windows 下的行为：

```bash
testenv/testenv.sh deps   # 安装 Python、Wine、Xvfb 等系统依赖（apt）
testenv/testenv.sh all    # 搭建两套环境并运行全部测试
```

详见 [本地测试环境](./docs/zh_cn/develop/test_env.md)。

## 生态共建

MAA 正计划建设为一类项目，而非舟的单一软件。

若您的项目依赖于 MaaFramework，我们欢迎您将它命名为 MaaXXX, MXA, MAX 等等。当然，这是许可而不是限制，您也可以自由选择其他与 MAA 无关的名字，完全取决于您自己的想法！

同时，我们也非常欢迎您提出 PR，在 [社区项目列表](https://github.com/MaaXYZ/MaaFramework#%E7%A4%BE%E5%8C%BA%E9%A1%B9%E7%9B%AE) 中添加上您的项目！

## 常见问题

请阅读 [常见问题](./docs/zh_cn/develop/faq.md)。

## 鸣谢

本项目由 **[MaaFramework](https://github.com/MaaXYZ/MaaFramework)** 强力驱动！
