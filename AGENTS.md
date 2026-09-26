# AGENTS.md

边狱巴士(Limbus Company)自动汉化工具:单文件 Python 脚本,扫描游戏韩文资源中
零协汉化组(LLC_zh-CN)未覆盖的内容,调用 OpenAI 兼容大模型 API 翻译为简体中文,
与零协译文合并成完整语言包输出到游戏 `Lang/ai/`。仅依赖 Python 3.10+ 标准库,
无任何第三方包。

## 常用命令

- 语法检查: `python -m py_compile limbus_loc.py`
- 命令总览: `python limbus_loc.py help`(help <命令> 看单命令详情)
- 连通测试: `python limbus_loc.py test`
- 扫描语料: `python limbus_loc.py scan`(清单写入 logs/scan_report.json)
- 翻译:     `python limbus_loc.py translate [--file 子串] [--dry-run]`
- 一键流程: `python limbus_loc.py run`(scan + translate + 自动合并零协包)
- 打包分发: `python limbus_loc.py pack [--out 路径]`(输出 dist/*.zip;
  zip 根目录即语言包内容,附《使用说明.txt》,发布用 `gh release create` 附上 zip)
- 无 pip install;正式翻译前先 `--dry-run` 估算批次与费用

## 项目布局

- `limbus_loc.py` — 全部逻辑(单文件,约 1400 行)
- `glossary.json` — 用户术语表;`_invalidate_if_contains` 列出的废弃错译会触发缓存剔除
- `translatable_keys.json` — 可翻译字段白名单(44 个;`model`/`id` 永不翻译)
- `config.json` — 运行配置,**含明文 API key**(缺失时由 config.example.json 自动生成)
- `cache/translations.json` — 字符串级翻译缓存;`cache/glossary_official.json` — 从零协包自动抽取的译名表
- `untranslated/` — 语料副本;`logs/` — 各类报告;`dist/` — 分发包输出
- 游戏侧(经 Steam libraryfolders.vdf 自动探测):`kr/` 与 `Lang/LLC_zh-CN/` 只读,
  翻译输出到 `Lang/ai/`;游戏内把 `Lang/config.json` 的 `"lang"` 改为 `"ai"` 生效

## 硬性约束(改代码前必读)

1. **绝不写 `kr/` 与 `LLC_zh-CN/`** — 全程只读;零协译文不得被修改或转发
   (pack 会自动排除零协已完整覆盖的文件;零协包未安装时 scan/translate/pack
   自动降级为全量翻译,`locate_game_dirs(..., require_zh=False)` 返回 zh=None)
2. **零协译文永远优先于 AI 译文** — 落后文件条目级叠加(零协旧译保留,AI 只补缺);
   零协补齐后整体回填替换 AI 版
3. **`model`/`id` 是资源标识符,禁止翻译** — 仅白名单字段且值含韩文才送翻
4. 缓存键 = sha1(字段名 + 原文 + 命中的用户术语);改 glossary.json 相关条目自动失效,
   无需 `--force` 全量重翻
5. 译文必须过校验:标签/占位符/换行数与原文一致、无残留韩文;失败自动重试,仍败回退原文
6. 名称类字段(speaker/name 等)整条命中术语时直接套官方译名,不经模型

## 更新时序(游戏更新 + 零协跟进)

- scan 按 id/key 集合判定:缺失(新文件)与落后(缺条目);不看句子内容,
  零协改写旧句不会触发重翻,终态由零协更新后 merge 自动回填
- merge:官方完整文件整体采用零协版,落后文件条目级叠加;
  `untranslated/` 中零协已覆盖或韩文源已删除的文件自动清理

## 代码风格

- 仅标准库;JSON 输出 `ensure_ascii=False, indent=2`(缓存 indent=1)+ 换行结尾
- 配置读取用 `utf-8-sig` 容错(BOM);缓存写盘 = 临时文件 + `os.replace` 原子替换,每批落盘
- 用户可见文案与注释均为简体中文
- Ctrl+C:保存缓存后 `os._exit(130)` 立即退出,不被线程池拖住

## 测试方式

- 无正式测试框架。改逻辑后:`py_compile` → 真实数据 `scan` 回归 → 需要时用沙盒模拟:
  import limbus_loc 后 monkeypatch `UNTRANSLATED_DIR` / `LOG_DIR` /
  `OFFICIAL_GLOSSARY_PATH` 到临时目录,配假游戏目录跑 cmd_scan/merge/pack,
  **严禁**让沙盒写入真实 cache/、logs/、untranslated/
- 真实游戏路径:`D:\Program Files (x86)\Steam\steamapps\common\Limbus Company`

## 安全

- `config.json` 含明文密钥,已在 .gitignore;任何输出只显示密钥首尾片段
- 远程仓库:git@github.com:Noplch0/limbus-ai-localization.git(分支 main);
  面向玩家的分发渠道是 GitHub Release(附 dist zip),不是仓库本身
