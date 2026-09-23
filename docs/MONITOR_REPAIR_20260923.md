# 2026-09-23 监控分类与时效修复：实现及交接

## 本次范围与发布状态

用户要求“修复完善”。在独立目录 D:\桌面\NewChoose\.worktrees\week2-completion-20260923 开发，基于重新核实未变的远端master 3064b0a53e84183dcfebb087f3ae01e5bd0183e2，分支 codex/monitor-health-fix-20260923。

本轮是本地代码修复与测试；没有推送、触发工作流、发送通知、写生产状态或创建付费服务。不要把本地修复称为线上已恢复。旧主目录/旧开发目录用户改动原样保留。

## 修复内容

1. **通知分类统一**：新增 monitor_reporting.py。信号发现过晚未发送、真实未投递成功、outbox事件过期、渠道尝试/失败分开计数。process_events保留expired信息，提前过期仍不发送/不登记影子交易。临界情况下扫描时尚有效但进入发送时已过期，也不再登记影子交易。
2. **不以误报修复掩盖真实问题**：三条过期信号改为 round_failed=0 / expired_signals=3，运行仍degraded，原因明确为signals_expired_before_dispatch；Cloud健康非ok仍失败，35/45分钟心跳阈值原样保留。通知备成功仍记录主渠道失败，但不计成整条消息失败。
3. **去重与重试计数**：已送达事件再次命中去重不返回旧渠道results冒充本轮尝试；过期/已耗尽未尝试的事件同样不回放旧尝试。观察预警加入本轮attempted_event_ids，避免一次扫描内刚发送失败又立即重试同一条。
4. **按市场/周期计算K线时效**：分别记录bar开盘、收盘、收盘距今、应有收盘K线逾期与缺失根数；聚合最差市场，不用全局最新一根掩盖其它市场。未收盘、未知周期/时间、旧结构不能被猜成新鲜。当前范围明确为formal_spot_scan，不能代替研究或永续数据健康。
5. **年龄不等于接口延迟**：4h K线08:00开、12:00收，14:58观察时收盘距今178.1分钟，但下一根尚未收盘，单看该根不说明接口停更。因此不能以“收盘距今>30分钟”判数据源故障；应有下一根收盘后仍缺失超过30分钟才报告market_data_stale。信号有效期仍独立按10分钟保护，不放宽。
6. **可读失败原因**：monitor_health.health_reasons及notification_summary持久化，健康检查显示“信号发现过晚/行情时效异常”等；通知校验输出notification_queue_status与monitor_health_status，工作流步骤改名Validate notification queue and runtime health，Summary页面输出原因。显式指定的health文件缺失不再静默放行。
7. **看板/摘要同步**：展示收盘距今（非接口延迟）、应有K线逾期、当轮投递失败/过期未发送/通知过期和中文原因；旧格式年龄显示未知，原因转义。没有重写历史手机消息或历史状态。
8. **调度取证而非猜测修复**：新增workflow_diagnostics.py，GET读取两类工作流最近20次运行，记录创建时间、开始时间、创建→开始耗时、可见定时运行创建间隔及取消状态。二者都不是“计划触发→开始延迟”，不能单凭取消判定并发抢占。保持原有调度表达式、共享写锁和cancel-in-progress=false，不无证据拆锁冒险造成状态并发冲突。
9. **失败也保留诊断**：两类工作流加always诊断与artifact，14天留存，actions:read，API失败只记脱敏类别和HTTP码，不遮蔽主失败。报告不包含Token/完整心跳URL/生成配置。外部Secret只报告配置是否存在。
10. **研究归档持久化补漏**：普通状态保存步骤补充research_signal_archive的显式git add，复用原有归档逻辑，不改变收益统计口径。

## 验证

- test_turtle + test_week2 + test_monitor_fixes：**247项通过**（原有及前轮218，本轮29）。在临时副本运行，拦截socket.connect，源根目录JSON前后SHA256一致。
- 新增测试覆盖三条过期信号、真假通知失败、主失败备成功、跨周期/最差市场、收盘边界、未知/未收盘时间、主流程--once联通、发送时临界过期、告警阈值保留、健康门禁不放行、API限流脱敏、工作流约束。
- node --check app.js通过；test_monitor_dashboard.cjs DOM冒烟通过：旧/新schema渲染、3条过期/0条投递失败、原因转义。
- 修改的Python语法检查、git diff --check通过。
- PyYAML 6.0.3解析三个工作流成功；检查步骤run/uses、唯一id、只读诊断权限、默认不发D11。它是本地临时校验依赖，不是线上新增依赖；未声称GitHub表达式或实际工作流执行已验收。
- 本次重新查询实时Actions：放行联网后两个API均返回403，未拿到实时运行列表。不能认定小时级间隔的根因已经修好。部署后的诊断使用工作流临时GITHUB_TOKEN和actions:read，若仍失败会明确记录。
- 记录见 reports/monitor-fix-tests-20260923.log/.json、reports/workflow-yaml-validation-20260923.json、reports/live-timing-query-network-20260923.json、reports/monitor-fix-replay-20260923.json。

## 必须保留的未完成项

- **线上发布与观察尚未做**。正式部署须从届时最新master审查并合入修复源码/测试/工作流，不覆盖bot的新状态、不提交本地config，不直接整目录覆盖或force-push。
- **调度小时级间隔未根治**：API目前无可用结果。部署后读诊断区分可见创建间隔与启动等待，再决定调度方案；外部监控只发现问题，不执行扫描。不能承诺改了统计后Health不再红。
- **外部心跳尚未配置**：需要实际外部服务的私密ping地址和收件验证，不可拿仓库网址或随意URL代替。未新增费用。
- **第二周D14仍未冻结**：历史摘要收件已用户确认；专用D11仍未实际发送。统计更新时间/旧时间无时区/研究汇总历史字段等旧缺口并未在本轮整体重构。

## 发布后如何判断

1. 普通扫描如果又发现晚到信号，允许继续红，但原因必须显示signals_expired_before_dispatch，投递失败不再虚增。
2. 通知队列正常且当轮无扫描/时效/投递异常时，健康门禁可以通过；心跳超过35/45分钟仍告警失败。
3. 同一run_id核对Summary、monitor_health和看板；旧日摘要不会被重发替换。
4. 查看monitor-diagnostics artifact的运行时间线；不要仅凭任务名称的绿色判断手机、外部心跳全部通过。
5. 只有稳定运行、外部告警/收件、对账门禁闭环后才冻结版本。
