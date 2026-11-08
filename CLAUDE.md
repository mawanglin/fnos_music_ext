# fnos_music_ext · native-only 分支（fork）规矩

本仓库 `origin` = `mawanglin/fnos_music_ext`（fork），`upstream` = `javycoder/fnos_music_ext`（官方，Docker + native 双形态）。

## 铁律
- **作者信息一律用 `Cob`，不要用 `mawanglin`**：manifest 的 maintainer、关于页、README、提交的 author/committer（git config 已是 `Cob <Cob@88.com>`，提交时不要用 `-c user.name=` 覆盖）都写 Cob。仓库地址 `github.com/mawanglin/...` 是 GitHub 账号路径，无法改，仅作为 URL 保留。
- 定制改动（fpk 去 Docker、只保留飞牛原生）只在 `native-only` 分支，**永远不合并进 `main`**。用户主动要求合并时也必须**二次确认**。
- `main` 只用来同步上游：`git fetch upstream && git checkout main && git merge --ff-only upstream/main`（或 reset 到 upstream/main），然后把 `native-only` rebase / merge 到最新 `main` 上。
- **永远不要向上游（javycoder）提 PR。**
- 版本号 = 上游基线 + 自有小版本：`VERSION=x.y.z-N`（N≥1）、`RELEASE_CHANNEL=fork`，对外 `x.y.z-nN`（tag `vx.y.z-nN`）。上游基线升级后 N 回到 1。改 VERSION 时同步改 `FNMUSIC_COMPAT.json` 的 `extension_version` 和 `CHANGELOG.md` 的 `## [x.y.z-nN]` 段。
- 发布：推 `native-only` 分支后，在其上打 tag `vx.y.z-nN` 并推送，触发 `.github/workflows/release.yml`（fork 版）。

## 与上游的差异面（rebase 时重点看）
`install.sh`/`extend.sh` 的部署形态解析、`packaging/fpk/**`（cmd、build.sh、manifest.in）、`proxy/version.py`（fork 通道）、`webui-service/app.py`（更新源、关于页）、`.github/workflows/{ci,release}.yml`、相关测试。Docker 代码在 install.sh/extend.sh 里仍保留为死代码，只为让与上游的 diff 最小。

## 内置运行时（fpk 全集成，不碰系统）
- fpk 按架构各出一个：`packaging/fpk/build.sh`（默认 x86 + arm），产物 `dist/fnmusic-ext-<版本>-<x86|arm>.fpk`。
- 内置内容由 `packaging/fpk/fetch_runtime.py` 按 `packaging/fpk/runtime/runtime.json` + `requirements-{proxy,sources}.lock` 生成到包内 `app/repo/runtime/`：Python（python-build-standalone）、依赖 wheel（含 nodejs-wheel，Node 即取自其中）、ffmpeg 静态构建。所有下载校验 sha256，改版本必须同步改校验和。
- 安装脚本以 `runtime/BUNDLED` 判断内置模式（`proxy/runtime_common.sh` 的 `fnmusic_bundled` / `bundled_make_venv`）：离线用内置 Python 建 venv 并挂载依赖目录，**绝不调用 apt / pip / 联网**。无标记（源码安装）保持上游行为。
- 升级依赖：在 x86 上用内置 Python 新建 venv 装一遍，`pip freeze` 覆盖两个 `.lock`（去掉 pip 本身），再 `fetch_runtime.py` 验证 x86 与 arm 都能取到 wheel。只有源码包的依赖必须是纯 Python（否则构建中止）。
- 仍用系统自带的 `/usr/bin/python3` 的只有 `takeover.py` 等只依赖标准库的辅助脚本（系统自带，不安装任何东西）。

## 测试
`python3 scripts/run_tests.py`（全量，较慢）；打包相关：`python3 -m pytest packaging/tests`。
