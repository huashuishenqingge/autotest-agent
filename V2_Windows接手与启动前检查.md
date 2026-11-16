# V2 Windows接手与启动前检查

2026-11-16。用户授权推进后续并运行正式模型实验，实际环境在搭档Windows电脑。本次为准备交接，不是正式冻结或运行成功证据。

## 在原工作副本同步

先执行git status；有未提交修改时先整理提交，不能覆盖原始产物。干净后执行：

```powershell
git fetch origin
git switch feature/experiments-p08
git pull --ff-only origin feature/experiments-p08
.venv/Scripts/python.exe -m unittest discover -s tests -v
node --check web/static/app.js
docker info
docker image inspect sha256:1756fbdd32c39afbdc437e6595f5b69e71128c75de260e02aaae73f709506dcf
.venv/Scripts/python.exe -m experiments.freeze_v2 --check-candidate
```

最后一条仅检查候选，不代表正式冻结或启动。不要重跑qualification_v2覆盖原目录。当前Mac完整200项196通过4跳过，不能作为Windows已通过。

## 后续实现和运行

1. V2-03：按specs/P08B-V2正式实验方案.md消费原30任务和120项原始资格证据，逐文件复验来源、镜像、manifest/run、JUnit、coverage和独立语义绑定。新增独立正式协议、运行时清单、来源提交和后续提交回执；完成拒绝漂移测试和独立审查。现有freeze_v2只实现候选检查，不能改status冒称冻结。
2. V2-04：实现独立360槽计划及严格V2准入，绑定新冻结身份和独立批次目录。完成并发、崩溃、重复claim、取消、未知返回、导出失败及恢复回归；仅核验和合成验证无模型调用。现有experiments.campaign是V1入口，不能直接替代V2。
3. V2-05：前两步完成后，按本次用户授权运行30×4×3=360条，保持模型deepseek-v4-flash-ascend、参数和轮换顺序。全部失败和未知保留，不自动补发、不按成功率换输入。全局账本从原333继续累计；先查原配置账本实际路径，不能创建空账本替代。密钥仅保存在Windows本地。

当前Mac缺Docker、模型配置、reference/p08、artifacts/p08a/image/wheels及120项原始资格目录。共享摘要核对30项qualified、120项valid，但不能替代原始资源复验。镜像缺失时不能换镜像沿用旧证据。V1旧360条及全部冻结/失败记录永久保留。

本次未实现正式冻结或V2调度，因此没有可运行的V2正式启动命令；交付后应记录实际命令。P06独立确认和P09不因本次授权自动完成。
