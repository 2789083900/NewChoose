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
- 每周海龟回测：自动比较全样本、样本内和样本外结果，默认计入 0.1% 手续费与 0.05% 滑点

## 运行

```bash
python -m http.server 5173
```

然后打开 <http://127.0.0.1:5173>。

也可以直接双击 `index.html` 在浏览器中打开。

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
    "require_higher_timeframe": true
  },
  "limits": {
    "max_symbol_units": 4,
    "max_strong_group_units": 6,
    "max_weak_group_units": 10,
    "max_direction_units": 12
  }
}
```

默认情况下，4h 信号会参考 1d EMA200，1h 参考 4h EMA200，15m 参考 1h EMA200；日线没有更高周期过滤。高周期方向与突破方向相反时只记录为过滤观望，不推送入场信号。`breakout_buffer_n` 可调整突破确认距离，设为 `0` 即恢复不加缓冲的突破条件。

海龟突破默认保留成交量和 ATR/收盘价区间保护，用于减少极端环境下的误报；ADX 目前只作为可选过滤器（默认关闭），因为初步多币种回测显示 ADX 硬过滤会减少部分有效趋势信号。可在 `strategy.filters` 中分别设置 `adx_enabled`、`volume_confirmation` 和 `volatility_filter` 为 `true` 或 `false`。这些条件只影响入场信号，不改变海龟的止损、加仓和退出规则；参数应通过样本外回测和实际信号记录验证，不要只追求胜率。

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

云端监控还会把新信号写入 `signal_records.json`，默认按信号确认后下一根 K 线开盘价进行影子成交，并在信号发出后的 24 小时和 48 小时分别记录 MFE（最大有利 excursion）、MAE（最大不利 excursion）和观察窗口收益；汇总结果写入 `signal_tracking_stats.json`。这些记录用于评估策略，不会自动下单，也不会改变入场规则。

每周回测工作流会运行 `backtest_turtle.py`，默认把最近 30% 数据作为样本外区间，并将报告写入 `turtle_backtest_compare.json`。报告同时给出每个币种的结果和各变体汇总，包括交易胜率、平均币种收益、最差币种回撤与最大连续亏损。可以在 GitHub Actions 手动运行时调整历史K线数量和样本外比例。样本外结果只用于验证，不会自动选择最优参数。

注意：密钥只会保存在 GitHub 的“Secrets”里，不会出现在代码或网页中。公网网站在中国大陆的访问稳定性受网络环境影响，如果打不开，可以再改用国内托管。
