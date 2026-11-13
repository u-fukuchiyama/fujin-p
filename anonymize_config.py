#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2024-2026 Toyoaki Nishida
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# anonymize_config.py — config.py を匿名化して config_template.py の形で出力する
#
#   さいまる（migrate_fujinp_scions/routes.py）の匿名化処理
#   _anonymize_config_source() と伏せ字漏れ検査 scan_source_for_secrets() を
#   そのまま抜き出し，Flask なしで単独に動くようにしたもの．
#
#   使い方:  python3 anonymize_config.py ~/config.py > ~/config_template_new.py
#   伏せ字漏れの検査結果は標準エラーに出る（値そのものは出さない）．
#
#   さいまるの処理に足したもの（2026-11-10）:
#     - 元の config.py の先頭にあるライセンス表示を取り除き，出力の先頭に
#       ほかの .py と同じフル形式の AGPL 告知を置く
#     - コメントの中にあるトークンらしい文字列（xoxb-…，ghp_…，sk-ant-… など）を
#       伏せ字にし，件数を報告する（さいまるの処理は代入の右辺しか見ないため）

import re
import ast
import sys
import datetime

# 名前にこれらが含まれる変数は「秘密情報の可能性が高い」とみなす。
# 該当する場合は、短い値・安全そうな値であっても伏せ字にし、
# 自動処理できない形（f 文字列・連結・関数呼び出し等）なら警告を付ける。
_SENSITIVE_NAME_RE = re.compile(
    r'(SECRET|PASSWORD|PASSWD|PWD|PW|TOKEN|API|KEY|CLIENT_ID|CLIENT_SECRET|'
    r'CREDENTIAL|PRIVATE|SALT|CERT|SIGNATURE|AUTH|SESSION|COOKIE|'
    r'MYSQL|DATABASE|DB_|DSN|HOST|PORT|USER|MAIL|SMTP|DOMAIN|URI|URL|'
    r'REDIRECT|WEBHOOK|ACCOUNT|LICENSE|ADMIN|EMAIL|ALLOWED|WHITELIST|'
    r'ALLOWLIST|OWNER|TENANT)',
    re.IGNORECASE
)

# 単純な文字列代入: NAME = 'value'  /  NAME: 'value'
_ASSIGN_STR_RE = re.compile(
    r"^(?P<indent>\s*)"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<op>\s*[:=]\s*)"
    r"(?P<quote>['\"])(?P<val>.*?)(?P=quote)"
    r"(?P<rest>\s*(#.*)?)$"
)

# 何らかの代入だが右辺が単純文字列でないもの
_ASSIGN_ANY_RE = re.compile(
    r"^(?P<indent>\s*)"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<op>\s*[:=]\s*)"
    r"(?P<value>\S.*)$"
)

# 辞書の中の「'キー': '値'」形式（DATABASES = {'default': {'PASSWORD': '...'}}
# のようなネストした設定を拾うために必要）
_DICT_ITEM_RE = re.compile(
    r"^(?P<indent>\s*)"
    r"(?P<kq>['\"])(?P<key>[^'\"]+)(?P=kq)"
    r"(?P<op>\s*:\s*)"
    r"(?P<vq>['\"])(?P<val>.*?)(?P=vq)"
    r"(?P<rest>\s*,?\s*(#.*)?)$"
)

# 三重引用符での代入開始
_TRIPLE_START_RE = re.compile(
    r"^(?P<indent>\s*)"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<op>\s*[:=]\s*)"
    r"(?P<quote>\"\"\"|''')"
)

# base64 / hex のような、見るからにトークンらしい文字列
_TOKEN_LIKE_RE = re.compile(r'^[A-Za-z0-9+/=_\-]{24,}$')

# 発行元がはっきりしている鍵・トークンの書式。長さが短くても拾う。
_KNOWN_SECRET_RE = re.compile(
    r'^('
    r'AKIA[0-9A-Z]{12,}'            # AWS アクセスキー ID
    r'|ASIA[0-9A-Z]{12,}'           # AWS 一時キー
    r'|(sk|pk|rk)_(live|test)_[A-Za-z0-9]{10,}'   # Stripe
    r'|xox[baprs]-[A-Za-z0-9-]{10,}'              # Slack
    r'|gh[pousr]_[A-Za-z0-9]{20,}'                # GitHub
    r'|github_pat_[A-Za-z0-9_]{20,}'
    r'|AIza[0-9A-Za-z_\-]{20,}'                   # Google API
    r'|SG\.[A-Za-z0-9_\-]{10,}'                   # SendGrid
    r'|eyJ[A-Za-z0-9_\-]{10,}'                    # JWT
    r'|-----BEGIN [A-Z ]*PRIVATE KEY-----'
    r')')

# 匿名化しない無害な値（真偽値・None・空、既知の設定切替キー等）
SAFE_VALUES = {'True', 'False', 'None', '',
               'development', 'production', 'default'}

WARN_TEXT = '⚠ 要確認: 自動匿名化できません。値を目視で確認してください'


# 行のどこにあっても拾いたい「キー: 値」「名前 = 値」の組。
#   {'password': 'x'} のような1行辞書、connect(password='x') のような
#   キーワード引数、obj.secret_key = 'x' のような属性代入を拾う。
_INLINE_KV_RE = re.compile(
    r"(?P<q1>['\"])(?P<key>[A-Za-z_][A-Za-z0-9_.\-]*)(?P=q1)"
    r"(?P<sep>\s*:\s*)"
    r"(?P<q2>['\"])(?P<val>(?:\\.|(?!(?P=q2)).)*)(?P=q2)"
)
_INLINE_KW_RE = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.]*)"
    r"(?P<sep>\s*=\s*)"
    r"(?P<q>['\"])(?P<val>(?:\\.|(?!(?P=q)).)*)(?P=q)"
)


def _placeholder_for(name: str) -> str:
    return '<YOUR_' + re.sub(r'[^A-Za-z0-9_]', '_', name).upper() + '>'


def _mask_inline_sensitive(line: str):
    """
    1行の中に現れる「秘密情報らしいキー／名前に対応する文字列値」を
    すべて伏せ字化する。

    行頭の単純な代入だけを見る規則では拾えない次の形を潰すのが目的:
        SETTINGS = {'password': 'x'}      1行に収まった辞書
        conn = connect(password='x')      キーワード引数
        obj.secret_key = 'x'              属性への代入
        {'AWS_KEY': 'AKIA....'}           トークン然とした値

    Returns:
        (処理後の行, 伏せ字化したキー名のリスト)
    """
    touched = []

    def kv(m):
        key, val = m.group('key'), m.group('val')
        if _is_masked(val):
            return m.group(0)
        if _SENSITIVE_NAME_RE.search(key) or _TOKEN_LIKE_RE.match(val):
            if val in SAFE_VALUES:
                return m.group(0)
            touched.append(key)
            return (m.group('q1') + key + m.group('q1') + m.group('sep')
                    + m.group('q2') + _placeholder_for(key) + m.group('q2'))
        return m.group(0)

    def kw(m):
        name, val = m.group('name'), m.group('val')
        if _is_masked(val):
            return m.group(0)
        if _SENSITIVE_NAME_RE.search(name) or _TOKEN_LIKE_RE.match(val):
            touched.append(name)
            return (name + m.group('sep') + m.group('q')
                    + _placeholder_for(name) + m.group('q'))
        return m.group(0)

    line = _INLINE_KV_RE.sub(kv, line)
    line = _INLINE_KW_RE.sub(kw, line)
    return line, touched


def _bracket_delta(line: str) -> int:
    """行に含まれる開き括弧と閉じ括弧の差（文字列中の括弧は数えない）"""
    depth = 0
    quote = None
    escaped = False
    for ch in line:
        if quote:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ('\'', '"'):
            quote = ch
        elif ch == '#':
            break
        elif ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth -= 1
    return depth


_BLOCK_VALUE_RE = re.compile(
    r"(?P<q>['\"])(?P<val>(?:\\.|(?!(?P=q)).)*)(?P=q)(?P<after>\s*:?)"
)


def _mask_block_values(line: str, placeholder: str) -> str:
    """
    「秘密らしい名前」の複数行定義の内側にある文字列値を伏せ字化する。

    辞書のキー（直後に ':' が続くもの）は構造を保つために残し、
    値だけを置き換える。
    """
    def rep(m):
        if m.group('after').strip().endswith(':'):
            return m.group(0)          # 辞書のキーは残す
        val = m.group('val')
        if _is_masked(val) or val in SAFE_VALUES or len(val) < 3:
            return m.group(0)
        return m.group('q') + placeholder + m.group('q') + m.group('after')
    return _BLOCK_VALUE_RE.sub(rep, line)


def _warn_line(indent: str, name: str) -> str:
    """
    警告コメントを「その行の上」に置くための1行を返す。

    行末に追記すると、バックスラッシュによる行継続
    （SECRET_KEY = 'a' \\ + 'b'）のときに継続が壊れて SyntaxError に
    なるため、必ず独立した行として出す。
    """
    return '%s# %s (%s)' % (indent, WARN_TEXT, name)


def _anonymize_config_source(source: str):
    """
    config.py のソースを受け取り、秘密情報になりうる値を
    プレースホルダー <YOUR_属性名> に置換して返す。

    Returns:
        (匿名化後のソース, 警告メッセージのリスト)

    方針:
      - 「属性 = '値'」「属性: '値'」の右辺の文字列値を置換する。
      - 属性名が秘密情報らしい（_SENSITIVE_NAME_RE に一致する）場合は、
        短い値・SAFE_VALUES に含まれる値であっても置換する。
        値の長さから中身を推測されるのを防ぐため、置換後は必ず
        <YOUR_属性名> の形にする。
      - 右辺が単純な文字列でない場合（f 文字列・文字列連結・関数呼び出し・
        リスト・辞書・三重引用符）は自動置換できない。
        属性名が秘密情報らしいときは行末に警告マーカーを付け、
        ファイル冒頭にも警告一覧を出す。レビュー時の見落としを減らす。
      - 左辺の属性名・辞書のキー・import 文などは対象外。

    例:
      SECRET_KEY = 'abc123'          -> SECRET_KEY = '<YOUR_SECRET_KEY>'
      DEBUG = True                   -> 変更しない
      URI = f"https://{D}/callback"  -> 変更せず ⚠ マーカーを付与
    """
    warnings = []
    out_lines = []

    # 「秘密らしい名前 = { …複数行… }」の途中にいるかどうか。
    # 中に入っている間は、キー名が無害でも値を伏せ字化する。
    block_name = None
    block_depth = 0

    lines = source.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]

        # --- 0a. 秘密ブロックの内側 ---------------------------------------
        if block_name is not None:
            masked = _mask_block_values(line, _placeholder_for(block_name))
            out_lines.append(masked)
            block_depth += _bracket_delta(line)
            if block_depth <= 0:
                block_name = None
                block_depth = 0
            i += 1
            continue

        # --- 0. 行内の「秘密らしいキー／名前 = 値」を先に潰す --------------
        # 行頭の単純な代入しか見ない規則では、1行辞書・キーワード引数・
        # 属性代入がすり抜けるため、まずここで拾う。
        if not _TRIPLE_START_RE.match(line):
            line, touched = _mask_inline_sensitive(line)
            for key in touched:
                warnings.append('%s の値を伏せ字化しました' % key)

        # --- 1. 三重引用符での代入（複数行文字列） -------------------------
        m3 = _TRIPLE_START_RE.match(line)
        if m3:
            name = m3.group('name')
            quote = m3.group('quote')
            after = line[m3.end():]
            closed = quote in after     # 1行で閉じているか
            block = [line]
            j = i
            while not closed and j + 1 < len(lines):
                j += 1
                block.append(lines[j])
                if quote in lines[j]:
                    closed = True
            if _SENSITIVE_NAME_RE.search(name):
                # 秘密情報らしい複数行文字列はブロックごと置換する
                out_lines.append(
                    '%s%s%s%s<YOUR_%s>%s' % (
                        m3.group('indent'), name, m3.group('op'),
                        quote, name.upper(), quote)
                )
                warnings.append(
                    '%s: 複数行文字列だったため、内容をまるごと '
                    '<YOUR_%s> に置換しました' % (name, name.upper())
                )
            else:
                out_lines.extend(block)
            i = j + 1
            continue

        # --- 1.5 辞書の 'キー': '値' ---------------------------------------
        # DATABASES = {'default': {'PASSWORD': 'xxx'}} のように、
        # 左辺が識別子でない形の秘密情報を拾う。
        md = _DICT_ITEM_RE.match(line)
        if md:
            key = md.group('key')
            val = md.group('val')
            sensitive = bool(_SENSITIVE_NAME_RE.search(key))
            token_like = bool(_TOKEN_LIKE_RE.match(val))
            if (sensitive or token_like) and val not in SAFE_VALUES:
                ph = '<YOUR_' + re.sub(r'[^A-Za-z0-9_]', '_',
                                       key).upper() + '>'
                out_lines.append(
                    md.group('indent') + md.group('kq') + key + md.group('kq')
                    + md.group('op') + md.group('vq') + ph + md.group('vq')
                    + md.group('rest')
                )
                warnings.append(
                    "辞書のキー '%s' の値を伏せ字化しました" % key)
            else:
                out_lines.append(line)
            i += 1
            continue

        # --- 2. 単純な文字列代入 -------------------------------------------
        m = _ASSIGN_STR_RE.match(line)
        if m:
            name = m.group('name')
            val = m.group('val')
            sensitive = bool(_SENSITIVE_NAME_RE.search(name))
            # 秘密情報らしい名前は、短くても SAFE_VALUES でも必ず伏せる
            if not sensitive and (val in SAFE_VALUES or len(val) < 3):
                out_lines.append(line)
            else:
                placeholder = '<YOUR_' + name.upper() + '>'
                out_lines.append(
                    m.group('indent') + name + m.group('op')
                    + m.group('quote') + placeholder + m.group('quote')
                    + m.group('rest')
                )
            i += 1
            continue

        # --- 3. その他の代入（自動置換できない形） -------------------------
        ma = _ASSIGN_ANY_RE.match(line)
        if ma:
            name = ma.group('name')
            value = ma.group('value')
            bare = value.split('#', 1)[0].strip()
            # 真偽値・数値・None は無害なので触らない
            harmless = (
                bare in ('True', 'False', 'None')
                or re.fullmatch(r'-?\d+(\.\d+)?', bare) is not None
            )
            has_literal = ("'" in bare) or ('"' in bare)
            if _SENSITIVE_NAME_RE.search(name) and not harmless:
                # 警告は必ず「上の行」に置く。行末に足すとバックスラッシュ
                # による行継続が壊れて SyntaxError になる。
                out_lines.append(_warn_line(ma.group('indent'), name))
                # 複数行の { } / [ ] が開いたままなら、閉じるまでの値を
                # まとめて伏せ字化する（キー名が無害でも中身は秘密のため）
                delta = _bracket_delta(line)
                if delta > 0:
                    out_lines.append(line)
                    block_name = name
                    block_depth = delta
                    warnings.append(
                        '%s: 複数行の定義のため、閉じ括弧までの文字列値を'
                        'まとめて伏せ字化しました' % name)
                    i += 1
                    continue
                continues = bare.endswith('\\')
                if has_literal and not continues:
                    # リスト・辞書・連結の中の文字列リテラルを個別に伏せる
                    placeholder = '<YOUR_' + name.upper() + '>'
                    replaced = re.sub(
                        r"(['\"])(?:(?!\1).){3,}?\1",
                        lambda mm: mm.group(1) + placeholder + mm.group(1),
                        bare
                    )
                    out_lines.append(
                        ma.group('indent') + name + ma.group('op') + replaced
                    )
                    warnings.append(
                        '%s: 単純な代入ではないため機械的に伏せ字化しました。'
                        '構文が壊れていないか確認してください' % name
                    )
                else:
                    out_lines.append(line)
                    warnings.append(
                        '%s: 値が f 文字列・変数・関数呼び出し・行継続の'
                        'ため自動匿名化できません。手で確認してください'
                        % name
                    )
                i += 1
                continue

        out_lines.append(line)
        i += 1

    return '\n'.join(out_lines), warnings


def _is_masked(value: str) -> bool:
    """すでにプレースホルダー化されている値か"""
    v = value.strip()
    return v.startswith('<') and v.endswith('>')


# モジュールパス・URL・ファイルパスらしい文字列（秘密ではないことが多い）
_PATHY_RE = re.compile(
    r'^(?:[a-z0-9_]+(?:\.[a-z0-9_]+)+'      # django.db.backends.mysql
    r'|/[\w./\-]*'                          # /var/www/...
    r'|[a-z]+://[\w./\-]*'                   # https://example.com/...
    r'|%\([a-z_]+\)s'                        # %(name)s
    r')$'
)


def _looks_like_path(value: str) -> bool:
    """モジュールパス・URL・ファイルパスのような、秘密でない可能性が
    高い値か（辞書の中の誤検知を減らすための判定）"""
    return bool(_PATHY_RE.match(value.strip()))


def _iter_str_constants(node):
    """式ノード配下の文字列リテラルを (値, 行番号) で列挙する"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            yield sub.value, getattr(sub, 'lineno', 0)


def _iter_value_constants(node):
    """
    式ノード配下の文字列リテラルを (値, 行番号) で列挙する。
    ただし辞書の「キー」は列挙しない。

    DATABASES = {'default': {'ENGINE': '...'}} のようなとき、
    キー名（'default' / 'ENGINE'）まで秘密候補として拾うと
    誤検知だらけになるため。
    """
    if isinstance(node, ast.Dict):
        for v in node.values:
            for item in _iter_value_constants(v):
                yield item
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for e in node.elts:
            for item in _iter_value_constants(e):
                yield item
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value, getattr(node, 'lineno', 0)
    else:
        for item in _iter_str_constants(node):
            yield item


def _target_names(node):
    """代入先ノードから名前らしきものを取り出す"""
    names = []
    for t in (node if isinstance(node, (list, tuple)) else [node]):
        if isinstance(t, ast.Name):
            names.append(t.id)
        elif isinstance(t, ast.Attribute):
            names.append(t.attr)
        elif isinstance(t, (ast.Tuple, ast.List)):
            names.extend(_target_names(list(t.elts)))
    return names


def scan_source_for_secrets(source: str) -> list:
    """
    Python ソースを AST で走査し、秘密情報らしき文字列リテラルを
    [(行番号, 手がかりの名前, 値の断片), ...] で返す。

    行ベースの正規表現では拾えない次の形を検出するのが目的:
        DATABASES = {'default': {'PASSWORD': 'hunter2'}}
        conn = connect(password='hunter2')
        obj.secret_key = 'hunter2'
    さらに、名前に手がかりが無くても base64/hex 風の長い文字列は
    トークンとみなして拾う。

    構文エラーで parse できない場合は行ベースの検査にフォールバックする。
    """
    hits = []
    seen = set()

    def add(lineno, name, value):
        if not isinstance(value, str):
            return
        if _is_masked(value) or value in SAFE_VALUES or len(value) < 3:
            return
        key = (lineno, name)
        if key in seen:
            return
        seen.add(key)
        # 値そのものは返さない（画面・スクリーンショット・ログに
        # 秘密の断片が残らないようにする）。長さだけを手がかりにする。
        hits.append((lineno, name, '%d文字の文字列' % len(value)))

    try:
        tree = ast.parse(source)
    except SyntaxError:
        # parse できないときは従来どおり行単位で最低限の検査をする
        for no, line in enumerate(source.split('\n'), 1):
            m = _ASSIGN_STR_RE.match(line)
            if m and _SENSITIVE_NAME_RE.search(m.group('name')):
                add(no, m.group('name'), m.group('val'))
        return hits

    for node in ast.walk(tree):
        # 1. 代入（NAME = ... / obj.attr = ... / NAME: t = ...）
        value = None
        names = []
        if isinstance(node, ast.Assign):
            names = _target_names(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            names = _target_names(node.target)
            value = node.value
        if value is not None:
            for nm in names:
                if not _SENSITIVE_NAME_RE.search(nm):
                    continue
                if isinstance(value, ast.Dict):
                    # API_KEYS = {'sendgrid': 'SG.xxx'} のように
                    # 「名前は秘密だがキーは無害」という形を拾う。
                    # ただし辞書のキー自体と、モジュールパス・URL・
                    # ファイルパス風の値（'django.db.backends.mysql' など）
                    # は除く。そうしないと DATABASES 定義のたびに警告が出て、
                    # 「とりあえず OK」を押す癖がついてしまう。
                    for s, ln in _iter_value_constants(value):
                        if len(s) >= 6 and not _looks_like_path(s):
                            add(ln, nm, s)
                else:
                    for s, ln in _iter_str_constants(value):
                        add(ln, nm, s)

        # 2. 辞書の 'キー': '値'（ネストも ast.walk で全部見える）
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant)
                        and isinstance(k.value, str)
                        and _SENSITIVE_NAME_RE.search(k.value)):
                    for s, ln in _iter_str_constants(v):
                        add(ln, k.value, s)

        # 3. キーワード引数（connect(password='...') など）
        if isinstance(node, ast.keyword) and node.arg:
            if _SENSITIVE_NAME_RE.search(node.arg):
                for s, ln in _iter_str_constants(node.value):
                    add(ln, node.arg, s)

        # 4. 名前に手がかりが無くてもトークン然とした文字列は拾う
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _TOKEN_LIKE_RE.match(node.value):
                add(getattr(node, 'lineno', 0), '(トークン風の文字列)',
                    node.value)

    hits.sort(key=lambda h: h[0])
    return hits




# ---------------------------------------------------------------------------
# 追加: ライセンス告知の付け替えと，コメント内のトークンの伏せ字化

FULL_NOTICE_PY = """\
# SPDX-FileCopyrightText: 2024-2026 Toyoaki Nishida
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This file is part of FUJIN-P.
# Copyright (C) 2024-2026 Toyoaki Nishida
#
# FUJIN-P is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# FUJIN-P is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with FUJIN-P.  If not, see <https://www.gnu.org/licenses/>.
#
# Source: https://github.com/u-fukuchiyama/fujin-p"""

_LICENSE_LINE_RE = re.compile(
    r'^#\s*(SPDX-|This file is part of FUJIN-P|Copyright \(C\)|Source: https://github\.com/'
    r'|FUJIN-P is (free|distributed)|it under the terms|the Free Software Foundation'
    r'|\(at your option\)|but WITHOUT ANY WARRANTY|MERCHANTABILITY|GNU Affero General'
    r'|You should have received|along with FUJIN-P)')


def strip_license_header(source):
    """先頭のコメント塊がライセンス表示なら取り除く（空のコメント行・空行も含めて）"""
    lines = source.split('\n')
    i = 0
    found = False
    while i < len(lines):
        t = lines[i].strip()
        if t == '' or t == '#':
            i += 1
            continue
        if _LICENSE_LINE_RE.match(t):
            found = True
            i += 1
            continue
        break
    return ('\n'.join(lines[i:]) if found else source), found


# コメントの中で伏せる文字列．発行元のはっきりした書式と，長いトークン風の語．
_COMMENT_SECRET_RE = re.compile(
    r'(xox[baprse]-[A-Za-z0-9-]{10,}'
    r'|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}'
    r'|sk-ant-[A-Za-z0-9_\-]{10,}|sk-[A-Za-z0-9]{20,}'
    r'|AIza[0-9A-Za-z_\-]{20,}|AKIA[0-9A-Z]{12,}|SG\.[A-Za-z0-9_\-.]{10,}'
    r'|eyJ[A-Za-z0-9_\-]{10,}|GOCSPX-[A-Za-z0-9_\-]{10,}'
    r'|1//[A-Za-z0-9_\-]{20,}'
    r'|(?<![A-Za-z0-9/])(?=[A-Za-z0-9+_\-]*[0-9])(?=[A-Za-z0-9+_\-]*[A-Za-z])[A-Za-z0-9+_\-]{32,}(?![A-Za-z0-9/]))')


def mask_comment_secrets(text):
    """# 以降（文字列の外）にあるトークンらしい文字列を伏せ字にする．
    返り値: (処理後の本文, [(行番号, 件数), ...])"""
    out = []
    hits = []
    for no, line in enumerate(text.split('\n'), 1):
        pos = _comment_start(line)
        if pos is None:
            out.append(line)
            continue
        head, comment = line[:pos], line[pos:]
        new, n = _COMMENT_SECRET_RE.subn('<伏せ字>', comment)
        if n:
            hits.append((no, n))
        out.append(head + new)
    return '\n'.join(out), hits


def _comment_start(line):
    """文字列リテラルの外にある # の位置（無ければ None）"""
    quote = None
    escaped = False
    for i, ch in enumerate(line):
        if quote:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ('\'', '"'):
            quote = ch
        elif ch == '#':
            return i
    return None


def main():
    if len(sys.argv) != 2:
        sys.exit('使い方: python3 anonymize_config.py <config.py のパス>')
    with open(sys.argv[1], encoding='utf-8') as f:
        source = f.read()
    stripped, had_license = strip_license_header(source)
    _offset = source.count('\n') - stripped.count('\n')
    source = stripped
    processed, warnings = _anonymize_config_source(source)
    processed, comment_hits = mask_comment_secrets(processed)
    for no, n in comment_hits:
        warnings.append('コメント中のトークンらしい文字列 %d 個を伏せ字化しました（元の config.py の %d 行目付近）'
                        % (n, no + _offset))
    jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    lines = FULL_NOTICE_PY.split('\n') + [
        '"""',
        'FUJIN-P Configuration Template',
        '',
        'このファイルをコピーして config.py に名前を変え、',
        '各プレースホルダー <YOUR_...> を実際の値に書き換えてください。',
        '',
        'Generated by anonymize_config.py (さいまるの匿名化処理): %s'
        % jst.strftime('%Y-%m-%d %H:%M JST'),
        '"""',
        '',
        '# !! このファイルは config.py から自動生成されたテンプレートです !!',
        '# !! <YOUR_...> を実際の値に置き換えて config.py として保存 !!',
        '',
    ]
    if warnings:
        lines += [
            '# ---------------------------------------------------------',
            '# ⚠ 自動匿名化できなかった／機械的に処理した箇所があります。',
            '#   下記を必ず目視で確認してください。',
            '# ---------------------------------------------------------',
        ]
        lines += ['#   - ' + w for w in warnings]
        lines.append('')
    text = '\n'.join(lines + processed.split('\n'))
    sys.stdout.write(text)
    if not text.endswith('\n'):
        sys.stdout.write('\n')

    try:
        compile(text, 'config_template.py', 'exec')
        sys.stderr.write('構文: OK\n')
    except SyntaxError as e:
        sys.stderr.write('構文: エラー（%s 行目）．目視で直してください\n' % e.lineno)
    if comment_hits:
        sys.stderr.write('コメント中のトークンらしい文字列を伏せ字化: %d 行．'
                         '元の config.py からも消すことを勧めます\n' % len(comment_hits))
    if not had_license:
        sys.stderr.write('元の config.py にライセンス表示が見つからなかったので，先頭に告知を付けました\n')
    hits = scan_source_for_secrets(text)
    _txt, left = mask_comment_secrets(text)
    if left:
        sys.stderr.write('コメント中にトークンらしい文字列が残っています: %s 行目\n'
                         % ', '.join(str(n) for n, _c in left))
    if hits:
        sys.stderr.write('伏せ字漏れの疑い %d 件（値は表示しません）:\n' % len(hits))
        for ln, name, desc in hits:
            sys.stderr.write('  %d 行目  %s  （%s）\n' % (ln, name, desc))
    else:
        sys.stderr.write('伏せ字漏れの疑い: なし\n')


if __name__ == '__main__':
    main()
