"""
mid_term_progress - まるごと移行（サイト間でデータを運ぶ）

事業全貌が使う8表を1つの JSON ファイルにまとめて書き出し，別のサイトで1回の操作で取り込む．
  常設4表：T_mid_plan_details／T_mid_plan_evaluations／T_ann_plan_details／T_ann_plan_evaluations
  ワークフロー：T_ann_plan_requests（執筆依頼）／T_ann_eval_questions（法人評価の質問）／
                T_ann_eval_posts（連絡の窓）／mid_term_progress_settings（執筆者に開いている依頼・担当部門の並び）

ファイルには各表の SHOW CREATE TABLE も入れる．取り込み側に表が無ければそれで作り，
列が足りなければ足す（列の削除・型の変更はしない）．
取り込みは「置き換え」：8表の中身をいったん空にして，ファイルの中身だけにする（1トランザクション）．
T_ann_eval_posts.question_id は取り込み先で振り直される質問の id に付け替える
（書き出し時に質問の年度・質問番号を _q_fiscal_year／_q_no として添える）．
"""
import datetime
import decimal
import json
import logging
import re

from flask import request, jsonify, session, Response

from . import mid_term_progress_bp
from .routes import login_required, require_upload_perm, _fdb, _s, get_jst_now, _load_columns
from .workflow import SETTINGS_TABLE, SETTINGS_DDL, load_active_sets

EXPORT_TYPE = 'mid_term_progress_data'
FORMAT_VERSION = 1

# 取り込みの順（posts は questions の後）．値は並べ替えの列
MIGRATE_TABLES = [
    (SETTINGS_TABLE, 'k'),
    ('T_mid_plan_details', 'term, sub_term, mid_plan_no, detail_no'),
    ('T_mid_plan_evaluations', 'term, sub_term, mid_plan_no, phase'),
    ('T_ann_plan_details', 'fiscal_year, mid_plan_no, annual_plan_no, detail_no'),
    ('T_ann_plan_evaluations', 'fiscal_year, mid_plan_no, annual_plan_no'),
    ('T_ann_plan_requests', 'fiscal_year, mid_plan_no, annual_plan_no, detail_no, stage'),
    ('T_ann_eval_questions', 'fiscal_year, q_no'),
    ('T_ann_eval_posts', 'id'),
]
TABLE_NAMES = [t for t, _ in MIGRATE_TABLES]
LABELS = {
    SETTINGS_TABLE: '設定（執筆者に開いている依頼・担当部門の並び）',
    'T_mid_plan_details': '中期目標・中期計画',
    'T_mid_plan_evaluations': '中期計画の評価',
    'T_ann_plan_details': '年度計画の細目（計画・報告の正本と担当）',
    'T_ann_plan_evaluations': '計画番号ごとの評価',
    'T_ann_plan_requests': '執筆依頼（状態・提出・コメント）',
    'T_ann_eval_questions': '法人評価：質問・意見と回答',
    'T_ann_eval_posts': '法人評価：連絡の窓の書き込み',
}
_COL_LINE = re.compile(r'^\s*`([A-Za-z0-9_]+)`\s+(.+?),?\s*$')


# -----------------------------------------------
# 下回り
# -----------------------------------------------
def _jv(v):
    """JSON に入れる値（日時は文字列，Decimal は数）。"""
    if isinstance(v, (datetime.datetime, datetime.date)):
        return _s(v)
    if isinstance(v, decimal.Decimal):
        return int(v) if v == int(v) else float(v)
    if isinstance(v, (bytes, bytearray)):
        return v.decode('utf-8', 'replace')
    return v


def _exists(cur, table):
    cur.execute('SHOW TABLES LIKE %s', (table,))
    return bool(cur.fetchall())


def _columns(cur, table):
    cur.execute('SHOW COLUMNS FROM `%s`' % table)
    return [r['Field'] for r in cur.fetchall()]


def _ddl(cur, table):
    cur.execute('SHOW CREATE TABLE `%s`' % table)
    r = cur.fetchone()
    return r.get('Create Table') if r else None


def _db_name(cur):
    cur.execute('SELECT DATABASE() AS d')
    return (cur.fetchone() or {}).get('d') or ''


def _ddl_columns(ddl):
    """CREATE TABLE 文から 列名 → 列定義 を取り出す（並び順どおり）。"""
    out = []
    for line in (ddl or '').splitlines()[1:]:
        m = _COL_LINE.match(line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def _safe_ddl(table, ddl):
    """ファイルの DDL が，その表の CREATE TABLE 文1つだけであることを確かめる。"""
    if not ddl or ';' in ddl:
        return None
    head = 'CREATE TABLE `%s`' % table
    if ddl.startswith(head):
        return 'CREATE TABLE IF NOT EXISTS `%s`' % table + ddl[len(head):]
    if ddl.startswith('CREATE TABLE IF NOT EXISTS `%s`' % table):
        return ddl
    return None


# -----------------------------------------------
# 状況（8表の行数）
# -----------------------------------------------
@mid_term_progress_bp.route('/api/migrate/status')
@login_required
@require_upload_perm
def api_migrate_status():
    try:
        with _fdb() as (cur, conn):
            db = _db_name(cur)
            rows = []
            for t in TABLE_NAMES:
                ex = _exists(cur, t)
                n = None
                if ex:
                    cur.execute('SELECT COUNT(*) AS n FROM `%s`' % t)
                    n = cur.fetchone()['n']
                rows.append({'table': t, 'label': LABELS[t], 'exists': ex, 'rows': n})
        return jsonify({'success': True, 'database': db, 'tables': rows,
                        'active_sets': load_active_sets(), 'columns': _load_columns()})
    except Exception as e:
        logging.error('api_migrate_status error: %s', e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# -----------------------------------------------
# 書き出し
# -----------------------------------------------
def _export_table(cur, table, order):
    if not _exists(cur, table):
        return None
    ddl = _ddl(cur, table)
    if table == 'T_ann_eval_posts' and _exists(cur, 'T_ann_eval_questions'):
        cur.execute('SELECT p.*, q.fiscal_year AS _q_fiscal_year, q.q_no AS _q_no '
                    'FROM T_ann_eval_posts p LEFT JOIN T_ann_eval_questions q ON q.id = p.question_id '
                    'ORDER BY p.id')
    else:
        cur.execute('SELECT * FROM `%s` ORDER BY %s' % (table, order))
    data = cur.fetchall()
    cols = [d[0] for d in cur.description]
    return {'name': table, 'ddl': ddl, 'columns': cols,
            'rows': [[_jv(r.get(c)) for c in cols] for r in data]}


@mid_term_progress_bp.route('/migrate/export')
@login_required
@require_upload_perm
def migrate_export():
    try:
        now = get_jst_now()
        tables = []
        with _fdb() as (cur, conn):
            db = _db_name(cur)
            for t, order in MIGRATE_TABLES:
                x = _export_table(cur, t, order)
                if t == SETTINGS_TABLE:
                    # 旧版のファイル置きの設定も運ぶ（設定表が無い，または行が無いサイト）
                    if x is None:
                        x = {'name': t, 'ddl': SETTINGS_DDL.replace('CREATE TABLE IF NOT EXISTS', 'CREATE TABLE', 1),
                             'columns': ['k', 'v', 'updated_at', 'updated_by'], 'rows': []}
                    ki = x['columns'].index('k')
                    have = {r[ki] for r in x['rows']}
                    for k, val in (('active_sets', {'sets': load_active_sets()}),
                                   ('matrix_columns', {'columns': _load_columns()})):
                        if k not in have:
                            row = {'k': k, 'v': json.dumps(val, ensure_ascii=False),
                                   'updated_at': _s(now), 'updated_by': None}
                            x['rows'].append([row.get(c) for c in x['columns']])
                if x is None:
                    x = {'name': t, 'missing': True}
                tables.append(x)
        site = (request.host or '').split('.')[0]
        body = {'export_type': EXPORT_TYPE, 'format_version': FORMAT_VERSION, 'app_name': 'mid_term_progress',
                'site': site, 'database': db, 'generated_at': _s(now),
                'generated_by': session.get('user_name') or session.get('user_id'),
                'tables': tables}
        fn = 'mid_term_progress_data_%s_%s.json' % (site or 'site', now.strftime('%Y%m%d_%H%M%S'))
        return Response(json.dumps(body, ensure_ascii=False), mimetype='application/json',
                        headers={'Content-Disposition': 'attachment; filename="%s"' % fn})
    except Exception as e:
        logging.error('migrate_export error: %s', e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# -----------------------------------------------
# 取り込み（確認 → 実行）
# -----------------------------------------------
def _read_upload():
    f = request.files.get('file')
    if not f or not f.filename:
        raise ValueError('ファイルが必要です')
    try:
        body = json.loads(f.read().decode('utf-8-sig'))
    except Exception:
        raise ValueError('JSON として読めません')
    if body.get('export_type') != EXPORT_TYPE:
        raise ValueError('事業全貌の「まるごと移行」で書き出したファイルではありません')
    if int(body.get('format_version') or 0) > FORMAT_VERSION:
        raise ValueError('このファイルは新しい形式です．アプリを更新してから取り込んでください')
    tables = {}
    for x in body.get('tables') or []:
        if x.get('name') in TABLE_NAMES:
            tables[x['name']] = x
    return body, tables


def _plan(cur, tables):
    """表ごとに，作る／列を足す／置き換える行数を調べる（書き込みはしない）。"""
    out = []
    for t in TABLE_NAMES:
        x = tables.get(t)
        p = {'table': t, 'label': LABELS[t], 'in_file': bool(x and not x.get('missing')),
             'file_rows': len(x.get('rows') or []) if x else 0, 'exists': _exists(cur, t),
             'now_rows': None, 'create': False, 'add_columns': [], 'skip_columns': [], 'error': None}
        if p['exists']:
            cur.execute('SELECT COUNT(*) AS n FROM `%s`' % t)
            p['now_rows'] = cur.fetchone()['n']
        if not p['in_file']:
            out.append(p)
            continue
        if not p['exists']:
            if _safe_ddl(t, x.get('ddl')) is None:
                p['error'] = '表を作るための定義がファイルにありません'
            p['create'] = True
        else:
            have = set(_columns(cur, t))
            defs = dict(_ddl_columns(x.get('ddl')))
            for c in x.get('columns') or []:
                if c.startswith('_') or c in have:
                    continue
                (p['add_columns'] if c in defs else p['skip_columns']).append(c)
        out.append(p)
    return out


@mid_term_progress_bp.route('/api/migrate/check', methods=['POST'])
@login_required
@require_upload_perm
def api_migrate_check():
    try:
        body, tables = _read_upload()
        with _fdb() as (cur, conn):
            db = _db_name(cur)
            plan = _plan(cur, tables)
        return jsonify({'success': True, 'source': {k: body.get(k) for k in ('site', 'database', 'generated_at',
                                                                              'generated_by')},
                        'database': db, 'plan': plan, 'ok': not any(p['error'] for p in plan)})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logging.error('api_migrate_check error: %s', e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


def _insert_rows(cur, table, cols, rows, batch=200):
    sql = 'INSERT INTO `%s` (%s) VALUES (%s)' % (table, ', '.join('`%s`' % c for c in cols),
                                                 ', '.join(['%s'] * len(cols)))
    for i in range(0, len(rows), batch):
        cur.executemany(sql, rows[i:i + batch])


@mid_term_progress_bp.route('/migrate/import', methods=['POST'])
@login_required
@require_upload_perm
def migrate_import():
    try:
        body, tables = _read_upload()
        report, notes = [], []
        with _fdb() as (cur, conn):
            plan = _plan(cur, tables)
            bad = [p for p in plan if p['error']]
            if bad:
                raise ValueError('；'.join('%s：%s' % (p['table'], p['error']) for p in bad))
            # 1) 表を作る・列を足す（DDL はトランザクションに入らないので先に済ませる）
            for p in plan:
                if not p['in_file']:
                    continue
                x = tables[p['table']]
                if p['create']:
                    cur.execute(_safe_ddl(p['table'], x['ddl']))
                    notes.append('%s を作成' % p['table'])
                defs = dict(_ddl_columns(x.get('ddl')))
                for c in p['add_columns']:
                    cur.execute('ALTER TABLE `%s` ADD COLUMN `%s` %s' % (p['table'], c, defs[c]))
                    notes.append('%s に列 %s を追加' % (p['table'], c))
            conn.commit()
            # 2) 中身を置き換える（1トランザクション）
            qmap = {}
            try:
                for p in plan:
                    if not p['in_file']:
                        continue
                    t, x = p['table'], tables[p['table']]
                    have = set(_columns(cur, t))
                    src = x.get('columns') or []
                    use = [c for c in src if c in have and c != 'id' and not c.startswith('_')]
                    idx = [src.index(c) for c in use]
                    rows = [[r[i] if i < len(r) else None for i in idx] for r in (x.get('rows') or [])]
                    skipped = 0
                    if t == 'T_ann_eval_posts':
                        qi = use.index('question_id') if 'question_id' in use else None
                        fy, qn = (src.index('_q_fiscal_year') if '_q_fiscal_year' in src else None,
                                  src.index('_q_no') if '_q_no' in src else None)
                        kept = []
                        for raw, r in zip(x.get('rows') or [], rows):
                            key = (raw[fy], raw[qn]) if fy is not None and qn is not None else None
                            if qi is None or key not in qmap:
                                skipped += 1
                                continue
                            r[qi] = qmap[key]
                            kept.append(r)
                        rows = kept
                    cur.execute('DELETE FROM `%s`' % t)
                    deleted = cur.rowcount
                    if rows and use:
                        _insert_rows(cur, t, use, rows)
                    if t == 'T_ann_eval_questions':
                        cur.execute('SELECT id, fiscal_year, q_no FROM T_ann_eval_questions')
                        qmap = {(r['fiscal_year'], r['q_no']): r['id'] for r in cur.fetchall()}
                    msg = '%s：%d 行（元の %d 行と置き換え）' % (t, len(rows), deleted)
                    if skipped:
                        msg += '．質問が見つからない書き込み %d 件は入れていません' % skipped
                    if p['skip_columns']:
                        msg += '．取り込み先に無い列 %s は読み飛ばし' % '・'.join(p['skip_columns'])
                    report.append(msg)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return jsonify({'success': True, 'notes': notes, 'report': report})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logging.error('migrate_import error: %s', e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
