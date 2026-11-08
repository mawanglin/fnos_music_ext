import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_notes_correction_only_updates_existing_release_body(tmp_path):
    workflow = (ROOT / '.github/workflows/release-notes.yml').read_text()
    assert 'paths: [CHANGELOG.md, .github/workflows/release-notes.yml]' in workflow
    script = workflow.split('        run: |\n', 1)[1]
    script = '\n'.join(line[10:] for line in script.splitlines())
    path = tmp_path / 'notes.sh'
    path.write_text(script)
    subprocess.run(['bash', '-n', str(path)], check=True)
    assert 'git diff --exit-code "$tag" HEAD' in script
    assert 'git merge-base --is-ancestor "$tag" HEAD' in script
    assert 'gh release edit "$tag" --notes-file' in script
    assert 'diff -u /tmp/identity-before.json /tmp/identity-after.json' in script
    assert 'gh release upload' not in script
    assert 'gh release create' not in script
    assert 'git push' not in script


@pytest.mark.parametrize('case,success,edited', [
    ('unreleased-runtime', True, False),
    ('changed-runtime', False, False),
    ('correction-only', True, True),
    ('missing-section', False, False),
    ('numeric-migration', True, False),
])
def test_notes_correction_skips_unchanged_section_but_guards_real_edits(tmp_path, case, success, edited):
    if not shutil.which('jq'):
        pytest.skip('release shell sandbox requires jq')
    repo = tmp_path / 'repo'
    repo.mkdir()

    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True,
                              capture_output=True, text=True)

    git('init', '-q')
    git('config', 'user.name', 'Release Test')
    git('config', 'user.email', 'release@example.invalid')
    (repo / 'proxy').mkdir()
    shutil.copyfile(ROOT / 'proxy/version.py', repo / 'proxy/version.py')
    (repo / 'VERSION').write_text('2.10.0b\n')
    initial = ('# Changelog\n\n## [未发布]\n\n## [2.10.0b]\n\nPublished notes.\n'
               '\n## [2.9.0]\n' + 'Historical notes.\n' * 10000)
    (repo / 'CHANGELOG.md').write_text(initial)
    (repo / 'runtime.py').write_text('value = 1\n')
    git('add', '.')
    git('commit', '-qm', 'release')
    git('tag', 'v2.10.0b')
    updated = initial.replace('## [未发布]\n', '## [未发布]\n\nUnreleased fix.\n')
    if case in ('changed-runtime', 'correction-only'):
        updated = updated.replace('Published notes.', 'Corrected published notes.')
    elif case == 'missing-section':
        updated = updated.split('## [2.10.0b]')[0]
    elif case == 'numeric-migration':
        (repo / 'VERSION').write_text('2.10.0-1\n')
        updated = updated.replace('Published notes.', 'New package notes.')
    if case != 'correction-only':
        (repo / 'runtime.py').write_text('value = 2\n')
    (repo / 'CHANGELOG.md').write_text(updated)
    git('add', '.')
    git('commit', '-qm', 'update')
    state = tmp_path / 'calls.json'
    state.write_text('[]')
    gh = tmp_path / 'gh'
    gh.write_text(f'#!{sys.executable}\n' + textwrap.dedent('''\
        import json, os, sys
        from pathlib import Path
        path = Path(os.environ['MOCK_CALLS'])
        calls = json.loads(path.read_text())
        args = sys.argv[1:]
        calls.append(args)
        path.write_text(json.dumps(calls))
        notes = Path(os.environ['MOCK_NOTES'])
        if args == ['api', 'repos/example/music/releases/tags/v2.10.0b']:
            print(json.dumps(dict(id=1, tag_name='v2.10.0b', draft=False,
                prerelease=True, assets=[dict(id=2, name='package.fpk', size=123,
                digest='sha256:unchanged')], body=notes.read_text())))
        elif args[:3] == ['release', 'edit', 'v2.10.0b']:
            assert args[3] == '--notes-file'
            notes.write_text(Path(args[4]).read_text())
        else:
            sys.exit(99)
    '''))
    gh.chmod(0o755)
    notes = tmp_path / 'published.md'
    notes.write_text('Published notes.\n')
    workflow = (ROOT / '.github/workflows/release-notes.yml').read_text()
    script = '\n'.join(line[10:] for line in workflow.split('        run: |\n', 1)[1].splitlines())
    # Each sandbox owns the workflow scratch files as well as its fake gh state.
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    script = script.replace('/tmp/', str(scratch) + '/')
    env = dict(os.environ, PATH=f'{tmp_path}:{os.environ["PATH"]}',
               GH_REPO='example/music', MOCK_CALLS=str(state), MOCK_NOTES=str(notes))
    result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], cwd=repo,
                            env=env, capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) is success, result.stdout + result.stderr
    calls = json.loads(state.read_text())
    assert any(call[:2] == ['release', 'edit'] for call in calls) is edited
    if not edited:
        assert calls == []
    else:
        assert notes.read_text().strip() == 'Corrected published notes.'


def test_formal_210_documents_changes_since_290():
    notes = (ROOT / 'CHANGELOG.md').read_text().split('## [2.10.0]', 1)[1].split('\n## [', 1)[0]
    for required in ('2.9.0', '猜你喜欢', '深浅色主题', '公共歌单', '垃圾清理', 'ARM64',
                     '#82', '无损', '红心', '搜索超时'):
        assert required in notes


def test_formal_2110_documents_cumulative_changes_since_2100():
    notes = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8').split(
        '## [2.11.0]', 1)[1].split('\n## [', 1)[0]
    assert 'Pre-release' not in notes
    assert '非正式预发布' not in notes
    for required in ('v2.10.0', 'v2.11.0', '2.10.1', '2.10.2', '2.11.0-4',
                     '### 新增', '### 修复', '### 安装与维护', '播放直连', '远程目录',
                     '洛雪', 'Chat Completions', 'Responses', 'token', '飞牛用户',
                     '公共歌单', '#83', '#84', '#85', '#96', '1.0.10'):
        assert required in notes
    assert '微信' not in notes


RELEASE_WORKFLOW = ROOT / '.github/workflows/release.yml'


@pytest.mark.parametrize("version,channel,tag,formal,success", [
    ("2.11.0-4", "stable", "v2.11.0", "true", True),
    ("2.11.0-26", "stable", "v2.11.0", "true", True),
    ("2.11.0-0", "prerelease", "v2.11.0a", "false", True),
    ("2.11.0-1", "prerelease", "v2.11.0b", "false", True),
    ("2.11.0-4", "prerelease", "v2.11.0e", "false", True),
    ("2.11.0-4", None, "v2.11.0", "", False),
    ("2.11.0-4", "", "v2.11.0", "", False),
    ("2.11.0-4", "formal", "v2.11.0", "", False),
    ("2.11.0-4", "stable", "v2.11.0e", "", False),
    ("2.11.0-4", "prerelease", "v2.11.0", "", False),
    ("2.11.0", "stable", "v2.11.0", "", False),
    ("2.11.0-0", "prerelease", "v2.11.0-0", "", False),
    ("2.11.0-26", "prerelease", "v2.11.0z", "", False),
    ("2.11.0a", "prerelease", "v2.11.0a", "", False),
])
def test_release_ci_validates_tag_and_derives_kind(tmp_path, version, channel, tag, formal, success):
    (tmp_path / "proxy").mkdir()
    shutil.copyfile(ROOT / "proxy/version.py", tmp_path / "proxy/version.py")
    (tmp_path / "VERSION").write_text(version)
    if channel is not None:
        (tmp_path / "RELEASE_CHANNEL").write_text(channel)
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _release_step("Verify tag matches VERSION")],
        cwd=tmp_path, env=dict(os.environ, TAG=tag, GITHUB_OUTPUT=str(output)),
        capture_output=True, text=True, timeout=10,
    )
    assert (result.returncode == 0) is success, result.stdout + result.stderr
    if success:
        values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        assert values["version"] == version
        assert values["tag"] == tag
        assert values["display_version"] == tag[1:]
        assert values["ui_version"] == tag
        assert values["channel"] == channel
        assert values["formal"] == formal
    else:
        assert not output.exists()


def _release_step(name):
    workflow = RELEASE_WORKFLOW.read_text()
    block = workflow.split(f'      - name: {name}\n', 1)[1]
    block = block.split('\n      - ', 1)[0]
    script = block.split('        run: |\n', 1)[1]
    # Stop at comments / the next job outside the run block.
    return '\n'.join(line[10:] for line in script.splitlines()
                     if line.startswith('          ') or not line.strip())


def test_fork_release_workflow_contract():
    """native-only fork：只发 GitHub Release，不回填 moo、不发 Gitee、不要求合入 main。"""
    import yaml
    text = RELEASE_WORKFLOW.read_text()
    workflow = yaml.safe_load(text)
    assert set(workflow["jobs"]) == {"release"}
    assert workflow[True]["push"]["tags"] == ["v*-n*"]  # PyYAML 把 on 解析成 True
    assert "origin/main" not in text, "fork 发布不得要求 tag 合入 main"
    assert "gitee.com" not in text and "update_moo_index" not in text
    assert "Remove previous published prereleases" not in text
    assert "--latest" in text and "--prerelease=false" in text


@pytest.mark.parametrize("version,tag,success", [
    ("2.12.3-1", "v2.12.3-n1", True),
    ("2.12.3-12", "v2.12.3-n12", True),
    ("2.12.3-0", "v2.12.3-n0", False),   # fork 小版本号必须 ≥1
    ("2.12.3-1", "v2.12.3", False),       # 不得与上游 tag 同名
    ("2.12.3-1", "v2.12.3-n2", False),
    ("2.12.3", "v2.12.3-n1", False),
])
def test_fork_channel_version_and_tag(tmp_path, version, tag, success):
    (tmp_path / "proxy").mkdir()
    shutil.copyfile(ROOT / "proxy/version.py", tmp_path / "proxy/version.py")
    (tmp_path / "VERSION").write_text(version)
    (tmp_path / "RELEASE_CHANNEL").write_text("fork")
    result = subprocess.run(
        [sys.executable, str(tmp_path / "proxy/version.py"), "--tag", tag, "--format", "ui_version"],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert (result.returncode == 0) is success, result.stdout + result.stderr
    if success:
        assert result.stdout.strip() == tag


def test_repository_is_on_fork_channel_with_matching_tag():
    version = (ROOT / "VERSION").read_text().strip()
    assert (ROOT / "RELEASE_CHANNEL").read_text().strip() == "fork"
    base, _, revision = version.partition("-")
    assert int(revision) >= 1
    notes = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{base}-n{int(revision)}]" in notes
