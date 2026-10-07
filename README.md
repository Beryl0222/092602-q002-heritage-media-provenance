# 非遗影像来源与许可

面向非遗短视频申诉场景的**来源与许可图谱**服务端：把原始访谈、手艺步骤、参考文献、翻译说明、剪辑片段、创作者声明、传承人授权和派生作品组织成可互相追溯的图谱，支撑审核人员核验“来自哪里、经历过哪些加工、在哪些地区和期限内可用、当前还有哪些作品必须处理”。

仅使用 Python 标准库与本地 SQLite，无需其他运行服务。

## 领域规则

- **来源图谱**：素材节点（`interview/step/reference/translation/clip/statement/license/work`）通过加工边（剪辑、AI 整理、翻译、混剪……）连接，递归 CTE 支持任意深度上下游追溯。
- **说法与分歧**：素材承载“说法”，说法之间可 `corroborate`（互相佐证，可共同支撑结论）或 `contradict`（内容矛盾，**必须保留分歧**）；证据树存在未解决矛盾的作品禁止发布。
- **指纹复用**：素材按指纹导入，同一指纹再次导入直接复用已有处理；指纹一致但许可声明（`declared_terms`）不同，自动生成**独立人员**复核任务，创作者不能自审。
- **授权范围**：授权按 `territories × valid_from/valid_until` 界定，随加工链向下继承；作品可用地区取各源素材有效授权的交集，超范围发布被拒。
- **传承人更正**：仅传承人本人可更正自己的说法。更正后，**未发布**的依赖作品 `blocked` 停止流转；**已公开**作品置 `corrected`，形成更正记录并重新判断传播范围。
- **版本冻结**：每次发布冻结当时采用的证据（说法）与许可（授权），版本不可变；更正后发布产生新版本并可指向上一版本。
- **授权撤回/到期**：`revoke` 与 `sweep` 找全受影响作品（含冻结版本引用该授权者），已发布下架、延续未完成复核。
- **争议回避**：创作者本人和争议发起者都不能关闭争议；争议未决期间依赖作品禁止发布。

## 目录

- `src/heritage_media_provenance/domain.py` — 领域模型（素材、加工边、说法、授权、争议、复核、公开版本、更正）。
- `src/heritage_media_provenance/store.py` — SQLite 表结构与递归图谱遍历。
- `src/heritage_media_provenance/service.py` — 全部业务规则与溯源查询。
- `src/heritage_media_provenance/cli.py` — 命令行入口。
- `contracts/record.json` — 输入契约说明。
- `tests/` — 基线测试与端到端规则测试。

## 运行

```bash
# 测试
PYTHONPATH=src python3 -m unittest discover -s tests

# 语法检查
python3 -m compileall -q src tests

# 基线样例
PYTHONPATH=src python3 -m heritage_media_provenance.cli validate data/sample.json
```

## 命令行

所有命令共用一个 SQLite 库（`--db` 或环境变量 `HMP_DB`，默认 `heritage_provenance.db`）。写入命令的载荷可以是 JSON 文件，也可以是内联 JSON。

```bash
HMP_DB=case.db python3 -m heritage_media_provenance.cli <命令> [参数]
```

| 命令 | 作用 |
| --- | --- |
| `import <payload>` | 按指纹导入素材（同指纹复用） |
| `derive <payload>` | 登记加工关系（父 → 子） |
| `claim` / `relate` | 登记说法；佐证或矛盾关系 |
| `license` / `attach` | 登记传承人授权（地区、期限、声明）；挂接到素材 |
| `publish <payload>` | 发布作品并冻结证据与许可 |
| `open-dispute` / `close-dispute` | 争议开关（创作者不能自关） |
| `correct <payload>` | 传承人更正说法，联动停转/更正记录/重判范围 |
| `revoke <license_id>` | 撤回授权，找全受影响作品 |
| `sweep [--at]` | 扫描到期授权并延续复核 |
| `resolve-task` / `tasks` | 独立复核闭环 / 复核清单 |
| `availability <work_id>` | 作品地区×期限可用窗口 |
| `pending` | 当前必须处理的作品 |
| `publications [work_id]` | 冻结版本清单 |
| **`trace <material_id>`** | **任一片段的完整溯源** |

`trace clip-001` 返回：

- `sources` / `processing_chain`：来自哪里、自源头起经历的每一步加工与操作人；
- `claims` / `claim_relations` / `contradictions`：采用的说法、佐证与保留的矛盾；
- `licenses` 与作品的 `availability`：在哪些地区、什么期限内可用（窗口及支撑授权）；
- `publications` / `corrections`：冻结的公开版本与更正记录；
- `disputes`：相关争议；
- `downstream_works` / `pending_works` / `open_review_tasks`：片段流向了哪些作品，以及当前必须处理的作品与未完成复核。

### 典型流程

```bash
cli import '{"material_id":"itv-1","material_type":"interview","fingerprint":"fp1","creator_id":"c1","holder_id":"h1"}'
cli license '{"license_id":"l1","fingerprint":"fp1","holder_id":"h1","creator_id":"c1","territories":["CN","SG"],"valid_from":"2026-01-01","valid_until":"2026-12-31","declared_terms":"完整口述可用"}'
cli attach '{"material_id":"itv-1","license_id":"l1"}'
cli import '{"material_id":"clip-1","material_type":"clip","fingerprint":"fp2","creator_id":"c1","holder_id":"h1"}'
cli derive '{"parent_id":"itv-1","child_id":"clip-1","process":"edit","actor_id":"c1"}'
# ... 继续派生作品、登记说法、publish；事后可用 correct / revoke / sweep 联动
cli trace clip-1
```
