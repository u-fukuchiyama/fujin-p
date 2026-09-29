"""ゆにこん v0.1 の画面（殻）。

トップダッシュボード＝プロジェクトの一覧（題目・編集者・最終変更日時・操作）。
操作は 閲覧／編集（いまは基本情報だけ）／エクスポート／インポート／削除。
"""

import json
import time
from functools import wraps

from flask import (Response, abort, flash, jsonify, redirect, render_template, request, session,
                   url_for)

import auth
from decorators import login_required

from . import model, store, unicon_bp


@unicon_bp.before_request
def _gate():
    """deny by default。いまはログイン済みだけを通す（閲覧者・担当者の経路は後で開ける）。"""
    if request.endpoint in (None, 'unicon.static'):
        return None
    if not session.get('user_id'):
        return redirect(url_for('auth.login', next=request.path))
    return None


@unicon_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    return auth.redirect_to_dashboard()


# ------------------------------------------------------------------ 本人と権限

def me():
    uid = session.get('user_id')
    c = session.get('unicon_me')
    if c and c.get('id') == uid and (time.time() - c.get('at', 0)) < 600:
        return c
    c = {'id': uid, 'admin': session.get('user_category') == 'admin',
         'groups': sorted(store.group_names_of(uid)), 'at': time.time()}
    session['unicon_me'] = c
    return c


def is_editor(m):
    """プロジェクトを作れる人：カテゴリ admin か，まいぐるの admin／ゆにこん編集者の一員。"""
    return bool(m.get('admin') or set(m.get('groups') or []) & set(store.EDITOR_GROUPS))


def can_manage(m, p):
    """そのプロジェクトを仕切れる人：admin，プロジェクトの編集者，作った人。"""
    if not p:
        return False
    return bool(m.get('admin') or p.get('editor_user_id') == m.get('id') or p.get('created_by') == m.get('id'))


def editor_required(f):
    @wraps(f)
    def inner(*a, **k):
        if not is_editor(me()):
            flash('ゆにこんの編集者だけが使えます', 'ng')
            return redirect(url_for('unicon.index'))
        return f(*a, **k)
    return inner


def _project_or_404(pid, with_data=False, manage=True):
    p = store.get_project(pid, with_data=with_data)
    if not p:
        abort(404)
    m = me()
    if manage and not can_manage(m, p):
        abort(403)
    if not manage and not (can_manage(m, p) or is_editor(m)):
        abort(403)
    return p


def _read_upload():
    """アップロードされた JSON を読んでプロジェクトの形にする。(data, エラー文)"""
    f = request.files.get('file')
    if not f or not f.filename:
        return None, 'ファイルを選んでください'
    if not f.filename.lower().endswith('.json'):
        return None, '.json（プロジェクト）か .xlsx（対応付け）のファイルを選んでください'
    try:
        obj = json.loads(f.read().decode('utf-8-sig'))
    except Exception as e:
        return None, 'JSON として読めません：%s' % e
    return store.normalize(obj)


# ------------------------------------------------------------------ トップダッシュボード

@unicon_bp.route('/')
@login_required
def index():
    m = me()
    editor = is_editor(m)
    try:
        rows = store.list_projects()
        db_error = None
    except Exception as e:
        rows, db_error = [], str(e)
    shown = []
    for r in rows:
        r['can_manage'] = can_manage(m, r)
        if r['can_manage'] or editor:
            shown.append(r)
    return render_template('unicon/index.html', projects=shown, editor=editor, db_error=db_error)


@unicon_bp.route('/new', methods=['POST'])
@login_required
@editor_required
def new_project():
    title = (request.form.get('title') or '').strip() or '（無題）'
    pid = store.create_project(title, store.empty_data(title), session.get('user_id'))
    flash('プロジェクト「%s」を作りました' % title, 'ok')
    return redirect(url_for('unicon.edit', pid=pid))


@unicon_bp.route('/import', methods=['POST'])
@login_required
@editor_required
def import_new():
    """ファイルから新しいプロジェクトを作る。"""
    data, err = _read_upload()
    if err:
        flash(err, 'ng')
        return redirect(url_for('unicon.index'))
    title = (request.form.get('title') or '').strip() or data.get('title') or '（無題）'
    pid = store.create_project(title, data, session.get('user_id'))
    s = store.summarize(data)
    flash('「%s」を取り込みました（作品 %d・複合部品 %d・倉庫部品 %d・箇条 %d）'
          % (title, s['works'], s['parts'], s['boxes'], s['items']), 'ok')
    return redirect(url_for('unicon.view', pid=pid))


# ------------------------------------------------------------------ 閲覧

@unicon_bp.route('/p/<int:pid>')
@login_required
def view(pid):
    p = _project_or_404(pid, with_data=True, manage=False)
    pj = model.Project(p['data'])
    works = pj.listing(model.WORK)
    parts = pj.listing(model.PART)
    sel = pj.get(request.args.get('w')) if request.args.get('w') else (works[0] if works else None)
    if sel is not None and sel.get('recipe') != 'arena':
        sel = None
    html = pj.render(sel) if sel else None
    return render_template('unicon/view.html', p=p, works=works, parts=parts, sel=sel, html=html,
                           level=pj.level_of(sel) if sel else None,
                           can_manage=can_manage(me(), p), n_boxes=len(pj.listing('box')))


# ------------------------------------------------------------------ 編集（いまは基本情報だけ）

@unicon_bp.route('/p/<int:pid>/edit', methods=['GET', 'POST'])
@login_required
def edit(pid):
    p = _project_or_404(pid)
    if request.method == 'POST':
        try:
            editor_id = int(request.form.get('editor_user_id') or 0) or None
        except ValueError:
            editor_id = None
        store.update_meta(pid, (request.form.get('title') or '').strip(), editor_id,
                          (request.form.get('note') or '').strip(), session.get('user_id'))
        flash('基本情報を保存しました', 'ok')
        return redirect(url_for('unicon.edit', pid=pid))
    return render_template('unicon/edit.html', p=p, users=store.users_list())


# ------------------------------------------------------------------ 削除・エクスポート・インポート

@unicon_bp.route('/p/<int:pid>/delete', methods=['POST'])
@login_required
def delete(pid):
    p = _project_or_404(pid)
    store.delete_project(pid, session.get('user_id'))
    flash('プロジェクト「%s」を削除しました' % p['title'], 'ok')
    return redirect(url_for('unicon.index'))


@unicon_bp.route('/p/<int:pid>/export.json')
@login_required
def export(pid):
    p = _project_or_404(pid, with_data=True)
    data = p['data']
    data['title'] = p['title']
    data['exported_at'] = store._now().strftime('%Y-%m-%d %H:%M:%S')
    body = json.dumps(data, ensure_ascii=False, indent=1)
    name = 'unicon_%s_%s.json' % (pid, store._now().strftime('%Y%m%d_%H%M%S'))
    return Response(body, mimetype='application/json; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="%s"' % name})


@unicon_bp.route('/p/<int:pid>/import', methods=['POST'])
@login_required
def import_replace(pid):
    """.json ならこのプロジェクトの中身を丸ごと差し替える（題目・編集者はそのまま）。
    .xlsx なら対応付けシートとして取り込む（対応付け画面の取り込みと同じ）。"""
    f = request.files.get('file')
    if f and f.filename and f.filename.lower().endswith('.xlsx'):
        return mapping_import(pid)
    _project_or_404(pid)
    data, err = _read_upload()
    if err:
        flash(err, 'ng')
        return redirect(url_for('unicon.index'))
    store.replace_data(pid, data, session.get('user_id'))
    s = store.summarize(data)
    flash('中身を差し替えました（作品 %d・複合部品 %d・倉庫部品 %d・箇条 %d）'
          % (s['works'], s['parts'], s['boxes'], s['items']), 'ok')
    return redirect(url_for('unicon.index'))


# ------------------------------------------------------------------ テーブルとの対応付け

from . import mapping as mp  # noqa: E402


def _save_mapping(p, m):
    m['updated_at'] = store._now().strftime('%Y-%m-%d %H:%M:%S')
    m['updated_by'] = session.get('user_id')
    data = p['data']
    data['mapping'] = m
    store.replace_data(p['id'], data, session.get('user_id'))


@unicon_bp.route('/p/<int:pid>/mapping')
@login_required
def mapping_page(pid):
    p = _project_or_404(pid, with_data=True)
    pj = model.Project(p['data'])
    m = mp.get(p['data'])
    rep = mp.check(pj, m, mp.all_group_names())
    names = store.user_names([m.get('updated_by')])
    grids = [(k, mp.TABS[k], mp.spec(k), m.get(k) or [], rep['tips'][k]) for k in mp.KINDS]
    return render_template('unicon/mapping.html', p=p, m=m, rep=rep, grids=grids,
                           str_keys=mp.STR_KEYS,
                           updated_by_name=names.get(m.get('updated_by'), ''))


@unicon_bp.route('/p/<int:pid>/mapping/save', methods=['POST'])
@login_required
def mapping_save(pid):
    p = _project_or_404(pid, with_data=True)
    body = request.get_json(silent=True) or {}
    m = mp.get(p['data'])
    for k in mp.KINDS:
        if k in body:
            m[k] = mp.normalize_rows(k, body.get(k))
    _save_mapping(p, m)
    rep = mp.check(model.Project(p['data']), m, mp.all_group_names())
    return jsonify(ok=True, errors=len(rep['errors']), warnings=len(rep['warnings']))


@unicon_bp.route('/p/<int:pid>/mapping.xlsx')
@login_required
def mapping_xlsx(pid):
    p = _project_or_404(pid, with_data=True)
    data = mp.to_xlsx(model.Project(p['data']), mp.get(p['data']))
    name = 'unicon_%s_mapping_%s.xlsx' % (pid, store._now().strftime('%Y%m%d_%H%M%S'))
    return Response(data, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': 'attachment; filename="%s"' % name})


@unicon_bp.route('/p/<int:pid>/mapping/import', methods=['POST'])
@login_required
def mapping_import(pid):
    """xlsx の対応付けで置き換える。シートが無い側（細目／計画番号）はいまのまま残す。"""
    p = _project_or_404(pid, with_data=True)
    f = request.files.get('file')
    if not f or not f.filename or not f.filename.lower().endswith('.xlsx'):
        flash('.xlsx のファイルを選んでください', 'ng')
        return redirect(url_for('unicon.mapping_page', pid=pid))
    new, err = mp.from_xlsx(f.read())
    if err:
        flash(err, 'ng')
        return redirect(url_for('unicon.mapping_page', pid=pid))
    m = mp.get(p['data'])
    for kind in new.get('_sheets') or ():
        m[kind] = new[kind]
    _save_mapping(p, m)
    rep = mp.check(model.Project(p['data']), m, mp.all_group_names())
    flash('対応付けを取り込みました（細目 %d 行・計画番号 %d 行・中期細目 %d 行・中期評価 %d 行，誤り %d・注意 %d）'
          % (rep['n_details'], rep['n_plans'], rep['n_mid_details'], rep['n_mid_evals'],
             len(rep['errors']), len(rep['warnings'])),
          'ng' if rep['errors'] else 'ok')
    return redirect(url_for('unicon.mapping_page', pid=pid))



# ------------------------------------------------------------------ テーブルへの書き込み・テーブルからの読み出し

from . import sync  # noqa: E402


def _flash_notes(items, limit=15):
    for t in items[:limit]:
        flash(t, 'ng')
    if len(items) > limit:
        flash('ほか %d 件' % (len(items) - limit), 'ng')


@unicon_bp.route('/p/<int:pid>/sync/to_table', methods=['POST'])
@login_required
def sync_to_table(pid):
    """部品の中身を常設テーブル（T_ann_／T_mid_ の4表）へ書く。"""
    p = _project_or_404(pid, with_data=True)
    try:
        rep = sync.to_table(p['data'], session.get('user_id'))
    except Exception as e:
        flash('テーブルへ書き込めませんでした：%s' % e, 'ng')
        return redirect(url_for('unicon.mapping_page', pid=pid))
    flash('テーブルへ書き込みました（新しい行 %d・書き換えた行 %d・変わらなかった行 %d）'
          % (rep['inserted'], rep['updated'], rep['same']), 'ok')
    _flash_notes(rep['skipped'] + rep['notes'])
    return redirect(url_for('unicon.mapping_page', pid=pid))


@unicon_bp.route('/p/<int:pid>/sync/from_table', methods=['POST'])
@login_required
def sync_from_table(pid):
    """テーブルの中身を部品へ書き，プロジェクトを保存する。"""
    p = _project_or_404(pid, with_data=True)
    data = p['data']
    try:
        rep = sync.from_table(data)
    except Exception as e:
        flash('テーブルから読み出せませんでした：%s' % e, 'ng')
        return redirect(url_for('unicon.mapping_page', pid=pid))
    if rep['changed'] or rep['cleared'] or rep['added_items'] or rep['removed_items']:
        store.replace_data(pid, data, session.get('user_id'))
    msg = 'テーブルから読み出して部品に書きました（書き換えた部品 %d・空にした部品 %d・変わらなかった部品 %d' % (
        rep['changed'], rep['cleared'], rep['same'])
    if rep['added_items'] or rep['removed_items']:
        msg += '・足した箇条 %d・外した箇条 %d' % (rep['added_items'], rep['removed_items'])
    if rep['empty']:
        msg += '・テーブルが空だった欄 %d' % rep['empty']
    if rep['missing_rows']:
        msg += '・テーブルに行が無かった %d 行（空として扱いました）' % rep['missing_rows']
    flash(msg + '）', 'ok')
    _flash_notes(rep['conflicts'] + rep['notes'])
    return redirect(url_for('unicon.mapping_page', pid=pid))
