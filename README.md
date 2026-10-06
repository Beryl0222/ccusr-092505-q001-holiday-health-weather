# 假日气象健康联动台

本项目提供假日气象健康联动台的领域事件交换约定、基础校验库与联动服务。值班人员按地区和有效时间登记预报版本、风险阈值、出行线路、重点人群、可用医疗点和通知授权；系统把每条建议拆成气象事实、健康措施、交通处置三类可追溯内容。

## 目录

- `contracts/domain.schema.json`：领域事件信封和已登记类型。
- `data/sample.json`：中文联调样例。
- `src/holiday_health_weather/contracts.py`：不依赖第三方包的基础校验器。
- `src/holiday_health_weather/clock.py`：可注入时钟（`SystemClock` / `ManualClock`）。
- `src/holiday_health_weather/models.py`：预报版本、阈值、线路、人群、医疗点、授权与通知模型。
- `src/holiday_health_weather/service.py`：联动服务 `LinkageService`。
- `tests/test_contracts.py`：契约边界检查。
- `tests/test_service.py`：联动服务行为检查。

## 服务行为

- **登记**：`register_forecast / register_threshold / register_route / register_population / register_medical_point / register_authorization`，预报按地区和有效时间分版本管理。
- **建议生成**：`compose_notice` 按当前生效预报生成建议，拆成 `WEATHER_FACT`、`HEALTH_MEASURE`、`TRAFFIC_HANDLING` 三类内容，每条的 `basis` 记录预报版本、区段、医疗点、人群等依据。
- **预报修订**：登记新版预报只换基尚未送达的通知（草拟、已批准）；已送达的建议保留当时依据。
- **回执幂等**：`confirm_receipt` 对同一旅客跨设备确认只记一次回执，不重复触发升级。
- **目的地变更**：`change_destination` 只重算受影响区段的交通处置，其余内容与依据保持原样。
- **分级升级**：局地暴雨、低温、轻雾按登记阈值随注入时钟推进（关注/警戒/严重），`refresh_escalations` 只记录档位上升。
- **医疗点下线**：`withdraw_medical_point` 只替换同区域相关线路的医疗点，其他区域服务不受影响。
- **安全合并**：`ingest` 按 `event_id` 去重、按聚合版本识别晚到事件，网络恢复后的重复上报可安全合并。
- **复盘**：`review_notice` 给出一条提醒的预报版本依据、批准人、接收人和回执完成情况。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```
