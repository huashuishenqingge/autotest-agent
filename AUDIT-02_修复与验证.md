# AUDIT-02 修复与验证

日期2026-11-10。用户明确“给我解决一下”授权四项修复。基于origin/feature/experiments-p08提交6b04080，分支fix/audit-web-paths。实施人：当前助手。独立成员复核及用户验收待补。

## 修复与验收

|问题|实现|回归证据|
|---|---|---|
|P08A相对/平台别名路径误判|project_tasks.py拒绝reference/project根链接或reparse后resolve；再执行既有检查，不移除源码白名单与哈希验证|原Mac失败测试通过；新增relative_reference_root回归|
|停服仅等待2秒|worker非daemon，close设置停止标记后等worker退出；期间禁止提交；持久化与排队interrupted处理结束后main才释放锁|阻塞worker超过2.2秒时close仍等待，释放后检查线程结束、结果保存及排队终态|
|轮询完整结果过大|SQLite摘要查询和分页，不读取result；active_count直接COUNT；前端只在选中项updated变化时加载详情|30项含大结果的7项分页响应小于5KB；详情保留原文；101条历史不遮蔽活动计数；浏览器2次轮询后仍仅1次详情请求|
|非法token异常|ASCII与256长度检查先于compare_digest|HTTP非ASCII及超长token均403，不创建任务|

新增跨平台Actions（Python3.12三系统），仅运行标准库/HTTP测试，不需要模型密钥。进度表将02.1—02.4统一标注实现已接受、独立复核待补；保留P06用户确认待补，不冒记用户接受。CI配置尚未在GitHub执行，不能称三平台均已通过。

## 实际命令与结果

- 首先读取最新AGENTS、进度表、已完成复核报告及相关规格；取得最新远端后创建独立分支，不覆盖其他成员的T2筛选进展。
- macOS、Python3.12.14：`python -m unittest discover -s tests -q`，152项，150通过、2跳过（Windows junction；大小写不敏感文件系统的冲突用例）。无错误。需要允许本机回环HTTP监听，已按权限流程执行。
- `node --check web/static/app.js`通过。
- `python -m web.server --port 8767 --evidence --data artifacts/audit02-web`启动只读历史证据服务；Chrome/Playwright验证分页摘要不含result、任务切换/覆盖率显示、相同updated的轮询不重复取详情、分页按钮及无页面错误。通过。脚本artifacts/audit02/browser.cjs（忽略产物）。
- git diff --check通过。

本轮没有模型调用、Docker运行、正式实验执行或新修复率数据；使用固定历史证据仅验页面，未改业务任务、配额、账本、冻结清单或历史结果。停服回归用阻塞替身，不宣称已重新验证真实容器清理。独立成员复核待补；后续建议先查看Actions三平台结果，再接续原计划，当前不启动下一阶段。
