# 2026-09-23 发布验收中发现的旧快照检查问题

## 已发布的第一阶段
- 修复f0a7d35已正常推送master，没有强推或上传本地运行态。
- 正常Cloud Monitor #174（run 35856184498）于北京时间19:43:22创建、19:46:55完成，conclusion=success；测试通知步骤均skipped。
- 远端bot生成8d201ed、ae467d3状态提交，均先fetch并fast-forward接入本地，没有用旧状态覆盖。
- 新monitor_health：run_id=2f8c2868d73d、status=ok、coverage=100%、freshness=fresh、health_reasons=[]；round_failed/expired_signals/expired_notifications均0；channel_attempts=0。
- 224.2分钟为4h K线收盘距今，不是接口延迟；应有收盘K线逾期0。

## 新确认的原因及修复

认证API和旧Health #84日志提供如下证据（北京时间）：
- 普通扫描#171在9/23 07:53:15已完成成功。
- Health #84的事件在07:51:59创建，但job在07:53:19才开始；Checkout在07:53:20。
- 日志中的实际检出SHA仍为0d628d3383fe2104e4e8c7d7a7493e484dce5d34，与前一扫描触发时的旧SHA相同。
- 检查结果普通心跳age_seconds=7902、永续7747，两个组件均stale。

这证明至少该次健康检查读了排队前的旧快照；并不能证明全部历史失败同因。共享concurrency序列化了写入，但checkout默认事件SHA没有自动刷新为前一任务刚发布的状态。

修复：两个状态写入工作流的Checkout显式ref=${{ github.ref_name }}、fetch-depth=1，保证在获得同一个coinpulse-state-writer执行槽后读取分支当时最新状态。不拆并发锁，不放宽阈值，不改调度周期。
诊断额外记录local_head_sha_at_diagnostics，说明它可能包含保存状态生成的本地提交；GITHUB_SHA继续保留事件版本，二者不再混为一谈。
新增三项回归覆盖两个checkout设置、事件/本地版本区分及git缺失降级。

## 仍待观察
- 认证API最近十次可见Cloud定时创建间隔约121.6～305.9分钟，Health约130.3～308.4分钟。创建到run_started_at为0不等于job无排队，#84已证实job晚启动80秒。
- 旧快照修复不解释这些小时级“创建间隔”；调度及时性仍需继续观察，不能承诺系统每5分钟实际执行。
- 外部心跳Secret仍未配置。正常工作流可在该步骤打印跳过并绿色结束，不视为外部监控验收通过。
- 不发送D11额外测试消息；待此补丁发布后独立执行一次Health并据实际结果记录，不预填成功。
