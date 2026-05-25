# 绘墨：诺保科CRM后台管理页面

## 项目路径
/Users/gary/Projects/nuoboke-project/
后台前端: source/nuoboke-admin/

## 技术栈
layuimini (layui 2.x)

## 任务1: 审批管理页面
文件: source/nuoboke-admin/pages/approve/index.html
- 列表所有审批请求（待审/已审/驳回）
- 每个审批项有"通过/驳回"操作按钮
- 对接API: GET /api/v1/admin/approves/pending, GET /api/v1/admin/approves, POST /api/v1/admin/approves/{id}/approve

## 任务2: 日报统计
文件: source/nuoboke-admin/pages/report/daily.html
- 按日期/销售筛选
- 展示统计图表（拜访数/签到率/趋势）

## 任务3: 系统设置
文件: source/nuoboke-admin/pages/setting/index.html
- 打卡半径、拜访时间窗口等配置
