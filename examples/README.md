# 离线视频示例

`video-demo.mp4` 是 0.4.0 的真实离线渲染样例，使用 eSpeak NG 机械音、中文标题卡及分段字幕。720×1280、24 fps，约 7.83 秒。此内容仅用于软件功能演示，不是真实新闻。

复现（先安装 video、voice-offline 依赖和 FFmpeg、中文字体）：

```bash
python -I tools/smoke_video.py --preset portrait --output-dir /path/to/demo
```

该工具会生成配音、编码成片，检查视频流/音频流各自时长、文件校验和及 HTTP Range，并保留示例 MP4、封面、SRT 和清单。
