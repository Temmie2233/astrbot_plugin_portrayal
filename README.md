
<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_portrayal?name=astrbot_plugin_portrayal&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_portrayal

_✨ 人物画像插件（本地增强版）✨_

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![作者-Zhalslar](https://img.shields.io/badge/作者-Zhalslar-blue)](https://github.com/Zhalslar)
[![Fork-Temmie2233](https://img.shields.io/badge/Fork-Temmie2233-blue)](https://github.com/Temmie2233/astrbot_plugin_portrayal)

</div>

> 本仓库基于 [Zhalslar/astrbot_plugin_portrayal](https://github.com/Zhalslar/astrbot_plugin_portrayal) v1.1.7 做本地增强。
> 上游自 v1.1.5 起移除了内置 t2i，长文本会直接发送；建议搭配 [输出增强插件](https://github.com/Zhalslar/astrbot_plugin_outputpro) 自动转成图片。

## 💡 介绍

根据群友的聊天记录，调用 LLM 分析群友的性格画像。

本仓库在保留上游功能的基础上，增加了分类型画像、查看画像、使用限制、缓存复用和人格恢复修复。

## 本仓库增强

- **分类型保存**：`画像`、`正画像`、`负画像`、`找对象` 的结果分别保存，互不覆盖；`克隆人格` 只写克隆提示词，不再覆盖画像。
- **查看画像**：`查看画像 [正/负/逆/找对象] @群友` 查看对应类型正文；`逆` 是 `负` 的查看别名。
- **画像帮助**：`画像帮助` 动态列出当前配置的命令、权限和用法。
- **使用限制**：用户冷却、每日次数、同目标次数、群/全局并发、缓存复用。
- **人格恢复**：切换人格时保存原 `conversation.persona_id`，恢复时还原；可选删除克隆人格。
- **昵称/头像同步**：改为可配置项，默认关闭。

## 📦 安装

- 本仓库为手动安装版本，克隆到 AstrBot 插件目录：

```bash
cd /AstrBot/data/plugins
git clone https://github.com/Temmie2233/astrbot_plugin_portrayal

# 重启 AstrBot 或重载插件
```

- 上游原版可在 AstrBot 插件市场搜索 `astrbot_plugin_portrayal`。

依赖：`aiohttp`、`Pillow` 等由 AstrBot 自带；如缺少依赖，AstrBot 会根据 `requirements.txt` 自动安装。

## ⚙️ 配置

面板：插件管理 -> `astrbot_plugin_portrayal` -> 操作 -> 插件配置。

### 消息查询

| 配置 | 默认 | 说明 |
|:---|:---:|:---|
| `default_query_rounds` | 30 | 默认查询轮数，每轮 200 条群消息 |
| `max_msg_count` | 500 | 单个目标最多分析多少条消息 |
| `cache_ttl_min` | 120 | 消息缓存过期时间（分钟） |
| `protected_user_ids` | `[]` | 不允许被画像的 QQ 号；这些用户的发言也不会写入消息缓存 |

### 使用限制

| 配置 | 默认 | 说明 |
|:---|:---:|:---|
| `cooldown_seconds` | 600 | 每用户冷却秒数，所有生成命令共用；0 = 关闭 |
| `daily_limit` | 5 | 每用户每天成功生成次数；0 = 不限制 |
| `target_daily_limit` | 2 | 同一用户每天查询同一目标的次数；0 = 不限制 |
| `reuse_hours` | 12 | 同目标同类型复用缓存的时长；0 = 不复用 |
| `group_concurrency` | 1 | 每群同时运行的画像任务数，超出的排队 |
| `global_concurrency` | 2 | 全局同时运行的画像任务数 |
| `admin_exempt` | true | 管理员豁免冷却和配额；并发限制仍生效 |

- 限流状态保存在内存中，重载或重启插件后清零。
- 缓存命中不会消耗冷却、每日次数或同目标次数。
- 每日次数按本地日期计算，跨天自动重置。

### 人格切换

| 配置 | 默认 | 说明 |
|:---|:---:|:---|
| `sync_bot_profile` | false | 切换人格时同步 bot 的 QQ 昵称/头像。账号级全局生效，会影响所有群 |
| `delete_clone_persona_on_restore` | false | 恢复人格时删除本次切换使用的克隆人格 |

## ⌨️ 使用说明

- 提示词命令（`画像` / `正画像` / `负画像` / `找对象` / `克隆人格`）、`查看画像`、`画像帮助` 可以直接发送。
- `切换人格`、`恢复人格` 是标准命令，需要 `/` 前缀或先 @bot 唤醒。
- `@群友` 必须是真实艾特；轮数可省略，省略时使用 `default_query_rounds`。
- 生成类命令有冷却和次数限制；同一目标同一类型在缓存期内会直接返回结果。

## ⌨️ 指令表

### 提示词命令（可在插件配置中自定义增删）

| 指令 | 默认权限 | 说明 |
|:---:|:---:|:---|
| `画像 @群友 [轮数]` | 所有人 | 综合性格画像（含优点/缺点/相处建议） |
| `正画像 @群友 [轮数]` | 所有人 | 偏优点向的画像 |
| `负画像 @群友 [轮数]` | 所有人 | 偏缺点向的画像 |
| `找对象 @群友 [轮数]` | 所有人 | 基于聊天记录的伴侣匹配建议 |
| `克隆人格 @群友 [轮数]` | 管理员 | 生成可用于「切换人格」的 system prompt |

> 权限由提示词条目的 `need_admin` 控制，可在面板修改。

### 内置命令

| 指令 | 权限 | 说明 |
|:---:|:---:|:---|
| `查看画像 [正/负/逆/找对象] @群友` | 所有人 | 查看本地已保存的对应类型画像；不带类型显示综合画像 |
| `画像帮助`（别名 `/画像help`） | 所有人 | 显示当前可用指令、权限和前缀说明 |
| `切换人格 @群友` | Admin | 把当前会话切到该群友的克隆人格。前置：需先执行「克隆人格」 |
| `恢复人格` | Admin | 恢复切换前的人格和对话历史；可按配置删除克隆人格 |

### 查看画像说明

- `查看画像 @群友`：综合画像（`portrait`）
- `查看画像 正 @群友`：正画像
- `查看画像 负 @群友` 或 `查看画像 逆 @群友`：负画像（`逆` 仅用于查看，生成请用 `负画像`）
- `查看画像 找对象 @群友`：找对象结果
- 对应类型没有生成过时，会提示先运行对应命令。
- `查看画像` 同时支持 `/查看画像` 和直接发送。

### 管理员强制刷新

生成命令后加 `强制`、`刷新` 或 `force`，可以跳过 12 小时缓存重新生成，例如：

```text
负画像 强制 @群友
```

该参数仅管理员可用。

## 缓存与配额

- 每个类型有独立的更新时间；缓存期内直接返回正文，不重新扫群、不调用 LLM。
- 配额只在真正执行生成并成功后扣除。
- 管理员默认不受冷却和配额限制，但仍占用群/全局并发槽位。
- 想完全关闭某项限制，把对应配置设为 0；并发上限最低为 1。

## 🎭 人格切换说明

- 切换前会保存当前会话的原 `conversation.persona_id`；`恢复人格` 会恢复该值（原本没有会话级人格时恢复为空）。
- 克隆人格 ID 为 `portrayal_{QQ号}`，重复克隆会更新同一个人格。
- 昵称/头像同步默认关闭；开启后会把 bot 账号的昵称和头像改成目标群友的，**对所有群生效**。
- 恢复失败时会保留原始资料缓存，便于重试。
- 开启 `delete_clone_persona_on_restore` 后，恢复人格时会一并删除本次使用的克隆人格。

## 🔐 隐私说明

- `protected_user_ids` 中的用户不会被画像，其发言也不会写入消息缓存。
- 画像与克隆提示词保存在插件数据目录的 `portrayal.json`；消息缓存在 `cache/message_cache.json`，会按 `cache_ttl_min` 过期清理。
- 画像由 LLM 根据聊天记录生成，可能存在偏差；开放给群友使用前建议先告知并获得理解。
- 建议至少把 bot 自己和管理员加入 `protected_user_ids`。

## 效果图

![download](https://github.com/user-attachments/assets/988e7cc1-92d1-48c9-8d95-cf83e802bfc9)

## 上游与致谢

- 原项目：[Zhalslar/astrbot_plugin_portrayal](https://github.com/Zhalslar/astrbot_plugin_portrayal)
- 原作者：Zhalslar
- 本仓库仅维护本地增强，使用问题请在本仓库提交 Issue。

## 👥 贡献指南

- 🌟 给原项目和本仓库点个 Star
- 🐛 提交 Issue 报告问题
- 💡 提出新功能建议
- 🔧 提交 Pull Request 改进代码

## 📌 注意事项

- 画像类命令涉及群聊历史和第三方 LLM，请自行评估隐私与成本。
- 长文本建议搭配 `astrbot_plugin_outputpro` 转成图片发送。
