# 大帝的CYPHER挑战 AstrBot 版

这是从 [Amiya-Bot 插件开发](https://github.com/AmiyaBot/Amiya-Bot-plugins.git) 中的 `wordle2` game迁移出来的 AstrBot 独立插件，用于在会话中进行明日方舟干员竞猜。整个迁移/二创过程完全由Codex完成，我没写过一行代码。

## 功能

- 内置完整干员题库，当前包含 450 名干员，包含维什戴尔等数据。
- 支持 `普通` 和 `硬核` 两种难度。
- 支持群内/同会话接力竞猜。
- 支持插件内部本地 HTML 棋盘渲染，不再依赖 AstrBot 公共 `text2img` 节点。
- 支持 `提示`、`线索`、`跳过`、`下一个`、`结束`、`不玩了`。
- 公布答案时会揭晓本题全部线索，并保留展示已猜错干员的记录。
- 支持干员名、别名、短名和模糊匹配，例如当前答案是 `阿米娅` 时，发送 `阿米` 也可以命中。
- 支持额外别名表，`data/operator_aliases.json` 中的别名会在插件启动时自动合并进题库。
- 支持同音匹配，例如当前答案是 `阿米娅` 时，发送 `阿米亚` 也可以命中；完整拼音同音能力依赖 `pypinyin`。

## 指令

- `/猜干员`：开始游戏并选择难度。
- `/猜干员 普通`：直接开始普通模式。
- `/猜干员 硬核`：直接开始硬核模式。
- `/猜干员 干员名`：游戏已开始时，用指定干员名竞猜；当平台不稳定触发普通消息时推荐用这个格式。
- 支持自然语言触发，例如 `大帝挑战`、`大帝的CYPHER挑战`、`我想玩大帝挑战`、`我想玩大帝的CYPHER挑战`、`我想玩干员猜盐`、`明日方舟猜盐`。

游戏开始后，可以直接发送干员名或别名进行竞猜。如果直接发送没反应，使用 `/猜干员 干员名` 可以稳定触发命令处理。

## 本地渲染

插件现在自己使用 `Jinja2 + Playwright Chromium` 渲染棋盘，并把图片写到 `local_render_temp_dir`，默认是 `/AstrBot/data/temp`。

AstrBot 和 NapCat 分容器部署时，NapCat 也需要以相同路径挂载这个目录，例如：

```yaml
services:
  astrbot:
    volumes:
      - ./astrbot_data:/AstrBot/data
  napcat:
    volumes:
      - ./astrbot_data/temp:/AstrBot/data/temp:ro
```

在 AstrBot 容器里还需要安装 Playwright 浏览器：

```bash
python -m playwright install --with-deps chromium
```

如果 Chromium 下载慢，可以尝试：

```bash
PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright python -m playwright install chromium
```

如果本地渲染失败，插件会自动回退为纯文本棋盘。

## 依赖

插件目录包含 `requirements.txt`：

```txt
pypinyin>=0.50.0
jinja2>=3.1.0
playwright>=1.40.0
```

AstrBot 正常安装插件依赖后，会启用完整拼音同音匹配。即使依赖暂时不可用，插件也内置了 `亚`/`娅` 这类常见写法的兜底匹配。

## 配置

配置项在 `_conf_schema.json` 中定义：

- `round_timeout`：当前题目无人有效竞猜时的超时时间，默认 `120` 秒。
- `max_guesses`：每题最大竞猜次数，默认 `10` 次。
- `enable_image_board`：是否启用 HTML 图片棋盘，默认开启。
- `local_render_temp_dir`：本地棋盘图片输出目录，默认 `/AstrBot/data/temp`。
- `operator_data_file`：自定义干员数据 JSON 文件，留空时使用 `data/operators.json`。

## 自定义干员数据

插件会自动加载 `data/operator_aliases.json` 作为额外别名表。键可以写干员名、干员 ID 或已有别名，值必须是字符串数组；格式错误会在加载时直接报错。

```json
{
  "阿米娅": ["兔兔", "驴", "黑兔子"],
  "维什戴尔": ["EW", "益达", "维神"]
}
```

数据文件可以是数组，也可以是带 `operators` 字段的对象。每个干员至少需要：

```json
{
  "id": "char_example",
  "name": "示例干员",
  "rarity": 6,
  "classes": "近卫",
  "classes_sub": "领主",
  "race": "菲林",
  "nation": "谢拉格",
  "sex": "男",
  "team": "喀兰贸易",
  "group": "喀兰贸易",
  "drawer": "画师名",
  "aliases": ["别名"]
}
```

普通模式使用 `rarity`、`classes`、`classes_sub`、`race`、`nation`、`sex`。硬核模式会从普通字段以及 `team`、`group`、`drawer` 中随机抽 6 项。

## 更新题库

如需重新生成内置题库，可以把官方数据表放到 `data/raw`，然后执行：

```powershell
python scripts/build_operator_data.py
```

生成完成后只需要保留 `data/operators.json`，`data/raw` 不需要随插件发布。
