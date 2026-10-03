# CalShot 开发说明

CalShot 使用 Python 生成原生 iPhone 快捷指令。日常安装和使用见[README](README.md)，版本变化见[版本历史](History.md)。当前快捷指令版本为0.1.13，正式流程没有阶段计时或通知。

## 环境与结构

- Python 3.10 或更新版本；仅依赖标准库。
- 生成器可在 Windows、macOS 或 Linux 上运行；iPhone 导入需要有效签名。
- Windows 签名工具使用 Windows PowerShell 5.1；macOS 可使用系统的 `shortcuts sign`。

| 路径 | 职责 |
| --- | --- |
| `src/calshot/shortcut.py` | 原生动作序列化、模型请求、日期与提醒、日历预选和生成文件 |
| `src/calshot/__init__.py` | 快捷指令版本号 |
| `manage.py` | 生成、验证及显式自检入口 |
| `tests/verify_workflow.py` | 结构检查和固定数据回归，不调用真实模型 |
| `tests/verify_signer.ps1` | Windows 签名路径及本地验证模式的回归，不联网 |
| `scripts/sign-shortcut.ps1` | 校验生成源码、清理限定输出、签名并记录校验值 |
| `sign-on-windows.cmd` | 从任意工作目录启动项目签名工具 |
| `LICENSE` | MIT 许可证及版权声明 |
| `dist/` | 可重建输出，不进入 Git |

## 生成与检查

在项目根目录运行：

```sh
python manage.py verify
python manage.py verify --calendar-slots 8
python manage.py build --calendar-slots 3
```

`verify` 检查并重建，`build` 只重建。日历位置数量为任意正整数，省略参数时为5。每个位置对应一个原生单选安装项，名称和用途不固定；并非一页复选框多选，也不枚举全部日历。

默认只写出当前正式版、一个复用正式提醒计算的独立自检及构建记录，不生成压缩包或历史副本。

| 输出 | 含义 |
| --- | --- |
| `dist/calshot.plist` | XML 源码，Windows 签名输入 |
| `dist/calshot.unsigned.shortcut` | 未签名二进制源码 |
| `dist/calshot.shortcut` | 签名成功后的安装文件 |
| `dist/manifest.json` | 版本、源码校验值及匹配的签名记录 |
| `dist/verification.json` | 本地检查结果和验收范围 |
| `dist/reminder-runtime-check/` | 当前独立提醒自检的生成文件 |

重建会核对已有签名与源码及输出校验值；旧安装文件即使仍存在，也不代表与新源码匹配。当前0.1.13曾撤回阶段通知试验，仅比较版本号不足以区分文件。

## Windows 签名

先运行 `python manage.py verify`，再双击项目根目录 `sign-on-windows.cmd`，或运行：

```powershell
.\sign-on-windows.cmd
.\sign-on-windows.cmd -ReminderCheck
```

工具根据自身所在目录定位项目，可放在不同磁盘或带空格的路径。清理只针对该项目 `dist` 中已知生成文件，拒绝符号链接和目录联接；检查路径、清理计划及源码校验值后才执行清理或发送签名请求。

只检查本地输入和清理计划，可以运行：

```powershell
.\sign-on-windows.cmd -ValidateOnly
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/verify_signer.ps1
```

`-ValidateOnly` 不联网、不签名、不修改或清理文件。Windows 路径回归使用临时样本检查移动目录、含空格的路径、日历自检目标及篡改输入，随后清理自身测试样本。它也尝试创建越界目录联接；若环境禁止创建，会明确记录该项跳过，不能当作实际联接检查已通过。

实际 Windows 签名会把重新生成的占位密钥源码提交至第三方 HubSign 服务。无需真实 API Key，请在 iPhone 导入后填写密钥。HTTP200和AEA1容器检查只证明收到预期格式，手机是否接受、运行和保存仍须单独验收。网络连接失败、重定向或403应按签名服务错误处理，不是模型API密钥错误。

## macOS 签名

在项目根目录生成后使用系统命令：[Apple 说明](https://support.apple.com/zh-cn/guide/shortcuts-mac/apd455c82f02/mac)。

```sh
shortcuts sign --mode anyone --input 'dist/calshot.unsigned.shortcut' --output 'dist/calshot.shortcut'
```

该命令不会自动回写本项目的签名记录；请自行确认签名输出与当次生成输入一致，并在 iPhone 上导入测试。不要把未签名源码只改文件名后作为安装文件。

## 实现边界

每次运行截取屏幕，仅读取当前 CalShot 自身保存的配置。日历参数载体位于固定0等于1的禁用分支，正式流程不查询已有日程，也不读取其他快捷指令源码。

选中的真实名称和标识在本地校验及去重；同名不同标识提示配置错误。候选名称和默认名称进入一次识别请求，标识及完整配置源码不发送给模型。编辑页通过名称变量预选，不能把本地读到标识解释为按ID绑定已通过真机验证。

模型请求使用 `https://api.deepseek.com/chat/completions`、`deepseek-flash`、开启思考及 `high` 强度、JSON输出、非流式和16,384 token上限。图片保持原始尺寸，JPEG质量0.85，细节为 `high`。其他厂商与接口尚未接入。

模型未知时间留空，由快捷指令按README默认规则补齐。提醒值为：`null`或无效值默认30分钟，`-1`不提醒，`0`开始时，正整数为提前分钟数。三个原生提醒选项均为字面量，提前提醒使用 `Custom` 和计算后的ISO时刻；每条事件只在系统编辑页确认，由用户保存。

详细信息记录来源、编辑页生成前的设备时间（含时区）、相关可见原文及非空活动备注。添加时间不表示最终保存时间，正式流程不显示预填说明或核对窗口。

源码仍保留共享输入分支，但当前真机反馈未验证成功，不将其列为可用功能。

## 独立自检与验收

默认提醒自检不调用模型，只打开一个可取消的编辑页，详细信息显示计算分钟、选项和提醒日期。查看后取消，不自动保存。

仅在需要排查时生成其他自检：

```sh
python manage.py performance-check
python manage.py calendar-selection-check
python manage.py editor-check
```

分别用 `-PerformanceCheck` 或 `-CalendarSelectionCheck` 签名对应诊断；日期编辑页自检仅用于独立排查。普通生成不写出这些文件。配置耗时自检不截图、不调用模型、不查询或写入日程，其耗时不能代表API等待。

本地检查覆盖原生字段与引用、日期默认、提醒、任意日历位置、分类回退、来源和必要错误；不能模拟iOS权限、名称解析、编辑页、保存或通知。验收需分别记录本地检查、签名、iPhone导入、真实模型调用及真机编辑保存/提醒；已知问题见版本历史。

## 维护、仓库与安装链接

修改生成源码，避免只手改 `dist`。行为变化更新版本号及版本历史，运行 `python manage.py verify`；修改Windows工具时额外运行路径回归。用户说明放README，开发方法集中在本文件，不加入本机目录、真实密钥或私人截图。

使用一个 GitHub 仓库维护源码和文档。Git范围包含生成器、测试、工具、三份README、`LICENSE`及Git规则；本地维护约定、详细排查记录、缓存、密钥及 `dist` 被忽略。`dist` 是可重建的本地输出，安装入口使用 README 中的 iCloud 快捷指令链接。

安装链接准备流程：生成并检查当前版本，签名后在 iPhone 导入不含真实 API Key 的安装模板，通过系统共享取得 iCloud 快捷指令链接，再填入 README 顶部的安装入口。更新快捷指令时同步更新链接和版本说明；发布前分别核对签名、导入、真实模型调用及真机编辑保存和提醒。当前链接尚未提供，README 保持“待补充”。

项目采用 [MIT 许可证](LICENSE)，版权声明为 `Copyright (c) 2026 Feixu Zhu`，文本采用[标准 MIT 许可证](https://spdx.org/licenses/MIT.html)。第三方资料或代码仍按其原有许可证保留适用的声明。

## 参考

流程参考[Add to Calendar](https://github.com/Rowan-Strang/Add-to-Calendar)，原生动作字段参考[ScPL](https://docs.scpl.dev/actions/addnewevent)。资料和本地模拟不能替代设备验收；公开引用和第三方代码应保留适用的署名及许可证。
