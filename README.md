# 假日气象健康联动台

面向区域健康联络员的假日值班联动服务：按地区与有效时间登记气象预报版本、
风险阈值、出行线路、重点人群、可用医疗点与通知授权，把一条出行建议拆成
**气象事实 / 健康措施 / 交通处置** 三类可追溯内容，并围绕"修订不扰已送达、
跨设备不重复、局部变更只算局部"提供事件溯源的工作流。

## 目录

- `contracts/domain.schema.json`：领域事件信封与已登记的事件/聚合类型。
- `data/sample.json`：中文联调样例。
- `src/holiday_health_weather/`
  - `contracts.py`：不依赖第三方包的交换层基础校验器。
  - `clock.py`：可注入时间源（`VirtualClock` 手动推进、`SystemClock` 真实时钟）。
  - `event.py`：领域事件定义（`event_id`、聚合、单调版本、带时区时间、因果/关联标识）。
  - `store.py`：只追加事件登记簿（重复上报合并、晚到按发生时间落位、乐观版本冲突留痕）。
  - `model.py`：重放后的领域实体、三类建议内容 `AdvisoryItem` 与证据 `Evidence`。
  - `thresholds.py`：局地暴雨/低温/轻雾的默认分级阈值与分级计算。
  - `catalog.py`：从事件流折叠出的读模型（预报版本选取、通知状态、回执、审计忽略记录）。
  - `builder.py`：建议构建器，输出三类内容与逐条证据链。
  - `service.py`：值班操作服务（登记、起草、审批、送达、升级、重算、同步、复盘）。
- `tests/`：契约边界测试与端到端业务规则场景。

## 核心规则

1. **登记有版本、时间有依据**：预报按地区与 `valid_from/valid_to` 登记，同一预报
   再次登记即修订；阈值、线路、医疗点、授权、人群各自独立版本化，所有时间必须带时区。
2. **一条建议三类内容**：`weather_fact`（气象事实）、`health_measure`（健康措施）、
   `traffic_disposal`（交通处置），每条内容携带 `Evidence`（来源聚合、版本、事件号），
   可逐级回溯到具体预报版本。
3. **修订只影响未送达通知**：预报修订触发同地区未送达通知重算；已送达建议永久保留
   当时的内容快照与预报依据，重放目录和服务审计 (`HolidayHealthService.audit`) 双重留痕。
4. **可注入时间推进分级升级**：`advance_and_evaluate` 推进时钟，局地暴雨、低温、轻雾
   按阈值分级（注意/警示/警告），只在级别升高时产生 `RISK_ESCALATED`；送达后不再升级。
   预报窗口内稍后出现的风险会提前形成健康措施（如老人夜间保暖），但不提前抬级别或交通管制。
5. **跨设备确认幂等**：同一旅客同一阶段（确认/完成回执）只生效一次，换设备重复点击
   被业务指纹合并，不重复触发升级；重放层对绕过指纹入流的重复事件还有第二道幂等防线。
6. **目的地变更只重算受影响区段**：新旧区段集合取差集，移出的通知标记 `superseded`
   留档，保留区段的审批状态不动，新增区段自动起草。
7. **医疗点下线只替换相关线路**：引用下线点的区段把处置替换为备案医疗点，
   其他区段与其他区域服务不中断。
8. **断网恢复安全合并**：`sync` 先按发生时间排序（晚到消息正确落位），相同
   `event_id` 或业务指纹的重复上报只生效一次，同 id 不同内容与版本跳号都会拒绝并留痕。
9. **全程可复盘**：`trace(notice_id)` 还原一条提醒由哪版预报产生、经历的每次重算/升级、
   谁在何时批准、送达给哪些设备、旅客是否确认与完成回执。

## 快速示例

```python
from datetime import datetime
from holiday_health_weather import HolidayHealthService, VirtualClock
from holiday_health_weather.clock import CST

svc = HolidayHealthService(VirtualClock(datetime(2026, 10, 2, 8, tzinfo=CST)))
svc.grant_authorization("auth-1", "雁荡山区", ["sms", "app"])
svc.register_forecast("fc-1", "雁荡山区",
    datetime(2026, 10, 2, tzinfo=CST), datetime(2026, 10, 3, tzinfo=CST),
    [{"hazard": "rain", "value": 30, "unit": "mm/h",
      "at": datetime(2026, 10, 2, 14, tzinfo=CST).isoformat()}])
# ... register_route / register_medical_point / register_person / register_trip ...
svc.draft_for_trip("trip-1")                 # 按区段起草三类建议
svc.approve_notice("notice-trip-1-seg-a", "值班员小李")
svc.send_notice("notice-trip-1-seg-a")       # 授权 + 批准后才可送达
svc.advance_and_evaluate(hours=6)            # 时钟推进，级别升高时升级
print(svc.trace("notice-trip-1-seg-a"))      # 复盘完整来历
```

默认阈值（可按地区用 `register_threshold` 覆盖）：

| 灾种 | 注意 | 警示 | 警告 |
| --- | --- | --- | --- |
| 降雨强度 mm/h（越大越危险） | ≥10 | ≥25 | ≥50 |
| 气温 ℃（越低越危险） | ≤8 | ≤5 | ≤0 |
| 能见度 m（越低越危险） | ≤1000 | ≤500 | ≤200 |

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```
