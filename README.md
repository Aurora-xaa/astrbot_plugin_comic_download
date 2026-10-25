# 漫画下载

一个基于 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) 的 AstrBot 插件：下载漫画后按章节转换为 PDF 发送，并支持查询本子详情。

## 功能特性

- 下载整本漫画，自动按章节生成 PDF 并逐章发送。
- 下载指定章节：`/jm <本子ID或链接> p<章节序号>`。
- 查询本子详情，输出样式与原项目 `jmv` 保持一致。
- 整本下载最多发送前 5 个章节；超过 5 话时会先提示，再只下载并发送前 5 话。
- 下载任务在线程中执行，不阻塞 AstrBot 事件循环。
- 支持配置 HTTP 代理，适配需要代理的网络环境。
- 图片在生成 PDF 后自动删除，临时文件按配置自动清理。

## 安装

### 通过插件市场

在 AstrBot WebUI 的插件市场中搜索插件名或展示名，安装后启用即可。

### 手动安装

将仓库克隆到 AstrBot 的插件目录：

```bash
cd AstrBot/data/plugins
git clone https://github.com/Aurora-xaa/astrbot_plugin_comic_download.git
```

然后在 WebUI 中重载插件或重启 AstrBot。

插件依赖：

- `jmcomic`
- `img2pdf`
- `Pillow`

以上依赖已写入 `requirements.txt`。如果 AstrBot 未自动安装，可手动执行：

```bash
pip install -r requirements.txt
```

## 指令说明

插件提示文案：

```text
用法：
/jm <本子ID或链接> —— 下载整本（多章节会按章节分别发送 PDF）
/jm <本子ID或链接> p<章节序号> —— 下载该本子的第 x 话
/jmv <本子ID或链接> —— 查询本子详情
```

| 指令 | 说明 |
| --- | --- |
| `/jm <本子ID或链接>` | 下载整本。若超过 5 话，只下载并发送前 5 话，并提示用户。 |
| `/jm <本子ID或链接> p<章节序号>` | 下载该本子的第 x 话。章节序号对应详情中的“第 N 话”。 |
| `/jmv <本子ID或链接>` | 查询本子详情，输出标题、作者、标签、简介、章节列表等信息。 |

`/jm` 的别名：`jmcomic`、`漫画下载`。  
`/jmv` 的别名：`jm查询`、`jm详情`、`本子详情`。

示例：

```text
/jm 123
/jm 123 p1
/jmv 123
```

说明：`/jm` 只接受本子链接或本子 ID；指定章节时，章节序号写在 `p` 后面，而不是直接使用章节 ID。

## 配置项

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `proxy` | string | 空 | 下载代理地址；留空则直连。 |
| `cleanup_seconds` | int | `1800` | 临时文件保留时间（秒），最小 60 秒。 |

配置项可在 AstrBot WebUI 的插件配置页面修改。

## 限制与说明

- 整本下载固定最多发送前 5 个章节，避免一次发送过多文件。
- 指定章节下载不受 5 章限制。
- 单个 PDF 文件的大小和发送能力取决于所接入的聊天平台。
- 下载的图片会在生成 PDF 后删除；PDF 与临时目录会在 `cleanup_seconds` 后清理。
- 插件未声明 `support_platforms`，默认不限定平台；实际可用性取决于平台适配器是否支持文件发送。
- 请仅下载你有权访问的内容，并遵守所在地法律法规及相关平台规则。

## 致谢

- [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python)：提供漫画下载、章节查询等能力，本插件的查询输出样式参考其 `jmv` 查看器。
- [img2pdf](https://github.com/josch/img2pdf)：提供图片转 PDF 能力。
- [AstrBot](https://github.com/AstrBotDevs/AstrBot)：提供插件运行框架。
- AstrBot 插件模板：提供项目结构参考。

## 开源许可

本项目使用 AGPL-3.0 许可证，详见仓库中的 [LICENSE](LICENSE) 文件。  
使用、修改或分发本项目时，请遵守许可证要求，并保留相关版权与许可声明。
