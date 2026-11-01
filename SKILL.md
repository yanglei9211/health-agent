---
name: health-agent
description: "Record and review a private health diary with dot: meals, nutrition estimates, sleep, activity, body metrics, descriptive trends, and portable backups. Dot interprets the conversation; a local SQLite script preserves the records."
---

# 健康记录 Skill

你（dot）就是 agent。`health.py` 只做校验、存取、汇总和备份，不调用模型或外部服务。

## 操作约定

- 首次使用确认用户的本地时区及其愿意提供的基础信息；缺少的年龄、身高、目标等不要编造。用 profile 记录，后续读取最新有效记录。
- 仅在用户实际报告已发生的饮食/活动或明确要求登记时记录；假设、计划、菜品咨询、示例和“如果吃这个呢”不得写成实际摄入。不确定时先确认。
- 从聊天理解餐食、睡眠、活动、运动及身体指标。日期按用户本地日历；跨午夜/补记不明时先确认。不要默认使用服务器日期。
- 照片只由当前会话中 dot 实际查看后估算；脚本不接收或识别照片。只保存允许留存的引用，不下载原图，不保存带令牌的 URL。
- 营养估算记录区间、方法、份量假设和不确定性；不把估算表述成实测。标签/秤/设备值也注明来源。不要为了凑齐字段而填零。
- 稳定 `source_id` 用“渠道:消息ID:条目序号”。重放同一内容会去重；相同来源但内容不同会拒绝，须明确编辑。无法获得消息ID时生成一次唯一ID并复用，不每次重试新建ID。
- 编辑先 `get`，提交原 `updated_at`；payload 是完整替换，不是局部补丁。日期/类型错误可明确软删除旧记录并新建，保留审计。不得悄悄改写历史。
- 用户确认当日饮食记全时才设 `intake_complete:true`。无餐但明确完整表示零摄入；未记完整时空记录表示未知。一天只允许一个有效 daily 记录。
- `expenditure_kcal` 只能是**已包含基础代谢、活动与运动的全天总消耗**，显式写 `expenditure_basis`。运动单独记录但不得再加到全天总消耗。脚本不估算代谢率、不提供诊断或处方。
- 汇总先读取脚本结果。只有饮食热量完整且全天总消耗存在，才计算能量差区间；低值=摄入低值−消耗高值，高值反之。`calorie_deficit_kcal` 是热量缺口：消耗−摄入（低值=消耗低值−摄入高值），正数为缺口，负数为盈余；`energy_balance_kcal` 是相反符号的摄入−消耗。趋势保留缺失日期，不暗示单日体重等于脂肪变化。

## 调用

Python 3.10+，纯标准库。使用技能目录的绝对路径。JSON 从 stdin 输入，避免把健康内容写入命令参数/公共文件。

```sh
printf '%s' '{"action":"init"}' | python /path/to/health-agent/health.py --db /private/path/diary.sqlite3
```

路径通过 `--db` 或 `HEALTH_AGENT_DB` 设置，默认 `~/.local/share/health-agent/diary.sqlite3`。**必须在代码目录外**。未验证环境持久性前，不承诺云端永久保存。只在用户授权的环境存储，代码可公开、数据不能公开。

每次只传一个 JSON 对象；成功 `{ok:true,result:...}`，失败 `{ok:false,error:...}` 且退出码 1。只在成功后告诉用户已记录。

| action | 参数 |
|---|---|
| init | 无 |
| add | kind, day, source_id, payload |
| get | ident |
| edit | ident, expected_updated_at, payload（完整替换） |
| delete | ident, expected_updated_at（软删除） |
| list | start?, end?, kind?, include_deleted? |
| daily | day |
| trends | start, end（最多366天） |
| audit | ident? |
| backup | output_path（不可覆盖、代码目录外） |
| inspect | zip_path |
| restore | zip_path, mode（必须为 merge 或 replace） |

所有日期 `YYYY-MM-DD`。所有 payload 均含 `provenance:{method:"user_report|label|device|agent_estimate|其他说明",uncertainty:"说明"}`；agent_estimate 必须有 uncertainty。可带 note、label、source_refs（字符串列表）。

`metrics` 每项为 `{low:数值,high:数值}`；实测可上下相同。缺失可省略或 null。只能是 0..1e9 内的有限数值（防止异常输入与统计溢出）。

| kind | metrics / 附加字段 |
|---|---|
| profile | 无 metrics；timezone、height_cm、birth_year、goals、preferences 可选 |
| meal | kcal、protein_g、carbs_g、fat_g |
| sleep | hours |
| activity | steps、active_minutes |
| workout | minutes、exercise_kcal |
| body | weight_kg、body_fat_pct、waist_cm |
| daily | expenditure_kcal；intake_complete、expenditure_basis |

虚构示例：
```json
{"action":"add","kind":"meal","day":"2026-01-01","source_id":"example:message-1:0","payload":{"label":"示例餐食","metrics":{"kcal":{"low":450,"high":600},"protein_g":{"low":20,"high":30}},"provenance":{"method":"agent_estimate","uncertainty":"份量和烹调油未知，仅为示例估算"}}}
```

## 备份和恢复

- 用户要备份时生成 ZIP，调用 inspect 验证后通过环境支持的私密文件交付能力发送；生成本地路径不等于已发送。不把备份放 GitHub。
- ZIP 含一致性 SQLite、JSON、CSV、说明及 SHA-256 manifest。**未加密**，包含软删除数据与审计；告知用户妥善保存。CSV 防公式注入，原始值在 SQLite/JSON。
- 用户上传恢复文件后先 inspect，仅接受可信来源。校验和能检测损坏，不能证明来源可信。
- 说明记录数量并确认 merge 或 replace。merge 保留本地冲突并报告，不静默覆盖；replace 覆盖当前库。先自动生成恢复前备份，成功后报告数量和冲突。不得因失败换路径偷偷绕过限制。
- 不安排自动备份、手机同步或外部传输，除非另有明确授权。审计中的软删除不是彻底抹除；需要彻底删除时另行处理数据库与全部副本。
