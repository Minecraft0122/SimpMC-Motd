# SimpMC-Motd

SimpMC-Motd 是一个面向 AstrBot 的 Minecraft Java 版服务器状态插件。它会定时采集在线人数历史，并在收到整条消息 `motd` 或 `/motd` 时生成 MOTD 状态图片。每个群可以使用全局默认服务器，也可以在 AstrBot 控制台中配置独立的服务器映射。

## v2.0.0 破坏性重构

本次重构从 `v2.0.0` 开始。按照语义化版本规则，这次升级包含不兼容变更，必须提升主版本号：

- 删除群聊中的 `/setmotd` 和 `/clearmotd`，不再允许任何用户通过聊天消息修改查询目标；
- 将 AstrBot 控制台配置设为唯一配置权威，SQLite 中的服务器行只用于历史与作用域记录；
- 新增 `pages/settings` 图形化配置页面和经过后端校验的 Web API；
- 最低 AstrBot 版本提升到 `>=4.27.0`。

从旧版升级后，请先进入 `settings` 页面复核自动迁移的群服映射，再对外开放查询。

## 安装与配置

把整个 `SimpMC-Motd` 目录放入 AstrBot 插件目录，或通过 AstrBot WebUI 安装本仓库，然后启用插件。

要求 AstrBot `>=4.27.0`。该版本要求来自本次重构使用的 [AstrBot Plugin Pages](https://docs.astrbot.app/dev/star/guides/plugin-pages.html)、插件 Web API 和异步配置保存能力；`metadata.yaml` 已声明相同的最低版本。

启用后，在 AstrBot WebUI 中进入：

`插件 -> SimpMC-Motd -> 插件行为 -> 页面 -> settings`

`settings` 页面集中管理默认服务器、会话权限、群白名单、群服映射、查询与缓存、图片背景、图表与历史。保存请求会在插件后端重新校验，成功后清理相关缓存并刷新运行中的采样器；如果页面提示需要重载插件，请在 AstrBot 控制台完成一次重载。

> [!IMPORTANT]
> 所有 MOTD 设置只能在 AstrBot 控制台中修改。群聊和私聊只提供状态查询，不提供设置或清除入口；`/setmotd`、`/clearmotd` 已从 v2 删除。

主要默认值：

- 默认服务器：`127.0.0.1:25565`
- 后台采样间隔：`300` 秒
- 最大并行查询：`4`
- 图片缓存：`45` 秒
- 最近查询结果复用：`30` 秒
- 图表时间窗：`24` 小时
- 历史保留：`30` 天
- 高延迟黄点阈值：`200` ms
- 私聊查询：开启
- 未单独配置的群使用默认服务器：开启
- 群白名单：关闭
- 远程背景缓存：`3600` 秒

## 群服映射与访问控制

在 `settings` 页面的“群服映射”表格中为群填写会话范围、Minecraft Java 服务器地址和可选显示名称。会话范围支持以下形式：

```text
123456789
group:123456789
aiocqhttp:group:123456789
telegram:group:987654321
```

纯群号和 `group:群号` 会归一化为跨平台兼容范围；同时接入多个平台时，推荐使用 `平台ID:group:群号`，避免不同平台上的相同群号共用配置与历史。服务器地址支持域名、IPv4 和带方括号的 IPv6，例如：

```text
mc.example.com:25565
127.0.0.1:25565
[2001:db8::10]:25565
```

访问规则如下：

- 群服映射优先于全局默认服务器；
- 启用群白名单后，只有白名单或群服映射中出现的群可以查询；
- 关闭“未配置群使用默认服”后，没有群服映射的群不能查询；
- “允许私聊查询”只控制私聊能否使用 `motd` 或 `/motd`，不会赋予任何配置权限；
- 后台采样只处理控制台映射，以及启用白名单且允许使用全局默认服务器的白名单群；普通群临时查询默认服务器不会自动变成永久配置。

## 消息触发

插件只匹配两种精确的小写消息：

```text
motd
/motd
```

不接受参数、前后缀、重复斜杠或大写变体。匹配后插件会立即停止该消息的后续事件传播，并进入服务器状态查询与图片生成流程；缓存仍有效时会直接复用上一张图片。如果群不在白名单、没有可用目标或私聊查询被禁用，则返回对应提示，但消息仍不会继续交给后续处理器。

状态图片包含服务器显示名、Minecraft MOTD、版本、在线人数、图标和历史人数曲线。查询或渲染失败时，插件会安全回退为可读的文字状态，不会开放聊天侧配置入口。

## 运行机制

1. `main.py` 的精确正则监听器识别 `motd` 或 `/motd`，先停止事件传播，再解析平台与会话范围。
2. 目标解析器以控制台群服映射为最高优先级；没有映射时，按白名单、私聊权限和“未配置群使用默认服”决定是否使用全局默认服务器。SQLite 中的旧绑定不会覆盖控制台设置。
3. 渲染缓存命中时直接返回图片；否则状态服务优先复用最近一次有效采样，必要时才执行 Minecraft Java status 查询并写入历史。
4. 图片展示层生成有界模板数据，安全预取背景，再通过 AstrBot 的 `html_render` 能力生成 PNG。
5. 后台采样器按照当前控制台配置周期性刷新目标，并以受限并发写入 SQLite。

`settings` 页面通过 AstrBot 页面桥接调用插件注册的读取与保存 API。后端只接受完整的已知字段集合，对类型、数值范围、群作用域、地址、重复映射和数据规模进行校验，再异步持久化配置。保存成功后会清除渲染和背景缓存、刷新查询并发设置，并重启采样器以应用新配置。

热重载采用两阶段运行时所有权切换：新实例先完成数据库与服务准备，成功后才接管 Web API 和后台采样，并退役旧实例；新实例初始化失败时，原有健康实例继续运行。终止时会等待正在执行的 MOTD 与控制台保存操作结束，再关闭采样器。

## 查询、缓存与渲染

插件查询的是 Minecraft Java 版 status 协议，不支持 Bedrock 版服务器。

同一群或会话的状态图默认缓存 `45` 秒。缓存期内重复查询会直接返回上一张图片，不重新查询服务器和渲染；可通过 `render_cache_seconds` 调整，设为 `0` 可关闭。

消息查询和后台采样默认复用最近 `30` 秒的状态，避免短时间内重复 ping 同一服务器；可通过 `sample_reuse_seconds` 调整，设为 `0` 可关闭。插件不再暴露或读取特定 Minecraft 游戏版本的握手配置；状态查询内部使用通用探测握手，服务端版本仍以 status 响应为准。每次成功取得 status 响应后，插件都会发送 Minecraft Ping/Pong 包测量延迟；控制台中的 `latency_warning_ms` 决定何时在图表上把成功采样标为黄色点。

图片通过 AstrBot 的 `html_render` 能力生成 PNG。按照 AstrBot 的渲染部署方式，自定义 HTML 可能会交给 AstrBot 配置的 T2I 网络端点；发送的数据已缩减为图片实际需要的服务器显示名、群或会话标签、MOTD、图标、曲线与背景。如果这些信息不能交给第三方服务，请在 AstrBot 中配置可信的自托管 T2I 端点。

状态图会尽量保留 Minecraft MOTD 中的 JSON 颜色和 `§` 颜色码。时间轴和最后采样时间固定使用 `UTC+8 / Asia/Shanghai`，不跟随服务器系统时区。X 轴按真实采样时间窗显示；Y 轴顶部等于该窗口内探测到的最大在线人数，除顶部最大值外，可见刻度使用 `5` 的倍数。成功采样延迟严格高于 `latency_warning_ms` 时显示黄色点；无法连接的采样显示为红色竖条。历史记录不足两条时图表区会变灰。

## 远程背景安全

远程背景由插件先行下载、验证并转换为本地 data URI，渲染页面不会直接访问背景 URL。插件只接受解析结果全部为公网 IP 的 HTTP(S) 地址；每一跳重定向都会重新校验并固定已验证 IP，拒绝回环、内网、链路本地地址和 HTTPS 降级。

背景只接受 PNG、JPEG 和静态 WebP，并限制下载字节数、图片尺寸与总像素。`background_fetch_timeout_seconds` 是连接、重定向、响应头和响应体共用的总截止时间；`background_max_bytes` 控制文件上限。默认缓存 `3600` 秒，`background_cache_seconds=0` 表示每次重新预取但不复用；留空背景 URL 可完全禁用远程背景。

刷新失败时优先使用旧缓存，否则使用内置背景，并额外发送仅含协议与主机名的脱敏警告。Clash/TUN 的 Fake-IP DNS 可能返回保留地址，因此会被相同规则拒绝并安全回退；如需远程背景，请为该域名启用真实公网解析，或改用其他可信公网 URL。

## 历史数据与旧版迁移

采样数据保存在 AstrBot 数据目录的：

```text
plugin_data/SimpMC-Motd/history.sqlite3
```

新群历史按 `平台ID:group:群号` 隔离，私聊历史按平台与私聊会话隔离。旧版 `group:群号` 的跨平台历史会在对应平台首次访问时复制为平台独立记录。

从旧版升级时，插件会一次性检查 SQLite 中由旧 `/setmotd` 创建的显式绑定：

- 旧群绑定会导入 `settings` 页面对应的群服映射；
- 如果控制台中已经存在同群的精确映射或跨平台映射，现有控制台配置优先，不会被旧绑定覆盖；
- 只有在新配置成功持久化后，旧群绑定才会标记为非配置权威；失败时保留待迁移状态，并在下次启动重试；
- 旧私聊绑定不会导入，也不会继续作为查询目标；私聊是否可查询以及查询哪个服务器完全服从当前控制台设置；
- 自动导入完成后，管理员仍应进入 `settings` 页面逐条复核地址、平台范围和显示名称。

如果旧版数据库位于 `plugin_data/astrbot_plugin_mc_motd/history.sqlite3`，插件会通过 SQLite backup 复制；目标库已存在时，则事务化合并缺失绑定和去重历史。新库数据优先，旧文件不会被删除。首次复制失败且新库尚不存在时会暂时继续使用旧库；已有新库的合并失败时保持新库为权威来源。两种情况都会在下次启动重试，避免空库遮蔽旧历史或新旧数据库分叉。

新采样只保存绘图所需的有界 MOTD 描述、人数、版本、延迟、错误和已验证图标，不保存 status 响应中的玩家名称或 UUID 列表。迁移前旧版本已经写入的原始记录会按 `retention_days` 到期清理。SQLite 操作在工作线程中串行执行，不阻塞 AstrBot 消息事件循环；同一目标库的迁移与 Schema 初始化通过进程内锁和文件锁串行化。

## 代码结构

`main.py` 负责 AstrBot 生命周期、运行时所有权、Web API 和单个 MOTD 消息监听器；可独立测试的核心位于 `simpmc_motd/`，图形化页面位于 `pages/settings/`：

```text
.
├─ main.py                    # AstrBot 适配、页面 API、触发与生命周期
├─ _conf_schema.json          # AstrBot 配置 Schema
├─ metadata.yaml              # 插件元数据与最低版本
├─ pages/settings/            # settings 页面：HTML、CSS 与页面桥接逻辑
├─ .astrbot-plugin/i18n/      # 页面中英文元数据
├─ templates/status.html      # 状态图片模板
├─ simpmc_motd/
│  ├─ minecraft/              # Java status 编解码、MOTD 组件与查询客户端
│  ├─ rendering/              # 图表、背景、模板数据和有界渲染缓存
│  ├─ config.py               # 动态配置读取、类型转换和安全边界
│  ├─ web_settings.py         # 页面模型序列化与不可信输入校验
│  ├─ targeting.py            # 控制台权威的会话权限与目标解析
│  ├─ storage.py              # SQLite Schema、迁移和异步适配
│  ├─ status_service.py       # 查询合并、复用、持久化与配置刷新
│  ├─ collector.py            # 有界 worker 后台采样
│  ├─ concurrency.py          # 异步任务与并发辅助
│  ├─ models.py               # 核心数据模型
│  └─ text.py                 # 非法 Unicode 归一化
└─ tests/                     # 单元、适配、迁移与并发回归测试
```

## 开发与验证

Python 测试不需要运行 AstrBot、浏览器或公网服务：

```powershell
python -m unittest discover -s tests -t . -v
python -m ruff check .
python -m ruff format --check .
python -m compileall -q main.py simpmc_motd tests
```

页面脚本还可以单独执行语法检查：

```powershell
node --check pages/settings/app.js
```

GitHub Actions 会在 Python 3.10 和 3.13 上执行 Python 检查与测试。
