# 抖音取稿与回退

## 两种不同的文字

- `description`：作者在抖音页面发布的标题、简介或配文。浏览器页面、内置取稿结果或 yt-dlp 元数据可以提供；没有就标“未获取”。
- `transcript`：视频口播的逐字稿。只能来自能返回口播的内置工具、已有字幕或实际 ASR。页面配文、章节要点、评论、AI 摘要都不能冒充口播。

用户只说“提取抖音文案”时同时查这两项，并在结果中分别标明。不要把两项简单拼成一段“完整文案”。

取得口播后必须按 [`terminology-review.md`](terminology-review.md) 做专业术语专项复核，尤其检查人名、产品/模型名、行业概念、缩写、数字与单位。术语复核只做有证据的局部纠错；ASR 原稿必须保留，疑似项不能凭常识擅自改写。复核状态、证据和最终稿哈希需同步到运行记录。

## 快速路径：豆包工作内置 web.fetch

[social-media-data-tools 的抖音采集说明](https://github.com/jinchenma94/social-media-data-tools/blob/main/skills/douyin-transcript-exporter/references/douyin.md) 使用的是豆包工作内部能力；其仓库没有提供 web.fetch 解析代码。先查看本会话真实暴露的工具及 schema。不存在、只返回普通 HTML 或结果不含口播时，直接转音频路径。

对每个规范视频 URL：

1. 首次调用：`web.fetch(url=视频URL, pagination={"offset":0,"limit":4000})`。按当前工具真实参数格式执行，不把示例当作外部 Python API。
2. 从返回的 `pagination_content` 读取本页文本及 `start_offset`、`end_offset`、`total_length`。保存原始页。只有工具明确给出正文时才算读到稿件；标题、简介、摘要不算。
3. 当 `end_offset < total_length`，用原样的 `end_offset` 作为下一次 `pagination.offset`。必须看到 offset 前进；发现跳号、回退、重叠不明或 total_length 改变时记录异常，不能悄悄跳过。
4. 到 `end_offset >= total_length` 才结束。按工具给出的 offset 范围拼接本页文字；边界若有重叠，只处理该边界，不能全局删除重复句，因为作者可能真的重复说话。offset 的单位由工具决定，不能用 Python 字符数代替。
5. 检查拼接结果中哪些是标题/发布文案、哪些是口播。上游示例以第一个空行分隔，但只有实际返回结构符合“标题/配文 + 空行 + 口播正文”时才能这样拆；结构变化时按真实字段或其他证据辨认。保留两份原文和来源，不靠模型补齐。

空白结果、只含页面配文、分页未到终点、占位说明或明显截断都不是完整口播。短于 50 字、无结束标点、出现“获取”等词只触发人工复核；它们本身不能证明失败。若内置工具只支持部分视频，失败后用下一条真实可用的路径，不反复尝试已知不支持的工具。

## 音频路径：浏览器会话 + yt-dlp + ASR

2026-09-24 在当前豆包工作环境的一条公开视频上，此路径取得 142 段、1241 字的口播并成功读回飞书；相比之下，内置 web.fetch 未暴露，doubao-video-extract 拒绝 douyin.com，旧移动分享页解析失败。此证据只覆盖当时的视频和账号，不保证其他视频均可访问。

1. 用当前浏览器展开分享短链，记录真实视频 URL / ID；需要访问限制内容时由用户在真实浏览器完成登录，不索取 Cookie 明文。
2. 检查 `yt-dlp`、`ffmpeg`。在技能根目录运行 `python3 scripts/audio_fallback.py download '视频URL' --output-dir ./runs --cookies-from-browser chrome`，其中浏览器参数按本机实际会话选择。脚本优先用 yt-dlp 元数据取得可用的 `description`，再下载可解码媒体并提取 WAV。不要把签名 CDN URL、Cookie 或请求头写入结果。
3. 检查当前豆包工作是否提供 `mediakit-cli video asr-subtitles` 或等效 Cloud ASR。先查该工具帮助/schema，再提交本地音频；保存任务 ID，等待状态完成，读取全部分段 JSON。组装脚本接受的 mediakit 结构是顶层 `status: completed`、`task_id` 与 `subtitles` 数组，其中每段有以秒计的 `start_time`、`end_time`、`subtitle_text`。当前工具若包了一层或字段名不同，先按真实响应提取并映射这些字段，不猜时间单位；不能把摘要当作字幕。
4. 对已取得的 `asr_result.json`，把下载脚本同次产生的 `result.json` 作为元数据传入组装脚本，例如 `python3 scripts/assemble_asr.py 'asr_result.json' --metadata '下载目录/result.json' --output-dir '新的组装目录'`。脚本按字幕时间顺序生成 `transcript.txt` 和可续跑的 `run.json`，保留 `description`，检查首尾覆盖和可疑间隔。`duration_seconds` 必须来自 yt-dlp、提取的音频或其他独立媒体元数据；ASR JSON 自身的 `duration` 可能与字幕末端不同，不能拿它冒充视频时长。时间轴较长空白不必然是漏字，按音频核实前标为部分/待核实。ASR 的同音字、数字和断句可能有误，输出标为“未逐字校对”。

已拿到音频或任务 ID 时先续跑对应阶段，不重复提交同一段云端识别。页面 `description` 缺失或明显截断时可对照浏览器可见原文，仍无法取得就标注缺失，不能从口播反推作者发布文案。若当前没有任何 ASR 能力，可保留音频和元数据，但不能声称取得口播全文。当前环境没有 mediakit 时，也可以使用已授权且实际可用的飞书妙记或其他转写工具；先验证它支持抖音来源或本地媒体。

ASR 分段中的时间戳应保留到复核环节，用来定位专业名词和回听片段。组装只证明字幕顺序与时间覆盖，不能证明术语写对；术语复核也不能替代全文完整性检查。

`scripts/audio_fallback.py` 的旧移动分享页解析只在没有 yt-dlp 时尝试，2026-09-24 已观察到 _ROUTER_DATA 不兼容。`scripts/audio_fallback.py transcribe` 调用本地 SenseVoice，需要 FunASR、ModelScope、PyTorch、torchaudio 和模型；默认不安装这些大型依赖。

## 运行状态和交付证据

每条视频保留：原链接、规范 URL、视频 ID、作者/发布时间（若取得）、`description`、实际取稿路径、原始工具结果、`transcript.txt`、字符数和 SHA-256、分页终点或 ASR 任务 ID、完整性判断及失败原因。互动数字不是取稿必要条件，不要为了它阻塞单条视频测试。

- “内置取稿全文”：分页范围连续并到终点，正文确实是口播。
- “ASR 全文已取回，未逐字校对”：识别任务完成，字幕覆盖首尾，无明确缺段。
- “部分内容”：分页或字幕有明确缺口，保留已取得原文并说明范围。
- “未取得逐字稿”：只有配文/摘要，或视频访问、音频/ASR 失败。
- “确认无口播”：实际检查音频或可信工具结果确认；不能由空返回或标题推断。

测试提取时给用户完整发布文案和逐字稿，分别标注来源与状态。若后续要写飞书，复用同一运行记录，不再下载和识别。
