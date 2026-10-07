# 非遗影像来源与许可图谱

面向非遗短视频审核的来源与许可图谱：让原始访谈、手艺步骤、参考文献、翻译说明、
剪辑片段、创作者声明、传承人授权和派生作品互相追溯，支撑审核结论、事实更正、
许可变更后的处置，并为审核人员提供命令行溯源入口。

## 领域模型（`src/heritage_media_provenance/domain.py`）

- **素材节点** `Material`：原始访谈、手艺步骤、参考文献、译注、剪辑片段、
  创作者声明、授权凭据、派生作品八类；素材带指纹、创作者与传承人。
- **加工关系** `Relation`：`source → target` 表示 target 由 source 加工而来
  （edits/cites/translates/annotates/uses…），递归边构成溯源链，支持环检测与钻形去重。
- **证据立场**：`supports` 彼此佐证共同支撑结论；`contradicts` 矛盾来源同时保留，
  审核结论里分歧不允许被删除。
- **授权** `Authorization`：限定传承人、素材、地区（territories）与期限
  （valid_from/valid_until），状态 active/withdrawn/expired。
- **作品与公开版本**：作品状态 draft/published/held；每次发布生成 `Version`，
  **冻结当时采用的证据集合、许可集合与授权覆盖地区**（含规范化 freeze_hash），
  后续许可状态变化不改写历史冻结。
- **更正记录** `Correction`：事实被传承人更正后，已公开作品留痕并触发传播范围重判。
- **争议** `Dispute` 与 **复核任务** `ReviewTask`：创作者本人永远不能关闭自己的争议；
  指纹一致但许可声明不同必须交独立人员复核。所有处置写入只增事件日志。

## 关键业务规则（`service.py`）

1. **指纹复用**：指纹归一化后再次导入，复用已有素材的处理，不重复建流程。
2. **声明冲突独立复核**：同指纹但许可声明不同，生成 `fingerprint_mismatch` 任务；
   不得指派给创作者本人，独立人员（person/team）作出 accept/keep 结论后才能关闭。
3. **佐证与分歧**：结论报告同时返回 supports 与 contradictions，矛盾持续可见。
4. **传承人更正事实**：
   - 旧素材置 `superseded`（内容保留留痕）；
   - 尚未发布的依赖作品置 `held` 停止流转，建 fact_correction 任务；
   - 已公开作品生成更正记录并建 rejudge_scope 任务，`rejudge` 以新事实链路上
     当前有效授权重新判断传播范围（可缩减地区直至清空）；
   - 旧事实上未完成的复核通过 `carried_from` 延续到新事实。
5. **版本冻结**：已被更正的证据不能冻结进新版本；冻结后的许可后来撤回/到期，
   历史版本仍保持原样，但受影响作品进入待处理清单。
6. **许可撤回/到期**：沿图谱找全授权素材的全部下游作品，以及冻结过该许可的
   历史公开版本所属作品——未发布停流，已发布重判；到期按日期批量、幂等处理。
7. **创作者不能关闭自己的争议**：无论争议是否已关闭，创作者执行关闭都被拒绝并审计。
8. **片段溯源** `trace`：任一片段/作品 id 返回来自哪里（root 素材）、
   经历过哪些加工（上游/下游边）、在哪些地区与期限可用（逐条许可状态）、
   冻结版本与更正记录、以及当前必须处理的复核任务。

## 存储（`store.py`）

仅依赖标准库 SQLite。表：records（兼容）、materials、relations、conclusions、
evidence、authorizations、works、versions、corrections、disputes、reviewers、
review_tasks、events。溯源使用 `WITH RECURSIVE` 公共表表达式。

## 命令行（`cli.py`）

```bash
# 健康检查 / 兼容早期基础记录
PYTHONPATH=src python3 -m heritage_media_provenance.cli health
PYTHONPATH=src python3 -m heritage_media_provenance.cli validate data/sample.json

# 装载综合业务场景（可持久化到同一数据库，供后续命令复用）
PYTHONPATH=src python3 -m heritage_media_provenance.cli scenario data/heritage_scenario.json --db /tmp/graph.db

# 审核人员：输入任一片段查看完整溯源与待处理事项
PYTHONPATH=src python3 -m heritage_media_provenance.cli trace clip-cut --db /tmp/graph.db
PYTHONPATH=src python3 -m heritage_media_provenance.cli tasks --db /tmp/graph.db

# 许可处置与更正后重判
PYTHONPATH=src python3 -m heritage_media_provenance.cli revoke --license lic-li-1 --db /tmp/graph.db
PYTHONPATH=src python3 -m heritage_media_provenance.cli expire --as-of 2026-10-07 --db /tmp/graph.db
PYTHONPATH=src python3 -m heritage_media_provenance.cli rejudge --work work-gallery --db /tmp/graph.db

# 争议关闭（创作者本人将被拒绝）与指纹声明冲突复核
PYTHONPATH=src python3 -m heritage_media_provenance.cli close-dispute --dispute dispute-1 --by reviewer-zhang --db /tmp/graph.db
PYTHONPATH=src python3 -m heritage_media_provenance.cli resolve-mismatch --task <id> --reviewer team-ethics --decision keep_existing --db /tmp/graph.db
```

## 场景数据

`data/heritage_scenario.json` 是一份按时间顺序的操作单（47 步），串起：
部分地区授权、剪辑反转口述、AI 未核对译注、佐证与矛盾并存、两个公开版本冻结、
同指纹再导入但声明不同、创作者关闭争议被拒、独立复核、授权到期、传承人更正事实、
未发布草稿停流、已发布作品更正与重判、授权撤回后找全三部受影响作品并延续复核。
`{"op": "clock", "date": ...}` 可拨动判定日期。

## 测试与检查

```bash
PYTHONPATH=src python3 -m unittest discover -s tests   # 33 个用例
python3 -m compileall -q src tests
```

契约说明见 `contracts/record.json`。
