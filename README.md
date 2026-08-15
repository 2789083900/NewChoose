# CoinPulse 币圈信号监控

基于 MACD、KDJ、RSI 的币圈行情监控网页。

## 功能

- 支持 15m / 1h / 4h / 1d 周期切换
- Binance、Bybit、OKX 行情源自动降级
- MACD、KDJ、RSI 本地计算与信号汇总
- ADX 趋势强度、StochRSI 超买超卖辅助确认
- EMA20 / EMA50 / EMA100 / EMA200 多周期均线
- 自选列表、K 线图、信号记录
- 每 30 秒自动刷新
- 根据当前信号自动生成交易策略（方向、入场、止损、目标、仓位）

## 运行

```bash
python -m http.server 5173
```

然后打开 <http://127.0.0.1:5173>。

也可以直接双击 `index.html` 在浏览器中打开。

## 安装成手机 App

CoinPulse 支持安装到手机主屏幕，像 App 一样全屏打开：

- 安卓 Chrome：打开网页后点右上角菜单，选择“添加到主屏幕”或“安装应用”
- iPhone Safari：点底部“分享”按钮，选择“添加到主屏幕”

网页部署到 HTTPS 地址后，安卓会直接弹出安装提示，并支持离线打开壳页面。局域网内使用时不走 HTTPS，安装入口可能不自动出现，但仍可按上面两步手动添加到主屏幕。

## 信号提醒

### 网页端提醒（页面开着时有效）

点击页面右上角的铃铛按钮开启提醒。出现信号时会播放提示音，手机支持的话还会震动，并尽量弹出浏览器通知。开启一次后会自动记住。

信号横幅下方会同步给出交易策略：方向、入场条件、止损位、目标位和建议仓位，随行情每 30 秒自动更新。

### 手机推送（页面关着、手机锁屏也能收到）

后台监控程序会按照网页同一套 MACD / KDJ / RSI 规则盯盘，信号变化时立刻推送到手机。

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

如果你想在公司关电脑之后仍然正常收到信号，可以把 CoinPulse 发布到 GitHub，让云端每 5 分钟检查一次信号，网站也会自动生成一个手机能打开的公网地址。

1. 打开 <https://github.com>，注册或登录 GitHub 账号。
2. 点击右上角 `+`，选择 `New repository`，仓库名填 `coinpulse`，可见性选 `Public`（免费），然后创建。
3. 把项目里的这些文件上传到仓库：`index.html`、`app.js`、`styles.css`、`sw.js`、`manifest.webmanifest`、`icons/`、`vendor/`、`signal_watch.py`、`signal_watch.config.template.json`、`.github/`、`.gitignore`。不要上传 `signal_watch.config.json`，里面含有你的推送密钥。
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

注意：密钥只会保存在 GitHub 的“Secrets”里，不会出现在代码或网页中。公网网站在中国大陆的访问稳定性受网络环境影响，如果打不开，可以再改用国内托管。
