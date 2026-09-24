# 来源与许可

## 工作流参考

- [jinchenma94/social-media-data-tools](https://github.com/jinchenma94/social-media-data-tools)，检查版本 `2a00cc157c8d6718dbbcd7818813da22d03fbf86`。
- 参考点：豆包工作内置逐字稿能力、分页取全文、飞书妙记回退。本包指令与文档重新撰写，未复制其 Skill 正文或参考文档。该版本仓库未见 LICENSE 文件，因此不把它标注为 MIT，也不将其原文打包再分发。

## 第三方代码与思路

- [chubbyguan/chubbyskills](https://github.com/chubbyguan/chubbyskills)，检查版本 `63135c89e3562106529c48f6ebeeb6ed93ce279e`。
- `scripts/audio_fallback.py` 的移动分享页解析和 SenseVoice 调用根据以下文件改编：`douyin-transcribe/scripts/download_douyin_audio.py`、`chubby_common/funasr.py`。
- 原作者：Chubby。许可证：MIT，全文保留在 [licenses/chubbyskills-MIT.txt](licenses/chubbyskills-MIT.txt)。
- 本包修改：独立标准库下载、完整分享文本提取、多次跳转处理、限量下载、独占运行目录、原地址保留、空结果检查、结构化状态和哈希记录。不继承“CPU 比 GPU 识别质量低”“永久免 Cookie”等未经验证的结论。

## 模型与服务

SenseVoice、FunASR、FFmpeg、豆包工作及飞书是各自独立的项目或服务，遵循它们自己的许可和使用条件。本包不包含模型权重、Cookie、访问令牌或视频素材。

## 验证边界

2026-09-24 已在当前豆包工作环境用一条公开视频完成浏览器会话 + `yt-dlp` 取音频、mediakit Cloud ASR 取逐字稿、飞书创建和全文读回。它不能证明其他视频、其他账号或尚未使用的豆包内置取稿、飞书妙记、SenseVoice 路径可用；批量处理前仍应先用一条视频验证当前环境。
