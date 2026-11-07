# 红警网页适配补丁

上游： https://github.com/ra2-games/ra2
固定基线： `fc4dd415628216e21e09e80d233ba228692b12dc`
许可： GPL-3.0-or-later（此目录的补丁和上游衍生代码；见 LICENSE）。不包含游戏资产。

在新的检出目录应用，不要对正在玩的目录操作：

```powershell
git clone https://github.com/ra2-games/ra2 ra2-dafeiyu
cd ra2-dafeiyu
git checkout fc4dd415628216e21e09e80d233ba228692b12dc
git apply --check <插件目录>/adapters/ra2/dafeiyu.patch
git apply <插件目录>/adapters/ra2/dafeiyu.patch
```

按上游 README 安装其指定 pnpm、编译器与构建依赖，再执行 `pnpm install --frozen-lockfile`、`pnpm run build`。开发预览执行 `pnpm exec vite --config vite.dafeiyu.config.ts`（仅监听 127.0.0.1:15184）。控制服务启动方式以该版本 `.agents/skills/ra2-battle-control/SKILL.md` 为准；插件 gameSkill 指向这个技能目录。

补丁包括：保存速度的续局、工程师占领和迷雾观察、音轨与 WebRTC 画面、后台渲染恢复。真实游戏仍需要使用者提供合法资源、导入并连接控制频道。没有嵌入任何个人配置或资源路径。补丁应用检查不等于新机完整构建或真实对局验收。
