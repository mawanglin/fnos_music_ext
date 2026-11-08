# fnos_music_ext · native-only 分支（fork）规矩

本仓库 `origin` = `mawanglin/fnos_music_ext`（fork），`upstream` = `javycoder/fnos_music_ext`（官方，Docker + native 双形态）。

## 铁律
- 定制改动（fpk 去 Docker、只保留飞牛原生）只在 `native-only` 分支，**永远不合并进 `main`**。用户主动要求合并时也必须**二次确认**。
- `main` 只用来同步上游：`git fetch upstream && git checkout main && git merge --ff-only upstream/main`（或 reset 到 upstream/main），然后把 `native-only` rebase / merge 到最新 `main` 上。
- **永远不要向上游（javycoder）提 PR。**
- 版本号 = 上游基线 + 自有小版本：`VERSION=x.y.z-N`（N≥1）、`RELEASE_CHANNEL=fork`，对外 `x.y.z-nN`（tag `vx.y.z-nN`）。上游基线升级后 N 回到 1。改 VERSION 时同步改 `FNMUSIC_COMPAT.json` 的 `extension_version` 和 `CHANGELOG.md` 的 `## [x.y.z-nN]` 段。
- 发布：推 `native-only` 分支后，在其上打 tag `vx.y.z-nN` 并推送，触发 `.github/workflows/release.yml`（fork 版）。

## 与上游的差异面（rebase 时重点看）
`install.sh`/`extend.sh` 的部署形态解析、`packaging/fpk/**`（cmd、build.sh、manifest.in）、`proxy/version.py`（fork 通道）、`webui-service/app.py`（更新源、关于页）、`.github/workflows/{ci,release}.yml`、相关测试。Docker 代码在 install.sh/extend.sh 里仍保留为死代码，只为让与上游的 diff 最小。

## 测试
`python3 scripts/run_tests.py`（全量，较慢）；打包相关：`python3 -m pytest packaging/tests`。
