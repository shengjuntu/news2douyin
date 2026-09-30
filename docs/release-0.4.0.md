# 0.4.0：本地视频制作闭环

本版从当前审核通过的脚本生成竖屏 MP4：准备来源快照和素材 → 配音 → 分段字幕 → 中文标题卡或上传图片 → H.264/AAC 编码 → 校验 → 预览/下载。视频任务复用现有数据库队列、租约、心跳、进度事件、取消、重试和恢复。

## 安装与运行

```bash
pip install -e ".[video,voice-offline]"
# 如需联网 Edge TTS，再安装：
pip install -e ".[tts]"
news2douyin-server --host 127.0.0.1 --port 18080
```

系统还需 FFmpeg、FFprobe（支持 libx264 和 AAC）及中文字体。程序不会在启动时自动安装系统软件。可以通过环境变量显式指定：

```bash
NEWS2DOUYIN_FFMPEG=/path/to/ffmpeg
NEWS2DOUYIN_FFPROBE=/path/to/ffprobe
NEWS2DOUYIN_VIDEO_FONT=/path/to/NotoSansCJKsc-Regular.otf
```

未配置字体时，程序检查常见 Windows/macOS/Linux 中文字体路径，再查询 fontconfig 的中文字体列表。环境检查会检测字体是否包含中文字形；渲染时也检查标题和字幕中的中文字形，避免悄悄输出方框。字体字节的 SHA-256 在任务提交时冻结；字体变化后需要重新提交任务。

`GET /api/video/capabilities` 和视频制作页面显示依赖检查结果。检查通过仅表示基础依赖存在；编码器构建、具体语音名称和网络连通性仍可能在执行时返回明确错误。

## 页面操作

1. 在脚本工作台保存并审核通过当前版本，点击“制作视频”。
2. 选择配音方式、语音、语速和画面规格。
3. 按需上传图片。图片按列表顺序分配到字幕片段，中心裁切到画面区域；没有图片时使用新闻标题卡。
4. 提交任务后进入结果页，可看进度、取消、重试，打开任务页查看持久化事件。
5. 完成后在页面预览视频，下载 MP4、WAV、SRT、封面、来源快照、清单或完整 ZIP。

程序不自动搜索图片、不抓取素材 URL、不调用图片生成服务，也不自动发布到抖音。图片需由使用者提供并确认使用权限。

## 配音和字幕方式

| 方式 | 行为和边界 |
|---|---|
| `espeak` | 安装 voice-offline；独立子进程调用 eSpeak NG，默认普通话 `cmn`。机械音适合功能验证、低成本预览 |
| `edge` | 安装 tts；默认 `zh-CN-XiaoxiaoNeural`，需要联网，正文会发送到所选 TTS 服务；本次未完成在线服务验收 |
| `uploaded` | 上传单/双声道未压缩 PCM WAV，并提供对应 SRT；可使用外部优质配音或人工录音 |

自动配音先按标点及长度拆分正文，每段分别合成，再转换成 24 kHz、单声道、16-bit PCM。字幕起止时间由真实 PCM 帧数累计得到，而不是按字数估算总时长。分段方式会影响句间停顿和韵律，属于最小流程，尚未提供长句上下文优化。

自动字幕是分段对齐，不是逐字强制对齐；画面切换有帧率量化偏差。实际成片时长由音频决定，脚本中的目标时长不会强制裁剪口播。

上传配音必须配套 SRT：正文需与审核稿一致（忽略空白），时间需有序、不重叠、不超过音频时长。程序不会自动转写上传音频，所以文字匹配检查不能证明上传音频实际说了这些话，仍需人工预览。

## 范围和限制

- 画面预设：360×640 / 12 fps、720×1280 / 24 fps、1080×1920 / 24 fps。
- 图片：每张最多 20 MiB、2400 万像素；解码后统一 RGB PNG、校正方向、最长缩放边界 2160×3840；单任务最多 12 张。
- 配音：PCM WAV 上传最多 50 MiB，0.2–600 秒、8–96 kHz，单/双声道。
- 正文：最多 6000 字符、300 个合成/字幕片段；总配音时长最多 600 秒。超限明确失败，不悄悄省略正文。
- 合成语速调整：-50% 到 +100%；语音名称须对应后端支持的语音。
- 基础画面为标题、图片或卡片、烧录字幕；没有运镜、视频片段剪辑、转场、背景音乐或自动分镜。
- 当前使用 CPU FFmpeg 编码，每个进程仍只有一个任务执行器，采集和视频共用队列。长视频会占用该执行器；尚未做独立队列或 GPU 编码。

## 可恢复执行

提交时先得到不可变的已审核脚本导出，再在相同审核状态版本上锁定并原子写入 TaskRecord 与 VideoProduction。若中途有人编辑或撤销审核，旧 expected_version 会被拒绝。任务入队后固定使用提交时的脚本、素材身份、文件哈希、字体和选项；后续改稿不会改变已经排队的视频。

自动配音每段完成就保存检查点，合并配音后再保存整体检查点。重试会校验检查点文件及 SHA-256，复用有效音频，重做丢失或损坏的检查点。经过校验的完整成片也有检查点，任务状态与产物登记在同一个数据库事务提交。

每次尝试使用独立目录，旧执行器失去租约后无法登记检查点或完成任务。取消会在轮询检查点生效，并终止运行中的配音/FFmpeg 子进程；正在解码图片、探测媒体或计算哈希时需等当前短步骤结束。正常进程退出与租约恢复沿用 0.2.0 的规则。

文件写入和数据库不是跨介质事务。突然中断可能留下未登记的临时目录或文件，不会显示成已完成结果。本版保留素材、失败尝试和检查点，没有自动清理策略，应备份数据库与整个 storage_root。

## 输出和校验

- `video.mp4`：H.264、yuv420p、AAC，faststart，烧录字幕。
- `narration.wav`：归一化后的实际配音。
- `subtitles.srt`：分段时间轴，可供后续编辑。
- `cover.png`：首帧封面。
- `script_snapshot.json`：所用审核稿及来源快照。
- `manifest.json`：脚本版本、任务和导出标识、输入哈希、配音方式、对齐方式、尺寸/帧率/实际时长、素材及产物 SHA-256。
- `video_bundle.zip`：上述全部文件。

编码后分别检查视频流和音频流时长、视频帧数、编码格式和分辨率，不仅检查 MP4 容器总时长。静态帧通过明确的尾帧延续覆盖剩余音频，避免最后一段画面提前结束。下载重新检查 SHA-256；未完成、文件丢失或校验失败不会作为正常成片返回。MP4 支持 HTTP Range，方便浏览器拖动播放。

## API 与 SDK

| 接口 | 用途 |
|---|---|
| `GET /api/video/capabilities` | 环境与配音后端检查 |
| `POST /api/video/assets` | multipart 上传，字段 package_key、kind（image/audio）、file |
| `GET /api/video/assets?package_key=...` | 当前脚本素材列表 |
| `GET /api/video/assets/{id}` | 预览标准化素材 |
| `POST /api/video/tasks` | 提交任务，返回 202 和 task_id |
| `GET /api/video/tasks?package_key=...` | 视频任务列表 |
| `GET /api/video/tasks/{id}` | 状态、固定脚本版本及可下载产物 |
| `GET /api/video/tasks/{id}/files/{name}` | 文件预览；加 download=true 下载 |
| `GET /api/tasks/{id}/events` 或 `/stream` | 通用事件和 SSE |
| `POST /api/tasks/{id}/cancel` 或 `/retry` | 通用取消和重试 |

提交示例：

```json
{
  "package_key": "pkg_...",
  "expected_version": 4,
  "idempotency_key": "video-request-001",
  "options": {
    "backend": "espeak",
    "voice": "cmn",
    "rate": 0,
    "preset": "portrait",
    "image_ids": []
  }
}
```

上传配音时设置 backend=uploaded、audio_id 和 subtitles（完整 SRT 字符串）。所用素材必须属于该脚本。相同幂等键和相同请求返回已有任务；不同请求复用同一键返回冲突。

SDK 增加 video_capabilities、upload_video_asset、submit_video、get_video、list_videos；已有 wait_task、cancel_task、retry_task 可用于视频。任务 JSON 新增 kind=collect/video。旧 TaskRecord 表未加列，视频通过新 VideoProduction 表关联，原有采集接口兼容。

## 升级与验证

升级前停止服务并备份数据库和运行目录。安装 0.4.0，使用原配置启动；只新增 VideoAsset、VideoProduction 两张表。下载源码 ZIP 可直接运行示例工具，wheel 包含服务和页面，但不捆绑 FFmpeg、字体或语音引擎二进制，额外依赖按需安装。

本地验证：

- 89 项自动化测试通过，含真实上传配音编码、字幕约束、并发入队、旧稿保护、子进程取消、逐段恢复、损坏音频检查点重建、无效字体检测、文件校验与 Range。
- 独立 wheel：18 项原有流程/页面检查，另有 8 项真实离线配音与视频检查。
- 生成 720×1280、24 fps、约 7.83 秒的中文演示视频；H.264 视频流 188 帧，音频约 7.82 秒。检查了开头与第 5 秒画面。
- 新视频页面及通用任务页 JavaScript 语法检查通过；未执行真实浏览器操作验收。
- 未连接在线 Edge TTS、真实新闻/LLM 服务；未执行远程 CI、Windows 真机或吞吐压测。

可运行 `python -I tools/smoke_video.py --preset portrait --output-dir /path/to/demo` 重现离线成片。仓库 examples/video-demo.mp4 为软件功能演示，不是真实新闻。

eSpeak NG 的 C API 接入依据：[官方 speak_lib.h](https://github.com/espeak-ng/espeak-ng/blob/master/src/include/espeak-ng/speak_lib.h)。语音库通过单独的 voice-offline 依赖提供，代码包不复制其二进制。

本版完成最小本地成片流程。高质量素材剪辑、长文本连贯配音、逐字对齐、Rundesk/Codex 编排、Go 视频 APP、LLM 改写/分镜和自动发布仍未实现。
