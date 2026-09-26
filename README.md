# 边狱巴士(Limbus Company)自动汉化工具

第十章的文本量使得零协会遭受西西弗斯之刑，但是作者非常想看新剧情，遂（让 AI）写了此项目。

使用 BYOK 自动翻译边狱巴士未汉化的内容。仅依赖 Python 3.10+ 标准库，无需安装任何第三方包。

本项目的使用需要安装零协会最新汉化，脚本会自动读取未汉化内容进行 AI 翻译（不安装也行，那就是翻译整个游戏文本，Token 杀手）。

## 使用方式（普通玩家）

从 [Release](https://github.com/Noplch0/limbus-ai-localization/releases) 页下载最新版本的压缩包：

1. 解压到 `<你的游戏目录>\LimbusCompany_Data\Lang\<你命名的文件夹>` 中
   （zip 根目录就是语言包内容，直接解压即可，无需再建子文件夹）；
2. 将零协会汉化文件（一般在 `Limbus Company\LimbusCompany_Data\Lang\LLC_zh-CN` 中）
   复制放进你解压的目录中。与其他覆盖安装不同的是，该步若提示有文件冲突，
   需要选择**跳过**而不是覆盖——包内同名文件是"官方已有译文 + AI 补译"的更完整版本；
3. 启动游戏，在标题界面左下角选择自定义语言里选择你命名的文件夹名即可。

想还原时，在标题界面切回原语言，或直接删除你命名的文件夹；零协汉化包全程不会被改动。

压缩包内附《使用说明.txt》，内容与本节一致。

## 从源代码运行

本项目 release 使用 deepseek-v4.1-flash 进行翻译，如果你想用其他模型翻译可参考下方运行方式，使用 OpenAI 兼容格式。

1. **配置 BYOK**：首次运行会自动生成 `config.json`，填入 `api` 三项：

   ```json
   "api": {
     "base_url": "https://api.deepseek.com/v1",
     "api_key": "sk-...",
     "model": "deepseek-v4.1-flash"
   }
   ```

   任何 OpenAI 兼容服务均可（DeepSeek、OpenRouter、硅基流动、本地 Ollama 等），`base_url` 填到 `/v1` 这一级。密钥也可以不改文件，改用环境变量 `LIMBUS_LLM_KEY`。

2. **测试连通性**：运行 `python limbus_loc.py test` 确认 API 可用。

3. **扫描并翻译**：

   ```bash
   python limbus_loc.py run
   ```

   游戏路径留空时会自动从 Steam `libraryfolders.vdf` 探测；找不到就在 `config.json` 的 `game_path` 手动填入游戏根目录。正式翻译前建议先加 `--dry-run` 估算批次与费用。

4. **让游戏生效**：把 `<游戏>\LimbusCompany_Data\Lang\config.json` 的 `"lang"` 改为 `"ai"`（想换回中文包就改回 `"LLC_zh-CN"`），或者按上一节的方式把 `ai/` 内容与零协包一起放进自定义文件夹。

   每次翻译完成后，脚本会自动把官方中文包 `LLC_zh-CN` 合并进 `ai/`（只读官方包，不会改动它），因此 `ai` 是**完整中文语言包**：已有官方译文的文件用官方版，官方尚未翻译或条目落后的新内容用 AI 补译。不想自动合并时，把 `config.json` 的 `merge_official` 设为 `false`；也可随时手动执行 `python limbus_loc.py merge`。

## 命令

```
python limbus_loc.py scan       # 找出无中文对应或官方包落后的 KR_ 文件，复制到 ./untranslated/
python limbus_loc.py test       # 测试大模型 API 连接性（一次最小请求）
python limbus_loc.py translate  # 翻译 ./untranslated/ 全部文件 → Lang/ai/
python limbus_loc.py merge      # 把官方中文包 LLC_zh-CN 合并进 ai/（translate 后自动执行）
python limbus_loc.py run        # scan + translate（+ 合并）
python limbus_loc.py pack       # 把补译文件打包成可分发 zip → ./dist/（含使用说明）
python limbus_loc.py help       # 查看所有命令与说明（help <命令> 看详情）
```

通用选项：

- `--file <子串>` 只处理相对路径含该子串的文件，如 `--file KR_S1017B`、`--file RPGSystem`
- `--force` 忽略缓存，所有字符串重新翻译
- `--cold-cache` 同样忽略全部缓存；改术语表后一般不必用（见下）
- `--dry-run` 只统计（提取了多少条、会调用多少次 API、哪些字段不在白名单），不花钱不写文件

## 游戏更新后怎么办

重新执行 `python limbus_loc.py run` 即可：

- `scan` 会重新对比，复制**仍然没有中文对应**以及**官方包缺条目（内容落后）**的文件；
- 落后文件会先套用官方已有译文，只把新增韩文送去翻译；
- `translate` 走字符串级缓存，没变的句子不重复计费，只有新增/改动的内容会调用 API；
- 官方包后来补齐的文件，合并时会用**官方译文覆盖** `ai/` 里的 AI 版本；官方仍缺条目时改为条目级叠加，保留 AI 补译（AI 译文仍保留在缓存中，随时可查）；
- 零协补翻后，`scan` 会自动把该文件移出 `untranslated/`，后续翻译与 `pack` 都不再涉及它。

## 打包分发

```
python limbus_loc.py pack            # 输出 dist/LimbusCompany_AI_CN_日期.zip
python limbus_loc.py pack --out D:\tmp\mypack.zip
```

zip 根目录即语言包内容（解压到 `Lang\<自定义文件夹>`，再把零协包复制进去、冲突选跳过即可，见"使用方式"）。zip 内只包含**本工具翻译的文件**（即 `untranslated/` 与 `ai/` 的交集，官方包已有的普通文件不会被打包），外加一份《使用说明.txt》（安装、还原、兼容性、免责声明）。若零协（LLC_zh-CN）后来补翻了某文件，该文件会自动从包中排除，避免把零协译文转发出去。绝不打包 `config.json`（含密钥）、`cache/`、`logs/`、`untranslated/` 或脚本本身，可直接分享给其他玩家。

新包生成后可通过 GitHub Release 发布（`gh release create` 附上 zip），玩家即可在 Release 页下载。

## 防止误翻资源名

部分 JSON 值虽是韩文，但被游戏当作**资源标识符**使用（典型：`model` 字段里的角色名 `"이스마엘"`），翻译会导致游戏找不到资源。因此本工具采用**字段白名单**策略：只有字段名在 `translatable_keys.json` 中、且值确实含韩文的字符串才会被翻译；其余一律保留原文。

含韩文但不在白名单的字段会汇总到 `logs/unknown_keys.json`（字段名、示例值、出现的文件数）。人工确认是正文后，把字段名加进 `translatable_keys.json` 的 `keys` 数组再重跑即可；因为翻译缓存按"字段名+原文"去重，重跑只为新增内容付费。

## 术语与文风

`glossary.json` 是韩→中固定译名表（罪人名字、人格/E.G.O/异想体/镜像迷宫、沉沦/烧伤/流血等战斗关键词），会作为强约束术语表注入系统提示词，译文要求符合 Project Moon 世界观与官方简中文风。

`scan` / `translate` 还会从官方中文包自动抽取 `speaker` / `teller` / `name` 等短译名到 `cache/glossary_official.json`，与用户表合并后按**当前批次实际出现的术语**注入提示词（用户表优先）。

改 `glossary.json` 后**不必** `--force`：缓存键包含原文命中的用户术语，相关条目会自动失效重翻。译文里若仍含已废弃错译（见该文件的 `_invalidate_if_contains`），加载缓存时也会剔除。只有想无视全部缓存时才用 `--force` 或 `--cold-cache`。

Ctrl+C 会立即保存已完成的缓存并退出；重跑自动续翻。

## 费用与稳定性

- 每 30 条或 2400 字符为一批请求，并发、批大小、重试次数均在 `config.json` 调节；
- 翻译结果会校验：返回数组长度一致、`[...]`/`<style>` 标签与换行原样保留，不合格自动重试，仍失败则回退原文并记入 `logs/failures.json`（文件保持完整可玩）；
- 中断（Ctrl+C）后缓存已保存，重跑自动续翻。

## 常见问题

- **游戏里没显示自定义语言 / 部分文本不是中文**：确认压缩包解压到了 `Lang\` 下你命名的文件夹（而不是 `Lang` 根目录），零协包文件已复制进去且冲突时选了"跳过"；游戏更新后的新增文本不在包内，会显示默认语言，等 Release 更新即可。
- **401/403**：API 密钥无效，检查 `api_key` 或 `LIMBUS_LLM_KEY`。
- **429 频繁**：调低 `concurrency`（如 2）或调小 `batch_max_strings`。
- **部分服务不支持 `response_format`**：脚本会自动退回普通模式；也可把 `api.json_mode` 设为 `false`。
- **输出 JSON 与中文包格式不一致**：游戏按 JSON 解析，缩进差异不影响加载。

## 免责声明

本项目与 Project Moon 及零协汉化组（LLC）无关；AI 译文非官方翻译，可能存在误译，仅供交流学习，请支持官方与零协的正式翻译。
