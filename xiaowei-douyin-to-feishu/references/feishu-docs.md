# 飞书文档写入

优先使用豆包工作当前提供的飞书文档连接器，依据实际工具 schema 创建、读取和追加。这里的 CLI 是备用实现，不能假定所有豆包工作环境已经安装或登录。

## 已有逐字稿直接续跑

本技能默认交付飞书文档。只得到本地 `transcript.txt` 时，先查看同目录 `run.json`，确认其中有 `video_id`、`source_url_canonical`、`transcript_file`、`transcript_status: complete`、字符数及 SHA-256。若当前环境已有获授权的 `lark-cli`，在技能根目录运行：

```bash
python3 scripts/publish_feishu.py '/实际路径/run.json'
```

脚本把单条视频运行记录映射为保真正文，创建飞书文档，再分别回读页面发布文案（若 `run.json.description` 存在）和逐字稿全文核对。已有 `feishu_doc` 时只回读，不重新创建。创建前会在 `run.json` 记录尝试；响应不确定时不能盲目重试，先到飞书确认已有文档，再运行 `python3 scripts/publish_feishu.py '/实际路径/run.json' --existing-doc '实际文档URL'`。指定已有文档时该脚本只核对正文，**不会追加**；需要追加时按下文的先读、去重、追加流程执行。

只有脚本输出 `feishu_write_status: verified_full_text` 并返回真实文档 URL，才可报告已完成。脚本失败时保留运行记录及原稿，按错误继续处理认证、权限或缺字问题；不能把本地文件改称为飞书交付。

## 目标与身份

- `docx` / 文档类型 `wiki`：读取并确认真实对象。已有目标默认追加；同视频 ID 已存在且正文完整时复用。补缺段或刷新已有稿件需要用户对应意图，不覆盖无关内容。
- 文件夹 / 知识库父节点：使用工具解析出的父 token 创建子文档，不靠 URL 外形猜 token。
- 没有目标：用用户当前可写的默认空间创建文档。若环境确实无法确定空间，先完成本地正文，再问缺失位置。
- 使用当前用户身份。不要为了写入成功切到不明机器人或测试账号，也不改文档对外权限。

## 使用 lark-cli 时

先运行当前环境的 `lark-cli docs +create --help` / `+update --help` / `+fetch --help`，确认命令存在。需要认证时按 CLI 的授权流程处理，不输出凭据。

以下命令用于导入已形成的保真 Markdown 原稿；如果当前环境的飞书技能要求其他创作流程，遵循其实际说明。原始口播的 `<`、`[`、`*`、`$` 等符号必须按 Markdown 文本转义。本包生成器会处理；不可把抓取来的 HTML 作为可执行富文本导入。

在正文所在工作目录执行：

```bash
# 导入完整稿件。TITLE 替换成真实标题；正文中不再放重复 H1。
lark-cli docs +create --as user --doc-format markdown --title '真实标题' --content @./document.md

# 在确认过的父位置创建时，增加 --parent-token '实际父token'。

# 写入已有文档前读取当前正文，用视频 ID 检查是否已存在。
lark-cli docs +fetch --as user --doc '实际文档URL或ID' --doc-format markdown --detail with-ids

# 确认本次视频尚未写入后，在文末追加。
lark-cli docs +update --as user --doc '实际文档URL或ID' --command append --doc-format markdown --content @./document.md

# 写后读回；长文可按章节读取，覆盖全部新增正文。
lark-cli docs +fetch --as user --doc '返回的真实文档URL或ID' --doc-format markdown --detail with-ids
```

正文通过文件或 stdin 传入，不把外部文本拼进 shell 命令。文件路径优先使用当前工作目录内的相对路径。

## 判断成功

CLI JSON 顶层 `ok: true` 表示调用成功，仍需看 `data.warnings`、更新的 `data.result` 是否为 `partial_success`，以及真实返回的 `data.document`。用返回值记录文档 ID/URL，不能猜链接。接口返回结构改变时以当前说明为准。

如果服务返回的是异步任务 ID，保存任务 ID，按实际工具给出的查询方式等待最终结果；提交成功不等于文档已经创建完成。不要把任务 ID 当作文档 ID。

创建后有局部失败时修补同一文档，不能重新创建整篇。创建响应丢失且无法确定是否成功时，保存待核实状态并定位已有文档；在确定前停止该次写操作，避免重复文档。

对长文，在服务实际限制内按完整段落分批，保存每批首尾边界及写入结果。不截断原稿，不静默省略后半段。追加超时或结果不明时先读回，逐段确认哪些内容已写入后只补缺失部分。

全文核验时，从回读中只取对应逐字稿章节，解读 Markdown 后比较原文字序；允许空白和格式差异，不允许文字缺失、增写、重排。注意 fetch 的 Markdown 已转义：若用于再次写入，不能先反转义后原样回写。只取了目录、工具截断或抽查片段时，状态必须是“写入待完整核验”。

## 失败交付

飞书无法写入时保留 `document.md`、核验清单及运行记录，交付本地文件，并说明需要哪个账号授权/哪个目标权限。以后恢复先定位原文档或原写入进度，不重新采集全部素材。
