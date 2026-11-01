# health-agent

一个由 dot 执行的健康日记 **skill**，不是独立 agent。核心只有 `SKILL.md` 与纯标准库 `health.py`：聊天理解和分析由 dot 完成，SQLite 脚本负责可追溯记录、区间汇总、备份和恢复。没有服务端、网页、后台任务或模型 API。

## 使用

Python 3.10+；无需安装依赖。详细 JSON 协议、字段和操作流程见 [SKILL.md](SKILL.md)。

```sh
export HEALTH_AGENT_DB=/private/health-data/diary.sqlite3
printf '%s' '{"action":"init"}' | python health.py
python -m unittest discover -s tests -v
```

请将目录作为 skill 安装到宿主支持的位置，或让 dot 读取 `SKILL.md` 并使用脚本绝对路径。本仓库不会自动修改宿主技能配置。

## 数据边界

- 代码可公开；健康数据、照片、备份、账号令牌不得提交。脚本拒绝在自身目录内存储数据库/备份，`.gitignore` 是附加防护而非安全边界。
- 默认路径 `~/.local/share/health-agent/diary.sqlite3`，可用环境变量或 `--db` 更改。需自行确认运行环境的存储耐久性；这不是云服务或同步保障。
- 文件尽量使用本机私有权限，但 SQLite/ZIP **没有加密**；有系统访问权的人可能读取。备份包含编辑历史和软删除记录。
- 照片不由脚本处理；只保留来源引用。数值由用户、标签、设备或 dot 提供，需注明不确定性。不做医疗诊断或自动饮食/运动处方。
- 统计区分未记录与真实零值；全天总消耗包含运动，不重复叠加。部分记录只给已知小计，不冒充完整总量。
- 恢复要求显式 merge/replace，验证 ZIP 内容、校验和、数据库结构与数据，并在覆盖前留备份。合并冲突保留本地值。仅恢复可信来源文件；校验和不是签名。

限制：单人日记；无联网、照片上传、自动备份/手机同步；日期由调用者按用户时区明确提供。备份最多 256 MiB、单文件最多 128 MiB。不要在备份恢复期间并发写入；本版面向 dot 串行操作。
