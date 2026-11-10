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
# Source: https://github.com/u-fukuchiyama/fujin-p


# data_saver/routes_bulk.py
# Data Saver: Claude 向け一括調査の依頼文と，調査結果の照合・反映
#
#   1. /bulk/prompt  … 全アプリを対象にした依頼文を作る．現在の表（ID付き）と，アプシャに
#                      登録されているアプリの一覧を埋め込み，結果を JSON で返すよう頼む
#
#   対象のアプリはアプシャの台帳（app_share_registry）に登録されているものに限る（2026-11-10）．
#   登録を外したアプリのディレクトリが残っていても対象にしない．表にそのアプリの行が
#   残っていれば，照合で削除の候補として示す（既定では選ばない）．
#   2. /bulk/preview … 貼り付けられた結果（JSON またはタブ区切りテキスト）を表と照合し，
#                      追加・訂正・削除・変更なしの計画を返す（表は変えない）
#   3. /bulk/apply   … 同じテキストで計画を作り直し，選ばれた変更だけを1トランザクションで反映
#
#   照合の規則:
#     - id があり表にある        → その行と照合（同じなら変更なし，違えば訂正）
#     - id が無い／表に無い      → アプリ名＋ディレクトリが一致する行があれば変更なし
#     - ID 列の無いテキストに限り，同じアプリで照合できなかった新項目と表の行が
#       ちょうど1つずつなら訂正（ID・保存記録を引き継ぐ）
#     - それ以外の新項目         → 追加
#     - 結果に出てきたアプリの行で，照合されなかったもの → 削除
#       （そのアプリにエラー行があるときは削除を出さない）
#     - 結果に出てこなかったアプリの行には触れない（照合対象外として報告）
#     - 登録されていないアプリの行 → 削除の候補（unregistered．既定では選ばない）

import os
import re
import json
import logging
import mysql.connector
from flask import request, jsonify
from decorators import login_required
from db import DatabaseConfig
from . import data_saver_bp
from .routes import (TABLE, base_dir, work_root, check_dir_path, check_app_name,
                     _parse_log, _close)

_NONE_WORDS = ('データ置き場なし', 'なし', 'none', 'null', '-', '')
_FENCE_RE = re.compile(r'```(?:json|tsv|text)?\s*\n(.*?)```', re.S)


# ============================================================
# サーバ上のアプリ一覧と現在の表
# ============================================================

REGISTRY_TABLE = 'app_share_registry'


def list_app_dirs():
    """アプシャに登録されているアプリのうち，基点直下に __init__.py を持つディレクトリが
    あるものの名前一覧（2026-11-10）．
    kind（app／kernel）や enabled は問わない．止めているアプリにもデータは残るため．
    _platform のように _ で始まる行と，Data Saver 自身は除く．"""
    return app_inventory()['apps']


def app_inventory():
    """{'apps': 対象のアプリ,
        'no_dir': 登録はあるが基点直下にディレクトリが無いもの,
        'unregistered': ディレクトリはあるが登録の無いもの（対象外）}"""
    base = base_dir()
    work = work_root()
    own = os.path.basename(os.path.dirname(os.path.abspath(__file__)))   # data_saver 自身

    def is_app_dir(name):
        path = os.path.join(base, name)
        return (os.path.isdir(path) and os.path.realpath(path) != work
                and os.path.isfile(os.path.join(path, '__init__.py')))

    registered = _registered_names()
    apps, no_dir = [], []
    for name in sorted(registered):
        if name.startswith(('.', '_')) or name == own:
            continue
        (apps if is_app_dir(name) else no_dir).append(name)
    unregistered = []
    try:
        for name in sorted(os.listdir(base)):
            if name.startswith(('.', '_')) or name == own or name in registered:
                continue
            if is_app_dir(name):
                unregistered.append(name)
    except Exception as e:
        logging.error("data_saver app_inventory listdir error: %s", e)
    return {'apps': apps, 'no_dir': no_dir, 'unregistered': unregistered}


def _registered_names():
    """アプシャの台帳に登録されているアプリ名の集合．読めなければ例外を上げる
    （ディレクトリの走査に戻すと，登録を外したアプリが対象に紛れ込むため）．"""
    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT app_name FROM `%s`" % REGISTRY_TABLE)
        return {r['app_name'] for r in cursor.fetchall() if r.get('app_name')}
    except Exception as e:
        logging.error("data_saver registry read error: %s", e)
        raise RuntimeError('アプシャの台帳（%s）を読めませんでした: %s' % (REGISTRY_TABLE, e))
    finally:
        _close(conn, cursor)


def load_rows():
    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, app_name, dir_path, save_log FROM `%s` ORDER BY id" % TABLE)
        rows = cursor.fetchall()
        for r in rows:
            r['save_log'] = _parse_log(r['save_log'])
        return rows
    finally:
        _close(conn, cursor)


# ============================================================
# 依頼文
# ============================================================

BULK_PROMPT = """FUJIN-P の全アプリを解析して，各アプリのデータ置き場となっているディレクトリを調べ，下の「現在の表」を正しい内容に直した結果を JSON で出力してください．

## データ置き場とは
アプリの実行中に利用者の操作やバッチ処理でファイルが書き込まれ，バックアップしておかないと失われるディレクトリのことです．アップロードされた画像・PDF・動画，アプリが生成して保存するファイル，再生成できないキャッシュや作業領域などが該当します．
プログラムと一緒に配布される静的ファイル（CSS・JS・アイコンなど）しか置かれないディレクトリは含めないでください．
一時ファイルだけが置かれ，消えても困らないディレクトリも含めないでください．

## 手がかりになるコード
- os.path.join / os.makedirs / open(..., 'w' または 'wb') / shutil.copy・move / FileStorage.save()
- send_from_directory / send_file で配信しているディレクトリ
- Config の定数（名前が _DIR・_FOLDER・_PATH・_ROOT で終わるもの）
- current_app.static_folder や __file__ を基点に組み立てたパス
- 他アプリ（いまくら・ふぁいこんなど）や Google Drive に保存していてサーバに置かないものは対象外

## 対象のアプリ（アプシャに登録されているもの，{app_count} 個）
{app_list}

{base} の直下にこの一覧に無いディレクトリがあっても，登録を外したアプリの残りなので解析しないでください．

## 現在の表（data_saver_dirs）
```json
{table_json}
```

## 出力形式
次の形の JSON を1つ，コードブロックで出力してください．説明文は JSON の外に書いてかまいません．

```json
{{"items": [
  {{"id": 3, "app_name": "kataribe", "dir_path": "{base}/kataribe/static/uploads",
    "evidence": "routes.py:upload_image()", "note": "スライドに貼る画像"}},
  {{"id": null, "app_name": "my_md_notes", "dir_path": "{base}/my_md_notes/static/notes_media",
    "evidence": "routes.py:upload_media(), Config.MD_NOTES_MEDIA_DIR", "note": "ノートに挿入した画像とPDF"}},
  {{"id": null, "app_name": "stats", "dir_path": null, "evidence": "", "note": "データ置き場なし"}}
]}}
```

## 規則
1. 上の「対象のアプリ」のすべてについて，少なくとも1項目を出力してください．データ置き場が無いアプリは dir_path を null にした1項目を出します．
2. 現在の表の行と同じディレクトリを指す項目（パスの誤りを直した場合も含む）には，その行の id を付けてください．新しく見つけたディレクトリは id を null にします．
3. 現在の表にあって不要と判断した行は，出力に含めないでください．含めなかった行は削除されます．
4. dir_path は絶対パスで書いてください．アプリのディレクトリや {base} の外にあってもかまいません（例：ホーム直下の static/mdimgs）．Config の値で場所が変わる場合は既定値で書き，evidence に定数名を添えてください．
5. 複数のアプリが同じディレクトリに書き込む場合は，アプリごとに1項目ずつ出してください．
6. evidence には根拠（ファイル名:関数名），note には中身の説明を短く書いてください．
7. 解析できなかったアプリは，推測で埋めずに出力から外し，JSON の外にその旨を書いてください．出力から外したアプリの行は変更されません．
"""


@data_saver_bp.route('/bulk/prompt', methods=['GET'])
@login_required
def bulk_prompt():
    try:
        rows = load_rows()
    except Exception as e:
        logging.error("data_saver bulk_prompt error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    try:
        inv = app_inventory()
    except RuntimeError as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    apps = inv['apps']
    base = base_dir()
    table = [{'id': r['id'], 'app_name': r['app_name'], 'dir_path': r['dir_path']} for r in rows]
    text = BULK_PROMPT.format(
        base=base,
        app_count=len(apps),
        app_list='\n'.join(apps) if apps else '（見つかりません）',
        table_json=json.dumps(table, ensure_ascii=False, indent=1) if table else '[]',
    )
    return jsonify({'success': True, 'text': text, 'app_count': len(apps), 'row_count': len(rows),
                    'no_dir': inv['no_dir'], 'unregistered': inv['unregistered']})


# ============================================================
# 貼り付けの解析
#   JSON:  {"items":[...]} / [...] / {"apps":[{"app_name":..,"dirs":[...]}]}
#   テキスト: 1行1項目，タブ区切り．
#     アプリ名<TAB>絶対パス[<TAB>根拠<TAB>説明]
#     ID<TAB>アプリ名<TAB>絶対パス[...]   （先頭が数字または空欄なら ID 列とみなす）
#     絶対パスが空・「データ置き場なし」なら，そのアプリはデータ置き場なし
# ============================================================

def _norm_id(v):
    if v is None or v == '':
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return 'bad'


def _is_none_path(v):
    return v is None or str(v).strip().lower() in _NONE_WORDS or 'なし' in str(v)


def _item(src, no, id_, app, path, evidence='', note='', id_known=True):
    return {'no': no, 'src': src, 'id': _norm_id(id_), 'id_known': id_known,
            'app_name': (app or '').strip(),
            'dir_path': None if _is_none_path(path) else str(path).strip(),
            'evidence': str(evidence or '').strip(), 'note': str(note or '').strip()}


def parse_input(text):
    text = (text or '').strip()
    if not text:
        raise ValueError('貼り付けが空です')
    m = _FENCE_RE.search(text)
    body = m.group(1).strip() if m else text

    # JSON として読めるか（コードブロックが無い場合は，最初の { か [ から読む）
    data = None
    for cand in (body, body[min([i for i in (body.find('{'), body.find('[')) if i >= 0] or [0]):]):
        try:
            data = json.loads(cand)
            break
        except Exception:
            continue

    items = []
    if data is not None:
        if isinstance(data, dict):
            if isinstance(data.get('items'), list):
                data = data['items']
            elif isinstance(data.get('apps'), list):
                flat = []
                for a in data['apps']:
                    dirs = a.get('dirs') or []
                    if not dirs:
                        flat.append({'app_name': a.get('app_name'), 'dir_path': None,
                                     'note': a.get('note', '')})
                    for d in dirs:
                        d = dict(d) if isinstance(d, dict) else {'dir_path': d}
                        d.setdefault('app_name', a.get('app_name'))
                        flat.append(d)
                data = flat
            else:
                raise ValueError('JSON に items がありません')
        if not isinstance(data, list):
            raise ValueError('JSON の形が想定と違います')
        for i, d in enumerate(data, 1):
            if not isinstance(d, dict):
                raise ValueError('JSON の %d 番目の項目がオブジェクトではありません' % i)
            items.append(_item(json.dumps(d, ensure_ascii=False), i, d.get('id'),
                               d.get('app_name') or d.get('app'),
                               d.get('dir_path') if 'dir_path' in d else d.get('path'),
                               d.get('evidence', ''), d.get('note', ''),
                               id_known='id' in d))
        return items, 'json'

    for no, line in enumerate(body.splitlines(), 1):
        raw = line.rstrip()
        if not raw.strip() or raw.lstrip().startswith('#'):
            continue
        f = raw.split('\t') if '\t' in raw else re.split(r'\s{2,}|\s+(?=/)', raw.strip())
        f = [x.strip() for x in f]
        if f[0].lower() in ('id', 'アプリ名', 'app_name', 'app'):
            continue                      # 見出し行
        if f[0] == '' or f[0].isdigit():
            f = f + [''] * (5 - len(f))
            items.append(_item(raw, no, f[0], f[1], f[2], f[3], f[4]))
        else:
            f = f + [''] * (4 - len(f))
            items.append(_item(raw, no, None, f[0], f[1], f[2], f[3], id_known=False))
    if not items:
        raise ValueError('項目が見つかりません')
    return items, 'text'


# ============================================================
# 照合
# ============================================================

def build_plan(text):
    items, fmt = parse_input(text)
    rows = load_rows()
    server_apps = list_app_dirs()
    registered = set(server_apps)
    by_id = {r['id']: r for r in rows}
    actions = []
    mentioned = set()
    error_apps = set()
    matched = set()
    pending = []       # 照合待ちの新項目

    def norm(p):
        return os.path.normpath(p) if p else p

    # 検査
    for it in items:
        try:
            it['app_name'] = check_app_name(it['app_name'])
        except ValueError as e:
            actions.append({'action': 'error', 'item': it, 'error': str(e)})
            continue
        mentioned.add(it['app_name'])
        if it['app_name'] not in registered:
            actions.append({'action': 'error', 'item': it,
                            'error': 'アプシャに登録されていないアプリです（取り込みません）'})
            continue
        if it['id'] == 'bad':
            actions.append({'action': 'error', 'item': it, 'error': 'id が数値ではありません'})
            error_apps.add(it['app_name'])
            continue
        if it['dir_path'] is None:
            actions.append({'action': 'none', 'item': it})
            continue
        try:
            it['dir_path'], _real = check_dir_path(it['dir_path'])
        except ValueError as e:
            actions.append({'action': 'error', 'item': it, 'error': str(e)})
            error_apps.add(it['app_name'])
            continue
        pending.append(it)

    # 1. id で照合
    rest = []
    for it in pending:
        rid = it['id']
        if rid is not None and rid in by_id:
            if rid in matched:
                actions.append({'action': 'error', 'item': it,
                                'error': 'id %d が重複しています' % rid})
                error_apps.add(it['app_name'])
                continue
            matched.add(rid)
            r = by_id[rid]
            same = (r['app_name'] == it['app_name'] and norm(r['dir_path']) == norm(it['dir_path']))
            actions.append({'action': 'keep' if same else 'update', 'item': it,
                            'row': _row_view(r), 'key': None if same else 'upd:%d' % rid})
        else:
            if rid is not None:
                it['note_id'] = 'id %d は表にありません' % rid
            rest.append(it)

    # 2. アプリ名＋ディレクトリの一致で照合（重複は1つにまとめる）
    rest2 = []
    seen = set()
    for it in rest:
        k = (it['app_name'], norm(it['dir_path']))
        if k in seen:
            actions.append({'action': 'dup', 'item': it})
            continue
        seen.add(k)
        hit = next((r for r in rows if r['id'] not in matched
                    and (r['app_name'], norm(r['dir_path'])) == k), None)
        if hit:
            matched.add(hit['id'])
            actions.append({'action': 'keep', 'item': it, 'row': _row_view(hit), 'key': None})
        else:
            rest2.append(it)

    # 3. ID の情報を持たない項目（ID 列の無いテキスト）に限り，同じアプリで1対1に
    #    残ったものを訂正とみなす．それ以外は追加
    #    （JSON で id: null と書かれた項目は，依頼文の規則どおり新規として扱う）
    by_app = {}
    for it in rest2:
        by_app.setdefault(it['app_name'], []).append(it)
    for app, its in by_app.items():
        left = [r for r in rows if r['app_name'] == app and r['id'] not in matched]
        if (len(its) == 1 and len(left) == 1 and not its[0]['id_known']
                and app not in error_apps):
            r = left[0]
            matched.add(r['id'])
            actions.append({'action': 'update', 'item': its[0], 'row': _row_view(r),
                            'key': 'upd:%d' % r['id'], 'paired': True})
        else:
            for it in its:
                actions.append({'action': 'add', 'item': it, 'key': 'add:%d' % it['no']})

    # 4. 削除と照合対象外
    #    登録されていないアプリの行は，結果に出ていなくても削除の候補にする（既定では選ばない）
    untouched = []
    for r in rows:
        if r['id'] in matched:
            continue
        if r['app_name'] not in registered:
            actions.append({'action': 'delete', 'row': _row_view(r), 'key': 'del:%d' % r['id'],
                            'unregistered': True})
        elif r['app_name'] in mentioned:
            if r['app_name'] in error_apps:
                actions.append({'action': 'hold', 'row': _row_view(r)})
            else:
                actions.append({'action': 'delete', 'row': _row_view(r), 'key': 'del:%d' % r['id']})
        else:
            untouched.append(_row_view(r))

    order = {'error': 0, 'delete': 1, 'update': 2, 'add': 3, 'hold': 4, 'dup': 5, 'none': 6, 'keep': 7}
    actions.sort(key=lambda a: ((a.get('item') or a.get('row'))['app_name'], order[a['action']]))
    counts = {}
    for a in actions:
        counts[a['action']] = counts.get(a['action'], 0) + 1
    return {
        'format': fmt,
        'actions': actions,
        'counts': counts,
        'untouched': untouched,
        'unreported_apps': [a for a in server_apps if a not in mentioned],
        'unknown_apps': sorted(a for a in mentioned if a not in server_apps),
    }


def _row_view(r):
    return {'id': r['id'], 'app_name': r['app_name'], 'dir_path': r['dir_path'],
            'save_count': len(r['save_log'])}


@data_saver_bp.route('/bulk/preview', methods=['POST'])
@login_required
def bulk_preview():
    try:
        plan = build_plan((request.json or {}).get('text'))
        return jsonify({'success': True, 'plan': plan})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logging.error("data_saver bulk_preview error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@data_saver_bp.route('/bulk/apply', methods=['POST'])
@login_required
def bulk_apply():
    data = request.json or {}
    selected = set(data.get('keys') or [])
    if not selected:
        return jsonify({'success': False, 'error': '反映する変更が選ばれていません'}), 400
    try:
        plan = build_plan(data.get('text'))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    todo = [a for a in plan['actions'] if a.get('key') in selected]
    missing = selected - {a['key'] for a in todo}
    if missing:
        return jsonify({'success': False,
                        'error': '照合の後で表が変わったため，選んだ変更の一部が見つかりません．もう一度照合してください'}), 409

    conn = None
    cursor = None
    done = {'add': 0, 'update': 0, 'delete': 0}
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        for a in todo:
            if a['action'] == 'delete':
                cursor.execute("DELETE FROM `%s` WHERE id = %%s" % TABLE, (a['row']['id'],))
            elif a['action'] == 'update':
                cursor.execute("UPDATE `%s` SET app_name = %%s, dir_path = %%s WHERE id = %%s" % TABLE,
                               (a['item']['app_name'], a['item']['dir_path'], a['row']['id']))
            elif a['action'] == 'add':
                cursor.execute("INSERT INTO `%s` (app_name, dir_path, save_log) VALUES (%%s, %%s, %%s)"
                               % TABLE, (a['item']['app_name'], a['item']['dir_path'], '[]'))
            done[a['action']] += 1
        conn.commit()
        logging.info("data_saver bulk_apply: %s", done)
        return jsonify({'success': True, 'done': done,
                        'message': '追加 %d・訂正 %d・削除 %d を反映しました'
                                   % (done['add'], done['update'], done['delete'])})
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        logging.error("data_saver bulk_apply error: %s", e)
        return jsonify({'success': False, 'error': '反映に失敗したため，表は元のままです: %s' % e}), 500
    finally:
        _close(conn, cursor)
