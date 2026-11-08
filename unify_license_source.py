# SPDX-FileCopyrightText: 2026 Toyoaki Nishida
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# unify_license_source.py — ライセンスヘッダの Source 行をサイトの系統に揃える
#
# 使い方（ホームディレクトリで実行）：
#   python3 unify_license_source.py kenshu          # 研修用（nishida-toyoaki/fujin-p）に揃える．点検だけ
#   python3 unify_license_source.py daigaku         # 大学用（u-fukuchiyama/fujin-p）に揃える．点検だけ
#   python3 unify_license_source.py kenshu --yes    # 実際に書き換える（変更前のファイルを tar.gz に退避）
#   python3 unify_license_source.py kenshu --add-missing --yes
#                                                   # あわせてヘッダの無い .py／.html にヘッダを付ける
#
# 書き換えるもの：
#   1. ヘッダ（先頭40行）の「Source: https://github.com/<相手側>/fujin-p」
#   2. add_license_headers.py の SOURCE = "..." の1行
#   3. templates/login.html の GitHub へのリンク（AGPL 表示）
#   4. ヘッダに Source 行が無い短い形（{# SPDX... This file is part of FUJIN-P... #}）を標準形に直す
#   5. --add-missing を付けたとき，ヘッダの無い .py／.html に標準のヘッダを付ける
#      （static_for_distribution の規約類は利用者にそのまま配信されるので付けない．他者の著作権表示があるものも付けない）
# 書き換えないもの（報告だけ）：
#   ・本文中にある相手側リポジトリの URL（インストール手順書のリンクなど）
#   ・ヘッダの無い .py／.html
# 対象：ホーム直下のファイル，templates/，static_for_distribution/，fujinp/ 配下
#        （config.py・import_backups・各アプリの static/ data/ img/・sql_saver_work・__pycache__ は除外）

import os
import re
import sys
import tarfile
import time

REPOS = {
    'kenshu': 'https://github.com/nishida-toyoaki/fujin-p',
    'daigaku': 'https://github.com/u-fukuchiyama/fujin-p',
}
EXPECT_USER = {'kenshu': 'nishida4fujinp', 'daigaku': 'fujinp'}
EXTS = ('.py', '.html', '.sql', '.js', '.css', '.md')
SKIP_DIRS = {'import_backups', '__pycache__', 'static', 'data', 'img', 'sql_saver_work', '.git'}
SKIP_FILES = {'config.py', 'unify_license_source.py'}
ANY_REPO = re.compile(r'https://github\.com/(?:u-fukuchiyama|nishida-toyoaki)/fujin-p')
SHORT_HTML = re.compile(
    r'\A\{# SPDX-FileCopyrightText: (?P<years>[^\n]+)\n'
    r'\s*SPDX-License-Identifier: AGPL-3\.0-or-later\n'
    r'\s*This file is part of FUJIN-P\. See <https://www\.gnu\.org/licenses/>\. #\}(?P<nl>\r?\n)')


def targets(home):
    for name in sorted(os.listdir(home)):
        p = os.path.join(home, name)
        if os.path.isfile(p) and name.endswith(EXTS) and name not in SKIP_FILES:
            yield p
    for root_name in ('templates', 'static_for_distribution', 'fujinp'):
        root = os.path.join(home, root_name)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            rel = os.path.relpath(dirpath, home)
            # fujinp/<app>/static などアプリの作業データは除外（static_for_distribution 自体は対象）
            dirnames[:] = sorted(d for d in dirnames
                                 if not (d in SKIP_DIRS and rel.startswith('fujinp')) and d not in ('__pycache__', '.git', 'import_backups'))
            for fn in sorted(filenames):
                if fn.endswith(EXTS) and fn not in SKIP_FILES:
                    yield os.path.join(dirpath, fn)


YEARS = '2024-2026'
HOLDER = 'Toyoaki Nishida'


def header_for(path, want, nl):
    if path.endswith('.py'):
        body = ['# SPDX-FileCopyrightText: %s %s' % (YEARS, HOLDER),
                '# SPDX-License-Identifier: AGPL-3.0-or-later',
                '#',
                '# This file is part of FUJIN-P.',
                '# Copyright (C) %s %s' % (YEARS, HOLDER),
                '#',
                '# FUJIN-P is free software: you can redistribute it and/or modify',
                '# it under the terms of the GNU Affero General Public License as published by',
                '# the Free Software Foundation, either version 3 of the License, or',
                '# (at your option) any later version.',
                '#',
                '# FUJIN-P is distributed in the hope that it will be useful,',
                '# but WITHOUT ANY WARRANTY; without even the implied warranty of',
                '# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the',
                '# GNU Affero General Public License for more details.',
                '#',
                '# You should have received a copy of the GNU Affero General Public License',
                '# along with FUJIN-P.  If not, see <https://www.gnu.org/licenses/>.',
                '#',
                '# Source: ' + want,
                '']
    else:
        body = ['{#',
                '  This file is part of FUJIN-P.',
                '  SPDX-FileCopyrightText: %s %s' % (YEARS, HOLDER),
                '  SPDX-License-Identifier: AGPL-3.0-or-later',
                '  Source: ' + want,
                '#}']
    return nl.join(body) + nl


def add_header(path, text, want):
    nl = '\r\n' if '\r\n' in text else '\n'
    hdr = header_for(path, want, nl)
    if path.endswith('.py'):
        lines = text.split(nl)
        k = 0
        # シバン行と文字コード指定行は先頭に残す
        while k < len(lines) and k < 2 and (lines[k].startswith('#!') or re.match(r'#.*coding[:=]', lines[k])):
            k += 1
        head = nl.join(lines[:k]) + (nl if k else '')
        return head + hdr + nl.join(lines[k:])
    return hdr + text


def convert(path, text, want):
    """書き換え後の本文と，変更点の説明リストを返す"""
    notes = []
    nl = '\r\n' if '\r\n' in text else '\n'
    lines = text.split('\n')
    head = min(40, len(lines))
    for i in range(head):
        m = re.search(r'Source:\s*(https://github\.com/[\w.-]+/fujin-p)', lines[i])
        if m and m.group(1) != want:
            lines[i] = lines[i].replace(m.group(1), want)
            notes.append('Source行')
    new = '\n'.join(lines)
    base = os.path.basename(path)
    if base == 'add_license_headers.py':
        new2 = re.sub(r'^SOURCE = "https://github\.com/[\w.-]+/fujin-p"', 'SOURCE = "%s"' % want, new, flags=re.M)
        if new2 != new:
            notes.append('SOURCE定数')
            new = new2
    if path.endswith(os.path.join('templates', 'login.html')) and '/fujinp/' not in path:
        new2 = re.sub(r'href="https://github\.com/[\w.-]+/fujin-p"', 'href="%s"' % want, new)
        if new2 != new:
            notes.append('ログイン画面のリンク')
            new = new2
    if path.endswith('.html'):
        m = SHORT_HTML.match(new)
        if m:
            n = m.group('nl')
            std = ('{#' + n + '  This file is part of FUJIN-P.' + n +
                   '  SPDX-FileCopyrightText: ' + m.group('years') + n +
                   '  SPDX-License-Identifier: AGPL-3.0-or-later' + n +
                   '  Source: ' + want + n + '#}' + n)
            new = std + new[m.end():]
            notes.append('短いヘッダを標準形に')
    return new, notes


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args or args[0] not in REPOS:
        print('使い方: python3 unify_license_source.py kenshu|daigaku [--yes]')
        sys.exit(1)
    site = args[0]
    want = REPOS[site]
    apply = '--yes' in sys.argv
    add_missing = '--add-missing' in sys.argv
    home = os.path.expanduser('~')
    user = os.environ.get('USER', '')
    if user and user != EXPECT_USER[site] and '--force' not in sys.argv:
        print('このアカウント（%s）は %s 用ではありません．間違いでなければ --force を付けてください．' % (user, site))
        sys.exit(1)

    changed, leftover, no_header, no_source = [], [], [], []
    for path in targets(home):
        try:
            raw = open(path, 'rb').read()
            text = raw.decode('utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        new, notes = convert(path, text, want)
        rel = os.path.relpath(path, home)
        if (add_missing and path.endswith(('.py', '.html'))
                and not rel.startswith('static_for_distribution')
                and 'SPDX-License-Identifier' not in '\n'.join(new.split('\n')[:40])
                and 'Copyright' not in new):
            new = add_header(path, new, want)
            notes.append('ヘッダを付けた')
        if notes:
            changed.append((path, rel, new, notes))
        for m in ANY_REPO.finditer(new):
            if m.group(0) != want:
                ln = new.count('\n', 0, m.start()) + 1
                leftover.append('%s:%d' % (rel, ln))
        if path.endswith(('.py', '.html')):
            top = '\n'.join(new.split('\n')[:40])
            if 'SPDX-License-Identifier' not in top:
                no_header.append(rel)
            elif 'Source:' not in top:
                no_source.append(rel)

    print('=== 系統: %s → %s' % (site, want))
    print('書き換え対象: %d 件' % len(changed))
    for _, rel, _, notes in changed:
        print('  %s  [%s]' % (rel, '・'.join(sorted(set(notes)))))
    print('本文に残る相手側リポジトリの URL（書き換えない）: %d 件' % len(leftover))
    for x in leftover:
        print('  ' + x)
    print('Source 行の無いヘッダ（書き換えない）: %d 件' % len(no_source))
    for x in no_source:
        print('  ' + x)
    print('ヘッダの無い .py／.html（--add-missing を付けなければそのまま）: %d 件' % len(no_header))
    for x in no_header:
        print('  ' + x)

    if not apply:
        print('\n点検だけです．書き換えるには --yes を付けて実行してください．')
        return
    if not changed:
        print('\n書き換えるものはありません．')
        return
    stamp = time.strftime('%Y%m%d_%H%M%S')
    bk = os.path.join(home, 'license_unify_backup_%s.tar.gz' % stamp)
    with tarfile.open(bk, 'w:gz') as tf:
        for path, rel, _, _ in changed:
            tf.add(path, arcname=rel)
    for path, _, new, _ in changed:
        with open(path, 'wb') as f:
            f.write(new.encode('utf-8'))
    print('\n%d 件を書き換えました．変更前のファイルは %s に退避しました．' % (len(changed), bk))


if __name__ == '__main__':
    main()
