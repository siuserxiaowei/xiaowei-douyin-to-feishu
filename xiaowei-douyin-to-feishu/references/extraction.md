# 取稿与回退

## 内置工具

原参考项目使用豆包工作的 `web.fetch` 获取口播。先核实本次会话的工具说明和响应，不能把普通网页正文视为转写结果。

如果响应提供 `pagination_content`、`end_offset`、`total_length`，按工具实际结构取正文，从 offset 0 开始，以返回的 end_offset 继续，直到终点覆盖总长度。不要用本地 Python 字符数替代工具 offset（计数单位可能不同）。遇到 offset 不前进、总长度不一致或已声明结束但中间有缺口时，保存已取部分并换路径。

拼接时依据 offset 和边界文本处理工具产生的重叠，不能对整篇稿件做全局句子去重：说话人可能真的重复说了同一句话。

标题、视频介绍、章节要点、评论、摘要均不等于逐字稿。不要机械地把“第一个空行之后”全部当作口播；识别实际返回结构。无法区分时标记不确定并回退。

## 飞书妙记或已有转写能力

先发现当前环境支持的能力和参数。如果安装了 `doubao-video-extract`，读取其 Skill 后定位真正存在的提取脚本。原项目提及的 `scripts/minutes/social_video_to_minutes.py` 不是本包文件，也不是通用命令。

有可用转写链路时，下载/提取音频、提交一次、保存任务 ID、轮询直至成功或失败。遵循服务给出的间隔；超时保留任务 ID，恢复时查询原任务，不重复上传。只保存口播结果，不用妙记摘要代替全文。

录音上传、任务完成、取回全文是三个阶段。记录真实状态；没有授权或额度时报告具体缺口。对于新出现的付费购买、非预期外部服务，不能擅自操作。

## 本包下载脚本

要求 Python 3.10+、FFmpeg。主路径不需要安装本地语音模型。

在技能根目录执行，下方 URL 和路径仅为参数示例，执行时替换为真实值：

```bash
python3 scripts/audio_fallback.py download 'https://v.douyin.com/实际短链/' --output-dir ./runs
```

脚本会在输出目录下创建独立子目录，返回 JSON，包含 `audio_path`、视频 ID、标题、规范来源和音频 SHA-256。支持纯链接、含一个链接的分享文案以及 `/video/ID`、`/share/video/ID`、`modal_id`。

实现路径：短链跳转 → 视频 ID → iPhone UA 请求 `iesdouyin.com/share/video/ID` → 解析 `window._ROUTER_DATA` → 读取播放地址 → 下载临时视频 → FFmpeg 提取 16 kHz 单声道 WAV。

播放地址只用于本次下载，不写进飞书文档，也不持久保存带签名的 CDN 链接。保持平台返回的播放地址，不为转文字额外做去水印修改。

脚本不会登录、安装依赖或调用收费解析 API。默认单次下载上限 256 MiB，超出后说明原因；确有需要再显式设置 `--max-mb`。没有 `_ROUTER_DATA` 时可能是页面变化或风控，不能声称永久免登录。

## 可选本地转写

仅在 `funasr`、`modelscope`、`torch`、`torchaudio` 和模型下载条件已准备好时使用。首次下载可能较大；不要在已有豆包/妙记能力可用时自动安装整套环境。

```bash
python3 scripts/audio_fallback.py transcribe './runs/本次目录/audio.wav' --output-dir ./runs
```

使用 SenseVoice-Small、CPU、FSMN VAD，中文识别及 ITN；模型可能规范化数字表达。输出 `transcript.txt` 和 `result.json`，带模型来源、字符数、SHA-256 和“未逐字校对”标识，不生成说话人分离或逐词时间戳。模型初始化依赖第三方代码；首次启用遵循当前环境的模型信任规则。

空结果是“未取得逐字稿”，不能自行判成无口播。每条失败都保留记录并继续处理其他视频。
