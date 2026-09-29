# FuckClassroom 课程录播插件

FuckClassroom 的独立课程录播插件，插件 ID 为 `classroom`。

## 功能

- 课程与学期同步
- 课程课次、直播状态、回放与 PPT 查看
- 直播 HLS / PPT 代理
- 课程资源与本地下载文件管理
- 课程录播登录与会话恢复
- 向其他插件提供 `classroom_client`、`semester_sync` 等基础服务

## 兼容性

- FuckClassroom: `>=0.1,<0.3`
- Plugin API: `1`
- Required host plugin: `core_ui`
- Python dependency: `playwright>=1.45`

当前版本仍复用 FuckClassroom 宿主提供的 `fuckclassroom.classroom`、认证、Plugin Process Host 等稳定兼容层；业务插件代码本身已从主仓插件包层级中拆出。

## 开发

实际开发在 `plugin-management` 分支进行。合并到 `main` 后，CI 成功会自动发布 `0.1.0-beta.N` Registry v1 prerelease。

Registry v1 Release ZIP 的根目录直接包含 `plugin.json`，不额外套 `classroom/` 目录。
