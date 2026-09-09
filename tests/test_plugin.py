"""Both hosts share skills; only Claude discovers bundled capture hooks."""
import json
from pathlib import Path
import os
import subprocess

ROOT = Path(__file__).resolve().parents[1] / 'plugin'


def test_shared_skills_with_one_capture_installation_per_host(tmp_path):
    claude = json.loads((ROOT / '.claude-plugin/plugin.json').read_text())
    codex = json.loads((ROOT / '.codex-plugin/plugin.json').read_text())
    assert claude['name'] == codex['name'] == 'sessionator'
    assert (ROOT / codex['skills']).resolve() == ROOT / 'skills'
    assert 'hooks' not in codex
    assert not (ROOT / 'hooks/hooks.json').exists()
    for skill in (ROOT / 'skills').glob('*/SKILL.md'):
        assert 'CLAUDE_PLUGIN_ROOT' not in skill.read_text()

    # The Claude hook still executes when its plugin path contains spaces.
    plugin = tmp_path / 'plugin with spaces'
    import shutil
    shutil.copytree(ROOT, plugin)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    receipt = tmp_path / 'receipt'
    fake = bin_dir / 'sessionator'
    fake.write_text('#!/bin/sh\ncat > "$RECEIPT"\n')
    fake.chmod(0o755)
    env = dict(os.environ, CLAUDE_PLUGIN_ROOT=str(plugin),
               CLAUDE_PROJECT_DIR=str(tmp_path), HOME=str(tmp_path),
               PATH=str(bin_dir) + ':/usr/bin:/bin', RECEIPT=str(receipt))
    hooks = json.loads((plugin / claude['hooks']).read_text())['hooks']
    for event in ('PreCompact', 'SessionEnd'):
        command = hooks[event][0]['hooks'][0]['command']
        result = subprocess.run(command, shell=True, input='{"test":true}',
                                text=True, capture_output=True, env=env)
        assert result.returncode == 0
        assert result.stdout == result.stderr == ''
        assert receipt.read_text() == '{"test":true}'
