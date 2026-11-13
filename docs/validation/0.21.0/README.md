# 0.21.0 验证记录

验证日期：2026-11-04；基线为0.20.0完整源码包。

## 结果

- 定向回归55项通过，2项依赖弃用警告。覆盖 radar_views、graph_browser、dual_desk、content_foundation、daily_workflow、country_directory、topics、config_and_resources，耗时85.55秒。
- 新增来源/频道/国家投影测试8项：混合来源、GKG不误标DOC、冻结采集记录恢复、unknown/other筛选一致、当前版本、人物/地点/否认/计划排除、事件记录去重、同国家对方向与动作分别保留、原文词边界。
- 新增Browser入口测试7项：URL格式、禁用脚本与带凭证地址、持久化、跨站写入拒绝；保存不更改数据库连接。
- 真实Redis + FalkorDB 4.22.1启动，验证现有图分析、更正撤边、全局/专题图恢复；新雷达与国家投影从真实保存证据构建。
- Chromium真实浏览器：1440px桌面与390px窄屏。混合来源标签、固定原文版本、网络选点/选边、缩放/平移/布局、放大与Escape恢复、国家按动作/方向独立成弦、证据及角色、频道范围、虚构示例切换、数据库网页入口、事件详情图。未发现JavaScript页面错误或第三方CDN请求；所测页面无横向溢出。
- 原有采集→选题→研究→初稿→作品闭环及专题页再次通过浏览器测试。
- 两个浏览器验证脚本结束时SQLite integrity_check为ok。
- Python编译与变更JavaScript语法检查通过。本地第三方脚本保留原许可证及来源/校验值。

测试新闻、抽取陈述和研究报告都是标记为演示/虚构的夹具；没有宣称调用真实模型抽取、RunDesk/Codex或Exa得到了这些新闻研究。生产应用不会注册QA测试端点，也不预填这些测试新闻。

本轮没有实际启动Docker Browser或验证远程认证/反向代理。配套服务仍固定原FalkorDB 4.22.1；其源码默认浮动Browser构建，不能据数据库版本宣称Browser的精确版本。需要在部署服务器核对Browser登录。

## 复现

```bash
python -m pip install '.[dev,research]' falkordblite==0.11.0
pytest -q tests/test_radar_views.py tests/test_graph_browser.py tests/test_dual_desk.py tests/test_content_foundation.py tests/test_daily_workflow.py tests/test_country_directory.py tests/test_topics.py tests/test_config_and_resources.py
```

浏览器还需Node、playwright-core与可用Chromium（N2D_QA_CHROMIUM），可用NODE_PATH指定Node依赖位置。若系统无中文字体，需安装或提供字体配置。

```bash
python tools/check_radar_live.py /tmp/n2d-radar-qa
python tools/check_workbench_live.py /tmp/n2d-workbench-qa
```

截图和日志仅用作内部检查，不包含在发布包中。安装包检查结果见 release-0.21.0/VALIDATION.txt。

## 数据含义

国家图是当前抽取证据的只读实时汇总；事件关联与重要度仍来自已有FalkorDB分析缓存。国家对的事件记录计数不等于已完成实体消歧的真实事件总量。未建立国家角色的材料不会纳入国家图，但仍可在综合/对应频道事件列表查看。覆盖与截断在接口及页面注明。

未重新做全量视频渲染或长期负载测试；没有将0.20.0的全套回归结果当作0.21.0的新测试结果。文献和算法边界见 ../../RADAR-VIEWS.md。
