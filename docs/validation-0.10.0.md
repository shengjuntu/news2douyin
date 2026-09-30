# 0.10.0 验证记录

验证日期：2026-09-30。环境：Python 3.12.14、SQLite、Chromium 154.0.8037.57、FFmpeg/FFprobe、Pillow、Noto Sans CJK 与本地 eSpeak NG。新闻内容为虚构资料，模型返回使用离线响应桩；未调用真实新闻源、模型账户或联网 Edge TTS。

| 范围 | 结果 | 内容 |
|---|---|---|
| Python 回归 | 199 通过，0 失败，0 跳过 | 既有 185 项与新增 14 项视频工作台检查 |
| 视频浏览器流程 | 18 项通过 | 模板预览、逐段配图、字幕校验、预检、失败恢复、真实播放、下载与作品管理 |
| 既有浏览器回归 | 27 项通过 | 每日采集选题 9、有据脚本工作台 18 |
| 已安装 wheel 与升级 | 44 项通过 | 基础 18、每日流程 6、策略 4、事件 4、脚本 5、视频与 0.9 升级 7 |
| 本地中文配音成片 | 7 项通过 | 实际 eSpeak 配音、H.264/AAC 编码、段落画面、时长与导出校验 |

Python 全量运行耗时 30.14 秒，有 15 条既有依赖或日期 API 弃用提示。浏览器的真实视频播放确认播放时间能向前推进，并验证 HTTP Range 返回 206。配音示例约 8.42 秒；试听效果并未进行主观质量评分。

## 视频制作与作品库

- 三套模板均实际生成预览与成片；检查预览配色不同，导出的帧区间连续，事实和分析段落保留来源、日期依据与图片分配。
- 预检和预览不创建任务、不新增脚本导出；预览支持尚未准备配音的稿件，缺少绘图依赖有明确提示。
- 制作参数固定审核版本和素材指纹。拒绝非本稿素材、失效段落及跨段 SRT 字幕，不按字符数猜测实际切换时刻。
- 模拟编码阶段中断，关闭页面后仍能找到失败任务、查看恢复建议，并复用已保存的配音成功重试。另验证原上传 WAV 丢失后可复用有效配音、输入损坏时阻止不安全重试。
- 作品名称与备注独立保存，修改不会重写审核稿或视频标题；收藏与归档刷新后保持，归档可以恢复。
- 查询覆盖标题与备注、状态、模板、时区日期边界、收藏、归档、分页以及搜索字符转义；编辑覆盖并发版本冲突。
- 桌面和 390px 手机宽度检查通过，无横向溢出与 JavaScript 异常。
- 环境检测实际检查 FFmpeg 编码器；成片、字幕、ZIP 下载以及缺失产物提示均已检查。

浏览器使用 `tools/smoke_video_workbench.py --serve` 和 `tools/smoke_video_workbench_browser.cjs`。该测试服务仅使用临时数据库，并专门注入一次编码失败以验证恢复，不用于日常运行。

- [视频制作设置](validation-0.10.0/video/video-setup.png)
- [作品库](validation-0.10.0/video/video-library.png) · [手机作品库](validation-0.10.0/video/video-library-mobile.png)
- [失败恢复](validation-0.10.0/video/video-recovery.png) · [制作结果](validation-0.10.0/video/video-result.png)
- [真实离线配音视频](validation-0.10.0/demo/video.mp4) · [完整示例制作包](validation-0.10.0/demo/video_bundle.zip)

结构化报告位于 `validation-0.10.0/video/video-browser-report.json`，既有流程报告在同目录的 `daily`、`scripts` 子目录；配音演示报告为 `demo/demo-report.json`。

## 安装与旧版升级

将新 wheel 安装到独立 Python 环境，再使用 `python -I` 运行全部安装检查，确认执行的是安装后的代码、模板和提示词。报告为 `validation-0.10.0/wheel-*.json`。

升级测试先由**安装的 0.9.0 wheel** 创建数据库、已审核旧稿、一段已完成视频及一项尚未执行的视频任务。随后安装 0.10 并直接打开原数据库和运行目录：

1. 为旧任务补充作品名称与“旧版新闻卡”标签，原脚本完整快照、制作输入和产物记录逐项相同。
2. 旧成片仍可下载与按范围读取，沿用原记录中的产物校验值。
3. 旧排队任务在新版本成功执行，保持原来的固定输入和旧版画面布局。
4. 新版本可生成新模板视频，作品查询、编辑、归档、恢复诊断及复用设置正常。

复现时先在 0.9 环境运行 `python -I tools/seed_upgrade_0_9.py /path/to/new-fixture`，再安装 0.10，运行 `python -I tools/smoke_video_workbench_installed.py --upgrade-fixture /path/to/new-fixture`。安装检查依赖本机 FFmpeg/FFprobe 和可用中文字体；可设置 `NEWS2DOUYIN_VIDEO_FONT`。

## 未覆盖

没有验证真实新闻检索覆盖率、模型语义引用充分性或稿件事实准确率；没有测试联网 Edge TTS 的可用性与专业配音效果。自动化成片主要使用 360×640 快速规格，未评测所有规格的长视频性能。没有验证多日驻留、PostgreSQL、完整浏览器矩阵或独立 RunDesk 客户端。Windows 和 macOS 仍需在部署环境确认系统编码器、字体与可选依赖。
