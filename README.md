# CoinPulse 币圈信号监控

基于 MACD、KDJ、RSI 与海龟趋势系统的币圈行情监控网页。

## 功能

- 支持 15m / 1h / 4h / 1d 周期切换
- Binance、Bybit、OKX 行情源自动降级
- MACD、KDJ、RSI 本地计算与信号汇总
- ADX 趋势强度、StochRSI 超买超卖辅助确认
- EMA20 / EMA50 / EMA100 / EMA200 多周期均线
- 自选列表、K 线图、信号记录
- 每 30 秒自动刷新
- 根据当前信号自动生成交易策略（方向、入场、止损、目标、仓位）
- 海龟趋势模式：S1 20/10 或 S2 55/20 唐奇安突破
- 海龟 N（20周期 ATR）波动率仓位：账户1%/N，最多4单位
- 海龟规则：突破入场、每0.5N加仓、2N止损、10/20周期反向突破退出
- 海龟过滤：只用已收盘K线、相邻高周期EMA200方向过滤、收盘价超过通道0.1N才确认突破
- 海龟可靠性过滤：ADX趋势强度、成交量相对均量、ATR波动率区间均可独立开关
- 本地订单流 Phase 1：Binance Futures `aggTrade` 持续采集、1分钟 Delta/CVD 聚合、MySQL 60天保留
- 影子信号记录：GitHub Actions 定时追踪信号发出后 24h/48h 的 MFE、MAE 和收益表现
- 影子成交闭环：记录下一根K线开盘成交、触发价偏差、手续费、滑点和成本后盈亏
- 异常行情保护：可配置跳空、单根波动和最低成交额过滤
- 每周海龟回测：自动比较全样本、样本内和样本外结果，默认计入 0.1% 手续费与 0.05% 滑点

## 运行

```bash
python -m http.server 5173
```

然后打开 <http://127.0.0.1:5173>。

也可以直接双击 `index.html` 在浏览器中打开。

## 永续合约研究数据

永续合约快照默认保存到项目所在 D 盘目录：

```text
D:\桌面\NewChoose\derivatives_data
```

采集命令只使用交易所公开接口，不读取 API 密钥，也不会下单：

```powershell
cd D:\桌面\NewChoose
python collect_perp_snapshot.py --symbol BTCUSDT --interval 4h --limit 3600
```

采集命令还会读取 Binance USD-M 的公开 `exchangeInfo`，把合约类型、USDT 保证金、价格最小变动、数量步长、最小数量和最小名义价值一起绑定到快照。回测会按价格/数量精度取整，并跳过不满足最小数量或最小名义价值的信号。合约、标记价格和指数价格三组 K 线必须完全时间对齐，重复时间戳、规格缺失或规格与币种不一致时，快照会被拒绝。这样回测报告能明确区分“已绑定交易所约束”和“交易所无关的研究近似”。

单个币种和周期的快照通常约几百 KB。历史快照和回测报告已加入 `.gitignore`，会保留在 D 盘但不会自动提交到 Git。运行回测时可以指定该目录：

```powershell
python run_perp_backtest.py --symbol BTCUSDT --interval 4h --data-dir D:\桌面\NewChoose\derivatives_data --output D:\桌面\NewChoose\perp_reports\BTCUSDT-4h.json
```

永续回测的成交约定是信号确认后的下一根 K 线开盘价，并记录该根 K 线的实际成交时间；持仓期间反向突破通道会逐根 K 线更新，入场当根不使用未知盘中路径立即退出。手续费、滑点、资金费率和近似强平仍分别计入报告。没有交易所风险档位数据时，报告会标明使用交易所无关的研究近似强平模型，不能当作实盘清算价。

只有经过校验并保存到版本化快照的资料才会进入永续回测；接口失败、数据过旧或数据不连续时不会写入文件。

### 永续影子交易

永续影子交易使用独立的 `perp_shadow_state.json` 和 `perp_shadow_stats.json`，不会读写现货的 `signal_watch.state.json`、`signal_records.json` 或 `trade_stats.json`。它只访问 Binance USD-M 公开接口，不使用 API 密钥，也不提交订单。

当前第一阶段研究配置已启用永续影子交易，但范围严格限制为 BTCUSDT、ETHUSDT 的 4h 合约数据；`research_only` 保持为 `true`，没有真实账户、API 密钥或自动下单。统计文件会记录 `closed_count`、当前开放样本、数据错误和样本可靠性：完成交易少于 30 笔只能视为不足样本，30～49 笔为观察样本，达到 50 笔才达到本阶段的优选样本门槛。

永续数据源暂时不可用时，云端工作流会保留错误到 `perp_shadow_stats.json`，但不会阻断现货信号扫描、健康文件更新和现货状态提交；这保证永续研究链路的问题不会被误报成整套监控失联。

在 `signal_watch.config.json` 的 `derivatives` 中明确设置 `enabled: true` 后运行：

```powershell
python perp_shadow.py --config signal_watch.config.json
```

每次运行会依次处理已有永续影子仓位，再检查新信号。信号按下一根合约 K 线开盘影子成交；止损、反向通道退出和近似强平使用标记价格 K 线；资金费率按公开结算时间戳计入；价格精度、数量步长、最小数量和最小名义价值来自公开合约规格。总开放风险受 `max_total_open_risk` 限制，历史窗口由 `history_limit` 控制并自动分页；OI 公共历史仍受数据源最多 500 条限制。配置默认 `enabled: false` 且强制 `research_only: true`，当前不会自动推送或自动下单。

每笔已成交记录还会保存 MFE/MAE（以首笔入场价为基准，避免加仓改写历史路径）、持仓小时数，以及入场/退出时的合约价、标记价、指数价、基差和 OI 快照。状态中的 `equity` 是已实现权益，`marked_equity` 会按最近标记价加入未实现盈亏；统计文件会给出最大回撤、平均 MFE/MAE、平均持仓时间和资金费率占毛收益比例。权益曲线按每轮处理到的最新已收盘合约 K 线时间采样，同一市场时间会覆盖旧点，不代表逐笔成交或逐根 K 线的完整组合净值。

## 本地订单流服务（Phase 1）

订单流采集是本地增强功能，不影响 GitHub Pages 的静态行情页面。它使用 Node.js 连接 Binance Futures `aggTrade`，只把 1 分钟聚合数据写入 MySQL；不会保存逐笔成交明细。

1. 先用 MySQL 管理员账号执行 `orderflow/schema.sql`，创建数据库和表。
2. 复制 `orderflow/config.example.json` 为 `orderflow/config.json`，填写 MySQL 用户、密码和数据库配置。
3. 安装依赖并启动服务：

```powershell
cd orderflow
npm install
npm start
```

服务默认监听 `http://127.0.0.1:8787`。然后按原方式启动网页：

```powershell
cd ..
python -m http.server 5173
```

打开 `http://127.0.0.1:5173` 后，订单流面板会显示实时连接、1分钟 Delta、CVD、主动买卖量和大单统计。MySQL 数据目录和日志位置仍由本机 MySQL 配置决定，需确保它们位于 D 盘；当前机器的 MySQL 数据目录为 `D:/MySQL/MySQL Server 8.0/Data`。

## 安装成手机 App

CoinPulse 支持安装到手机主屏幕，像 App 一样全屏打开：

- 安卓 Chrome：打开网页后点右上角菜单，选择“添加到主屏幕”或“安装应用”
- iPhone Safari：点底部“分享”按钮，选择“添加到主屏幕”

网页部署到 HTTPS 地址后，安卓会直接弹出安装提示，并支持离线打开壳页面。局域网内使用时不走 HTTPS，安装入口可能不自动出现，但仍可按上面两步手动添加到主屏幕。

## 海龟策略

网页默认使用海龟 S2（55周期入场、20周期退出）。可以在主策略面板切换 S1/S2、输入账户净值，并在回测区域选择对应的海龟模式。周期不是日线时，系统会把 20/55/10/20 日按当前周期换算为K线数量；例如 4h 的 S2 需要 330 根入场历史K线。

后台监控默认使用同一套规则。`signal_watch.config.json` 中的 `strategy` 可调整：

```json
{
  "mode": "turtle",
  "turtle_system": "system2",
  "account_value": 10000,
  "risk_fraction": 0.01,
  "filters": {
    "closed_candles_only": true,
    "higher_timeframe": true,
    "higher_ema_period": 200,
    "breakout_buffer_n": 0.1,
    "require_higher_timeframe": true,
    "anomaly_filter": true,
    "max_gap_pct": 0.1,
    "max_range_pct": 0.2,
    "liquidity_filter": false,
    "min_quote_volume": 0
  },
  "limits": {
    "max_symbol_units": 4,
    "max_strong_group_units": 6,
    "max_weak_group_units": 10,
    "max_direction_units": 12
  }
}
```

影子交易默认按双边 0.1% 手续费和双边 0.05% 滑点估算成本，可在配置的 `execution.fee_rate` 与 `execution.slippage_rate` 中调整。海龟信号会记录突破触发价与下一根K线开盘影子成交价的偏差，并在 `trade_stats.json` 中汇总成本前后盈亏。单位数量按 `账户净值 × 风险比例 ÷ (2N)` 计算，使首单位触达2N止损时的理论毛风险约为账户的1%。

默认情况下，4h 信号会参考 1d EMA200，1h 参考 4h EMA200，15m 参考 1h EMA200；日线没有更高周期过滤。高周期方向与突破方向相反时只记录为过滤观望，不推送入场信号。`breakout_buffer_n` 可调整突破确认距离，设为 `0` 即恢复不加缓冲的突破条件。

海龟突破默认保留成交量和 ATR/收盘价区间保护，用于减少极端环境下的误报；ADX 目前只作为可选过滤器（默认关闭），因为初步多币种回测显示 ADX 硬过滤会减少部分有效趋势信号。可在 `strategy.filters` 中分别设置 `adx_enabled`、`volume_confirmation` 和 `volatility_filter` 为 `true` 或 `false`。这些条件只影响入场信号，不改变海龟的止损、加仓和退出规则；参数应通过样本外回测和实际信号记录验证，不要只追求胜率。

默认启用异常行情保护：当前K线相对上一根收盘价跳空超过 10%，或单根高低价区间超过收盘价 20% 时，突破只记录为过滤观望。`liquidity_filter` 默认关闭；开启后按 `close × volume` 计算成交额，低于 `min_quote_volume` 的市场不入场。以上阈值只影响入场确认，不会修改已有持仓的止损和退出。

`mode` 改为 `legacy` 可恢复原来的 1h/15m 乖离回归、4h/1d RSI 背离推送。海龟 S1 的“盈利突破跳过”状态会保存在 `signal_watch.state.json`。

这些规则是机械化执行和风险管理实现，不代表收益承诺。回测应使用足够长、跨多种市场状态的样本，并同时关注回撤、交易成本和参数敏感度。

## 信号提醒

### 网页端提醒（页面开着时有效）

点击页面右上角的铃铛按钮开启提醒。出现信号时会播放提示音，手机支持的话还会震动，并尽量弹出浏览器通知。开启一次后会自动记住。

信号横幅下方会同步给出交易策略：方向、入场条件、止损位、目标位和建议仓位，随行情每 30 秒自动更新。

### 手机推送（页面关着、手机锁屏也能收到）

后台监控程序默认按照网页同一套海龟趋势规则盯盘，信号变化时立刻推送到手机；设置 `strategy.mode=legacy` 时才使用旧的指标策略。

1. 启动一次监控程序，让它自动生成配置文件：

```bash
python signal_watch.py --once
```

2. 编辑 `signal_watch.config.json`，在 `channels` 里填一个或多个推送渠道：

- Server酱（推荐，微信接收）：在 <https://sct.ftqq.com> 登录后拿到 SendKey，填到 `serverchan.sendkey`。
- 钉钉机器人：群设置里添加“自定义机器人”拿到 Webhook，填到 `dingtalk.webhook`。
- 企业微信机器人：企业微信群添加机器人拿到 Webhook，填到 `wecom.webhook`。
- PushPlus（微信接收）：在 <https://www.pushplus.plus> 登录后拿到 token，填到 `pushplus.token`。
- Bark（iPhone）：手机安装 Bark，把设备 Key 填到 `bark.key`，`bark.server` 保持默认即可。

#### 微信推送详细步骤（Server酱）

1. 用微信扫 <https://sct.ftqq.com> 页面上的二维码登录。
2. 登录后在控制台复制 SendKey，形如 `SCTxxxxx`。
3. 把 SendKey 填到 `signal_watch.config.json` 的 `serverchan.sendkey`。
4. 回到 Server酱 页面，在“消息通道”里选择“方糖服务号”并保存（程序发送时也会强制走这个通道）。
5. 运行 `python signal_watch.py --test`，微信里收到“CoinPulse 测试通知”即配置成功。

3. 正式启动监控：

```bash
python signal_watch.py
```

程序运行后也可以随时编辑 `signal_watch.config.json`，下一轮检测会自动生效，不需要重启。

4. 验证推送：

```bash
python signal_watch.py --test
```

手机收到“CoinPulse 测试通知”就说明配置成功。

信号推送消息里也会直接带上交易策略，包括入场条件、止损位和目标位。

也可以把 `symbols` 改成想监控的币种，把 `intervals` 改成 `["15m"]`、`["4h"]` 等周期。程序会把最近一次信号状态保存在 `signal_watch.state.json`，避免重启后重复推送。

注意：电脑关机时后台监控也会停止；想 24 小时接收，可以把电脑保持开机，或把这个程序放到一台常开的服务器上运行。

## 云端部署（手机 App + 微信推送，不依赖电脑）

如果你想在公司关电脑之后仍然正常收到信号，可以把 CoinPulse 发布到 GitHub，让云端每 5 分钟检查一次信号，网站也会自动生成一个手机能打开的公网地址。当前云端流程不依赖订单流服务；订单流仍然是可选的本地增强功能。

1. 打开 <https://github.com>，注册或登录 GitHub 账号。
2. 点击右上角 `+`，选择 `New repository`，仓库名填 `coinpulse`，可见性选 `Public`（免费），然后创建。
3. 把项目里的这些文件上传到仓库：`index.html`、`app.js`、`styles.css`、`sw.js`、`manifest.webmanifest`、`icons/`、`vendor/`、`signal_watch.py`、`track_signals.py`、`signal_watch.config.template.json`、`.github/`、`.gitignore`。不要上传 `signal_watch.config.json`，里面含有你的推送密钥。
4. 打开仓库的 `Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`，添加：
   - 名称填 `SERVERCHAN_SENDKEY`，值填你的 Server酱 SendKey（形如 `SCTxxxxx`，在 <https://sct.ftqq.com> 控制台复制）。
   - 如果你想同时用 PushPlus，可以再添加 `PUSHPLUS_TOKEN`，值在 <https://www.pushplus.plus> 用微信扫码登录后复制。
5. 打开仓库的 `Settings` -> `Pages`，`Source` 选择 `GitHub Actions`，然后点 `Save`。
6. 等几分钟后，打开仓库的 `Actions` 页面：
   - `Deploy CoinPulse site` 显示绿色，表示网站已发布。
   - `CoinPulse Cloud Monitor` 显示绿色，表示云端监控已开始。
7. 在 `Actions` 页面点 `CoinPulse Cloud Monitor`，再点 `Run workflow`，勾选“Send a test WeChat push”，然后点绿色按钮运行。微信收到“CoinPulse 测试通知”就说明推送配置成功。
8. 网站地址是 `https://你的用户名.github.io/coinpulse/`，手机浏览器打开后点“添加到主屏幕”，就能像 App 一样使用。
9. 云端监控第一次运行只会记录当前信号状态，之后信号变化时会通过微信推送通知你。

云端监控还会把新信号写入 `signal_records.json`，默认按信号确认后下一根 K 线开盘价进行影子成交，并在信号发出后的 24 小时和 48 小时分别记录 MFE（最大有利 excursion）、MAE（最大不利 excursion）和观察窗口收益；汇总结果写入 `signal_tracking_stats.json`。活跃记录最多保留 500 条，较早记录会先按信号月份归档到 `signal_archive/signals-YYYY-MM.json`，再从活跃文件移除，避免长期前瞻样本丢失。这些记录用于评估策略，不会自动下单，也不会改变入场规则。

仓库还包含独立的 `CoinPulse Monitor Health` 工作流，每 15 分钟检查 `monitor_health.json`。如果超过 20 分钟没有心跳，或最近一次扫描失败，会通过 `SERVERCHAN_SENDKEY` 发送一次失联告警；恢复后发送一次恢复通知，状态保存在 `monitor_alert_state.json`，不会在状态未变化时重复推送。健康记录还会保存当前多空单位、每个币种剩余容量和按止损估算的理论风险，供看板人工复核。

推送消息会同时标明K线收盘时间、信号生成时间（UTC/北京时间）、生成延迟和影子成交窗口状态。默认生成延迟超过5分钟就标记为“窗口已错过，禁止追价”；该状态只用于人工执行提示，不会自动下单。

现在看板还会读取 `signal_quality_report.json`，把“海龟实际止损/通道退出胜率”和“信号后 24h/48h 方向胜率”分开显示，并给出小样本 95% 区间和可靠性等级。至少完成 30 笔海龟影子交易前，胜率只作为观察数据；建议积累 50 笔后再评估是否值得据推送执行。24h/48h 方向胜率不能替代按完整止损、加仓和退出规则结算的策略胜率。

监控还会检查行情新鲜度和本轮市场覆盖率。默认要求至少 80% 的币种/周期成功取得最新已收盘K线；低于阈值时本轮不会推送信号，也不会覆盖原有扫描状态，健康文件会标记为 `degraded`。网络请求对临时超时、限流和服务器错误最多重试 3 次并采用指数退避。

每轮扫描会生成短 `run_id`，并写入健康记录、信号记录和交易记录，便于定位一次扫描对应的推送与结算结果。回测工作流在提交报告前还会检查报告字段、快照校验和以及组合滚动验证窗口；检查失败时不会提交新报告。

每周回测工作流会运行 `backtest_turtle.py`，默认把最近 30% 数据作为样本外区间，并将报告写入 `turtle_backtest_compare.json`。报告同时给出每个币种的结果、各变体汇总，以及 production_default 在统一资金池下的组合结果。组合回测按币种、总单位数和方向单位数限制持仓，并输出组合权益曲线；同一时点的信号按币种字母顺序确定性处理。滚动样本外窗口会同时输出基准成本、2 倍成本和 4 倍成本压力结果，用于判断策略对手续费和滑点的敏感性。可以在 GitHub Actions 手动运行时调整历史K线数量和样本外比例。样本外结果只用于验证，不会自动选择最优参数。

当前仓库随附的最新报告基线（2026-09-11）使用 3600 根 4h 现货 K 线：请求 12 个币种，TONUSDT 已停用，实际参与 11 个币种。组合全样本收益约 +15.69%，最大回撤约 -37.87%，48 笔交易；固定窗口样本外收益约 +36.91%，但只有 10 笔交易，仍属于观察样本。成本压力测试显示首个滚动窗口在四倍手续费/滑点下转为负收益，说明执行成本敏感。报告现在还记录组合最大连续亏损、样本可靠性、方向暴露峰值、保证金峰值及基准/双倍/四倍成本收益，查看看板时应优先关注这些风险字段，而不是单独追求胜率。

历史K线快照保存在 `backtest_data/`：每份数据记录来源、市场类型、时间范围、连续性检查和 SHA-256 校验，并由清单文件汇总。快照按校验和保留历史版本，刷新数据不会覆盖旧回测依据；通常回测会复用最近一份已验证快照，手动运行工作流时可选择“重新下载历史数据快照”。当前来源为 Binance 现货数据，因此结果不应被解读为永续合约回测。报告还记录代码修订、运行编号、Python 版本和数据校验和。默认工作流获取 3600 根 4h K线，以形成两个完整的滚动样本外窗口：每个窗口使用 2160 根作为开发期、后续 540 根作为验证期、每次前移 540 根，并同时列出基准成本和双倍手续费/滑点下的结果。除逐币种结果外，报告还对统一资金池执行相同滚动验证，并强制币种、总单位、方向和相关组限仓；该过程不会自动调参或选择变体。

注意：密钥只会保存在 GitHub 的“Secrets”里，不会出现在代码或网页中。公网网站在中国大陆的访问稳定性受网络环境影响，如果打不开，可以再改用国内托管。
