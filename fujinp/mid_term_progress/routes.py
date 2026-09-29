"""
mid_term_progress - ルート定義
中核は fujinp の DB の常設4表（T_mid_plan_details／T_mid_plan_evaluations／
T_ann_plan_details／T_ann_plan_evaluations．DDL は schema_core.sql）．事業全貌はこの4表だけで単独に動く．
ほかのアプリ（ゆにこんなど）が同じ表を読み書きしてもよい．

閲覧は4表から組み立てる．ダウンロードは4表をそのまま xlsx に出す．
アップロードは非常対応用で，ダウンロードと同じ形の xlsx を4表へ書き戻す．
"""
import datetime
import json
import os
import logging
import re
from auth import redirect_to_dashboard

from pytz import timezone
from flask import render_template, request, jsonify, session, abort
from functools import wraps

try:
    import openpyxl
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False

try:
    import markdown
    MARKDOWN_AVAILABLE = True
except ImportError:
    MARKDOWN_AVAILABLE = False

try:
    import mysql.connector
    from config import Config
    from db import DatabaseConfig, Tables
    import db as _db_module
    from decorators import login_required
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    def login_required(f):
        return f

from . import mid_term_progress_bp

JST = timezone('Asia/Tokyo')

# -----------------------------------------------
# 常設4表（nishida$fujinp）
# -----------------------------------------------
TABLE_DB = 'fujinp'          # db.get_db_cursor(database=...) に渡す名前
TABLES = {
    'T_ann_plan_details':     ('fiscal_year', 'mid_plan_no', 'annual_plan_no', 'detail_no'),
    'T_ann_plan_evaluations': ('fiscal_year', 'mid_plan_no', 'annual_plan_no'),
    'T_mid_plan_details':     ('term', 'sub_term', 'mid_plan_no', 'detail_no'),
    'T_mid_plan_evaluations': ('term', 'sub_term', 'mid_plan_no', 'phase'),
}
AUTO_COLS = {'id', 'created_at', 'updated_at', 'updated_by'}


def term_label(term):
    return '第%s期' % term


# -----------------------------------------------
# アップロード権限管理
# -----------------------------------------------
UPLOAD_MANAGER_GROUP = '中期計画進捗管理者'   # 編集・割り当て・データ管理ができるグループ（admin も可）


def _is_admin():
    return session.get('user_category') == 'admin'


def _ug(name):
    """まいぐる（ユーザとグループ）の公開APIを遅延で引く。無ければ None。"""
    try:
        from fujinp.user_groups import utils as _u
        return getattr(_u, name, None)
    except Exception:
        return None


def _get_user_group_names(user_id):
    if not user_id:
        return []
    f = _ug('get_user_group_names')
    if f:
        try:
            return list(f(user_id))
        except Exception:
            pass
    try:
        now = get_jst_now()
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT g.name
              FROM user_group_memberships m
              JOIN user_groups g ON g.id = m.group_id
             WHERE m.user_id = %s
               AND (m.valid_from  IS NULL OR m.valid_from  <= %s)
               AND (m.valid_until IS NULL OR m.valid_until >= %s)
            """,
            (user_id, now, now)
        )
        return [r['name'] for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close()
            conn.close()


def can_upload():
    if _is_admin():
        return True
    uid = session.get('user_id')
    if not uid:
        return False
    return UPLOAD_MANAGER_GROUP in _get_user_group_names(uid)


def require_upload_perm(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not can_upload():
            abort(403)
        return f(*args, **kwargs)
    return wrapped


def get_jst_now():
    return datetime.datetime.now(JST).replace(tzinfo=None)


def md_to_html(text):
    if not text or not isinstance(text, str):
        return ''
    if MARKDOWN_AVAILABLE:
        return markdown.markdown(text, extensions=['nl2br'])
    import html as html_module
    escaped = html_module.escape(text)
    lines = escaped.split('\n')
    result = []
    in_ul = False
    for line in lines:
        s = line.strip()
        if s.startswith('- ') or s.startswith('* ') or s.startswith('・'):
            if not in_ul:
                result.append('<ul>')
                in_ul = True
            item = s[2:] if s[0] in '-*' else s[1:]
            result.append(f'<li>{item.strip()}</li>')
        else:
            if in_ul:
                result.append('</ul>')
                in_ul = False
            if s:
                result.append(f'<p>{s}</p>')
    if in_ul:
        result.append('</ul>')
    return '\n'.join(result)


def detect_year(filename, sheet_title):
    for src in [filename, sheet_title]:
        m = re.search(r'(\d{4})年度', src)
        if m:
            return int(m.group(1))
        m = re.search(r'令和(\d+)', src)
        if m:
            return int(m.group(1)) + 2018
        m = re.search(r'平成(\d+)', src)
        if m:
            return int(m.group(1)) + 1988
    m = re.search(r'(20\d{2})', filename + sheet_title)
    if m:
        return int(m.group(1))
    return None


def safe_str(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None

def safe_int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


# -----------------------------------------------
# DB（常設4表）
# -----------------------------------------------
from contextlib import contextmanager


@contextmanager
def _fdb():
    """nishida$fujinp への (cursor, conn)。カーソルは辞書型。"""
    obj = _db_module.get_db_cursor(database=TABLE_DB)
    ctx = None
    if hasattr(obj, '__enter__') and not hasattr(obj, 'cursor'):
        ctx = obj
        obj = ctx.__enter__()
    conn = obj[1] if isinstance(obj, (tuple, list)) else obj
    cur = conn.cursor(dictionary=True)
    try:
        yield cur, conn
    finally:
        try:
            cur.close()
        except Exception:
            pass
        if ctx is not None:
            ctx.__exit__(None, None, None)
        elif not isinstance(obj, (tuple, list)):
            try:
                conn.close()
            except Exception:
                pass


def _s(v):
    """日時3層ルール：日時は文字列にして返す。"""
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.strftime('%Y-%m-%d %H:%M:%S') if isinstance(v, datetime.datetime) else v.strftime('%Y-%m-%d')
    return v


def _join(texts):
    out = [t.strip() for t in texts if t and str(t).strip()]
    return '\n'.join(out)


# -----------------------------------------------
# 【同上】：計画本文がちょうど【同上】の細目は，直前の細目と同じ計画を持つ
# （計画は共通，進捗報告と業務実績報告はそれぞれ書く）
# -----------------------------------------------
SAME_MARK = '【同上】'


def is_same(text):
    return (text or '').strip() == SAME_MARK


# 対応済み：過去に対応が完了し，取り組みが不要になった細目．計画・進捗報告・業務実績報告に「・対応済み」と入れる
DONE_TEXT = '・対応済み'
_DONE_FORMS = ('対応済み', '・対応済み', '- 対応済み', '-対応済み', '【対応済み】')
DONE_SQL = "TRIM(COALESCE(%s, '')) IN ('対応済み','・対応済み','- 対応済み','-対応済み','【対応済み】')"


def is_done(text):
    return (text or '').strip() in _DONE_FORMS


def resolve_same(rows, key='plan_text', no='detail_no'):
    """1つの年度計画の細目（細目番号順）について，細目番号 → 計画の出どころの細目（なければ None）。
    【同上】でない細目は自分自身．【同上】が続くときは最初の実体のある細目までさかのぼる。"""
    out, head = {}, None
    for r in rows:
        if is_same(r.get(key)):
            out[r[no]] = head
        else:
            head = r[no]
            out[r[no]] = head
    return out


# -----------------------------------------------
# 1. トップ = レポート表示（/ → view へリダイレクト）
# -----------------------------------------------
@mid_term_progress_bp.route('/')
@login_required
def index():
    from flask import redirect, url_for
    return redirect(url_for('mid_term_progress.view'))


# -----------------------------------------------
# 2. 管理画面（ダウンロードと非常用アップロード．権限: 全学評価室 / admin）
# -----------------------------------------------
@mid_term_progress_bp.route('/upload')
@login_required
@require_upload_perm
def upload_page():
    return render_template('mid_term_progress/index.html', tables=list(TABLES.keys()))


@mid_term_progress_bp.route('/api/status')
@login_required
def api_status():
    try:
        with _fdb() as (cur, conn):
            cur.execute("""SELECT term, sub_term, MIN(valid_from) AS vf, MAX(valid_to) AS vt,
                                  COUNT(DISTINCT mid_plan_no) AS plans, COUNT(*) AS n, MAX(updated_at) AS last_update
                           FROM T_mid_plan_details GROUP BY term, sub_term ORDER BY term, sub_term""")
            mids = cur.fetchall()
            cur.execute("""SELECT term, sub_term, phase, COUNT(*) AS n FROM T_mid_plan_evaluations
                           GROUP BY term, sub_term, phase ORDER BY term, sub_term, phase""")
            mevals = cur.fetchall()
            cur.execute("""SELECT d.fiscal_year, COUNT(*) AS n, COUNT(DISTINCT d.mid_plan_no, d.annual_plan_no) AS plans,
                                  MAX(d.updated_at) AS last_update,
                                  (SELECT COUNT(*) FROM T_ann_plan_evaluations e WHERE e.fiscal_year = d.fiscal_year) AS evals
                           FROM T_ann_plan_details d GROUP BY d.fiscal_year ORDER BY d.fiscal_year""")
            anns = cur.fetchall()
        for r in mids:
            r['label'] = term_label(r['term'])
        for rows in (mids, mevals, anns):
            for r in rows:
                for k in list(r):
                    r[k] = _s(r[k])
        return jsonify({'success': True, 'mid': mids, 'mid_evals': mevals, 'annual': anns})
    except Exception as e:
        logging.error('api_status error: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


# -----------------------------------------------
# 3. レポート表示（4表から組み立てる）
# -----------------------------------------------
def _goal_code(r):
    code = r.get('goal_chapter') or ''
    if r.get('goal_section'):
        code += '（%d）' % r['goal_section']
    if r.get('goal_item'):
        code += '①②③④⑤⑥⑦⑧⑨'[r['goal_item'] - 1] if 1 <= r['goal_item'] <= 9 else '-%d' % r['goal_item']
    return code


def _goal_title(r):
    return r.get('goal_item_title') or r.get('goal_section_title') or r.get('goal_chapter_title') or ''


def _merge_versions(vs, keys):
    """有効年度の順に並んだ版のうち，中身（keys）が同じ隣どうしを1つにまとめる。"""
    out = []
    for v in vs:
        if out and all(out[-1].get(k) == v.get(k) for k in keys):
            out[-1]['valid_to'] = v['valid_to']
            continue
        out.append(dict(v))
    return out


def _selected_years(all_years):
    """閲覧者が選んだ年度。?years=2024,2025 で指定し，セッションに覚える（?years=all で解除）。"""
    arg = request.args.get('years')
    if arg is not None:
        if arg in ('', 'all'):
            session.pop('mtp_years', None)
        else:
            ys = sorted({int(x) for x in arg.split(',') if x.strip().isdigit()})
            session['mtp_years'] = ys
    ys = session.get('mtp_years') or []
    return [y for y in ys if y in all_years]


def _author_open():
    """執筆者ダッシュボードの導線：所属グループがあれば未完了の依頼数（0も可），無ければ None（導線を出さない）。"""
    try:
        from .workflow import open_request_count, _my_groups
        return open_request_count() if _my_groups() else None
    except Exception:
        return None


@mid_term_progress_bp.route('/view')
@login_required
def view():
    from collections import OrderedDict
    try:
        with _fdb() as (cur, conn):
            cur.execute("SELECT * FROM T_mid_plan_details ORDER BY term, mid_goal_no, mid_plan_no, sub_term, detail_no")
            mid_rows = cur.fetchall()
            cur.execute("SELECT * FROM T_ann_plan_details ORDER BY fiscal_year, mid_plan_no, annual_plan_no, detail_no")
            ann_rows = cur.fetchall()
            # 業務実績は正本の result_text を優先して出す．正本が空の細目は，
            # 執筆者が報告してきた細目の実績（T_ann_plan_requests の業務実績報告）を出す
            # 計画の正本が空の細目も同じく，計画策定の段階の執筆者の提案を出す．正本以外を出すときは状態をバッジで示す
            author_res, author_plan = {}, {}
            try:
                cur.execute("SELECT * FROM T_ann_plan_requests WHERE stage IN ('plan', 'result') "
                            "AND author_text IS NOT NULL AND TRIM(author_text) <> ''")
                for q in cur.fetchall():
                    k = (q['fiscal_year'], q['mid_plan_no'], q['annual_plan_no'], q['detail_no'])
                    (author_res if q['stage'] == 'result' else author_plan)[k] = q
            except Exception:
                author_res, author_plan = {}, {}
            # 計画番号ごとの評価（大学の自己評価・評価委員会の評点とコメント）．各年度の行の直下に出す
            evals = {}
            try:
                cur.execute("SELECT * FROM T_ann_plan_evaluations")
                evals = {(q['fiscal_year'], q['mid_plan_no'], q['annual_plan_no']): q for q in cur.fetchall()}
            except Exception:
                evals = {}

        # (term, plan_no, sub_term) → 細目をまとめた1版
        vers = OrderedDict()
        for r in mid_rows:
            k = (r['term'], r['mid_plan_no'], r['sub_term'])
            v = vers.get(k)
            if v is None:
                v = vers[k] = {'term': r['term'], 'plan_no': r['mid_plan_no'], 'sub_term': r['sub_term'],
                               'valid_from': r.get('valid_from'), 'valid_to': r.get('valid_to'),
                               'goal_no': r.get('mid_goal_no'), 'goal_code': _goal_code(r), 'goal_title': _goal_title(r),
                               'goal_text': r.get('mid_goal_text') or '',
                               'item1': r.get('goal_chapter_title') or '',
                               'item2': ('%s%s' % ('①②③④⑤⑥⑦⑧⑨'[r['measure_no'] - 1] if r.get('measure_no') and 1 <= r['measure_no'] <= 9 else '',
                                                   r.get('measure_title') or '')).strip(),
                               'item3': ('%s %s' % (r.get('measure_sub') or '', r.get('measure_sub_title') or '')).strip(),
                               '_plans': [], '_prog': [], '_res': []}
            v['_plans'].append(r.get('mid_plan_text'))
            v['_prog'].append(r.get('progress_text'))
            v['_res'].append(r.get('result_text'))
        for v in vers.values():
            v['mid_plan'] = _join(v.pop('_plans'))
            v['mid_goal'] = v['goal_text']
            v['report_4yr'] = _join(v.pop('_prog'))
            v['report_mid'] = _join(v.pop('_res'))

        # 期ごとの骨格：目標 → 計画
        terms = OrderedDict()
        term_years = {}
        for v in vers.values():
            t = v['term']
            if t not in terms:
                terms[t] = {'id': t, 'label': term_label(t), 'years': [], 'goals': OrderedDict(),
                            'plans_ungrouped': OrderedDict()}
            lo, hi = term_years.get(t, (None, None))
            vf, vt = v.get('valid_from'), v.get('valid_to')
            term_years[t] = (min(x for x in (lo, vf) if x is not None) if (lo or vf) else None,
                             max(x for x in (hi, vt) if x is not None) if (hi or vt) else None)
            mid = terms[t]
            gk = v['goal_no'] if v['goal_no'] is not None else 0
            g = mid['goals'].get(gk)
            if g is None:
                g = mid['goals'][gk] = {'goal_code': v['goal_code'], 'goal_title': v['goal_title'],
                                        '_versions': [], 'plans': OrderedDict()}
            g['_versions'].append({'valid_from': v.get('valid_from'), 'valid_to': v.get('valid_to'),
                                   'goal_title': v['goal_title'], 'goal_text': v['goal_text'], 'sub_term': v['sub_term']})
            p = g['plans'].get(v['plan_no'])
            if p is None:
                p = g['plans'][v['plan_no']] = {'plan_no': v['plan_no'], 'annual': {}, 'report_4yr': None,
                                                'report_mid': None, '_versions': []}
            p['_versions'].append(v)
            if v['report_4yr']:
                p['report_4yr'] = v['report_4yr']
            if v['report_mid']:
                p['report_mid'] = v['report_mid']

        vkeys = ('mid_plan', 'item1', 'item2', 'item3')
        plan_index = {}
        for t, mid in terms.items():
            for g in mid['goals'].values():
                seen, gv = set(), []
                for x in sorted(g.pop('_versions'), key=lambda x: (x['valid_from'] or 0, x['sub_term'])):
                    if x['sub_term'] in seen:
                        continue
                    seen.add(x['sub_term'])
                    gv.append(x)
                g['versions'] = _merge_versions(gv, ('goal_text', 'goal_title'))
                for pno, p in g['plans'].items():
                    p['versions'] = _merge_versions(
                        sorted(p.pop('_versions'), key=lambda x: (x['valid_from'] or 0, x['sub_term'])), vkeys)
                    plan_index[(t, pno)] = p

        def term_of(year):
            for t, (lo, hi) in term_years.items():
                if lo is not None and lo <= year and (hi is None or year <= hi):
                    return t
            return None

        def version_for(p, year):
            hit = [v for v in p['versions'] if (v['valid_from'] or 0) <= year and (v['valid_to'] is None or year <= v['valid_to'])]
            return hit[-1] if hit else None

        for r in ann_rows:
            yr = r['fiscal_year']
            t = term_of(yr)
            if t is None:
                continue
            p = plan_index.get((t, r['mid_plan_no']))
            if p is None:
                continue          # 中期計画に結び付かない行（第１期の第14など）は出さない
            mv = version_for(p, yr)
            if mv is None:
                continue
            if yr not in terms[t]['years']:
                terms[t]['years'].append(yr)
            a = p['annual'].setdefault(yr, {'midterm': mv, 'entries': []})
            from .progress import fallback_badge
            rk = (r['fiscal_year'], r['mid_plan_no'], r['annual_plan_no'], r['detail_no'])
            plan, plan_badge = (r.get('plan_text') or '').strip(), None
            if not plan and rk in author_plan:
                plan, plan_badge = author_plan[rk]['author_text'], fallback_badge(author_plan[rk], '執筆者の提案')
            rep_, rep_badge = (r.get('result_text') or '').strip(), None
            if not rep_ and rk in author_res:
                rep_, rep_badge = author_res[rk]['author_text'], fallback_badge(author_res[rk], '執筆者の報告')
            a['entries'].append({'annual_sub_no': r.get('detail_no') or '',
                                 'annual_plan_no': r.get('annual_plan_no'),
                                 'annual_plan': plan, 'plan_badge': plan_badge,
                                 'report_content': rep_, 'report_badge': rep_badge})
        for mid in terms.values():
            mid['years'].sort()
        all_years = sorted({y for m in terms.values() for y in m['years']})
        sel = _selected_years(all_years)
        if sel:
            for t in list(terms):
                terms[t]['years'] = [y for y in terms[t]['years'] if y in sel]
                if not terms[t]['years']:
                    del terms[t]
        # 【同上】の細目は直前の細目にまとめる（計画は1回，業務実績は細目の順に並べる）
        for p in plan_index.values():
            for a in p['annual'].values():
                merged = []
                for e in a['entries']:
                    prev = merged[-1] if merged else None
                    if is_same(e['annual_plan']):
                        if prev is not None and prev['annual_plan_no'] == e['annual_plan_no']:
                            prev['report_content'] = _join([prev['report_content'], e['report_content']])
                            prev['report_badge'] = prev.get('report_badge') or e.get('report_badge')
                            continue
                        e['annual_plan'] = ''      # 相手のない【同上】は空欄として出す
                    merged.append(e)
                a['entries'] = merged
                seq = {}
                for e in merged:                   # まとめた後の並びで項番を振り直す
                    seq[e['annual_plan_no']] = seq.get(e['annual_plan_no'], 0) + 1
                    e['annual_sub_no'] = seq[e['annual_plan_no']]
        # 年度ごとの評価行：年度計画番号ごとに1行（自己評価・評価委員会のどちらかがあるときだけ）
        for (t, pno), p in plan_index.items():
            for yr, a in p['annual'].items():
                aps = []
                for e in a['entries']:
                    if e['annual_plan_no'] not in aps:
                        aps.append(e['annual_plan_no'])
                a['evals'] = []
                for ap in aps:
                    q = evals.get((yr, pno, ap))
                    if not q:
                        continue
                    se, ce = q.get('self_eval'), q.get('committee_eval')
                    sr, cc = (q.get('self_eval_reason') or '').strip(), (q.get('committee_comment') or '').strip()
                    if se is None and ce is None and not sr and not cc:
                        continue
                    a['evals'].append({'no_label': str(ap) if len(aps) > 1 and ap is not None else '',
                                       'self_eval': se, 'self_reason': sr,
                                       'drafting': q.get('self_eval_status') == 'drafting',
                                       'committee_eval': ce, 'committee_comment': cc})
        # 年度計画番号の見出し：同じ年度計画に細目が複数あるときだけ「番号-細目」にする
        for p in plan_index.values():
            for a in p['annual'].values():
                cnt = {}
                for e in a['entries']:
                    cnt[e['annual_plan_no']] = cnt.get(e['annual_plan_no'], 0) + 1
                for e in a['entries']:
                    no = e['annual_plan_no']
                    e['no_label'] = ('' if no is None else
                                     ('%s-%s' % (no, e['annual_sub_no']) if cnt[no] > 1 else str(no)))

        return render_template(
            'mid_term_progress/report.html',
            midterm_map=terms,
            md_to_html=md_to_html,
            generated_at=get_jst_now().strftime('%Y年%m月%d日 %H:%M'),
            can_upload=can_upload(),
            author_open=_author_open(),
            all_years=all_years,
            selected_years=sel,
        )
    except Exception as e:
        logging.error('view error: %s', e, exc_info=True)
        return f'<h3>エラー: {e}</h3>', 500


# -----------------------------------------------
# 4. ダウンロード（4表をそのまま xlsx に．シート名＝テーブル名）
# -----------------------------------------------
def _xlsx_response(sheets, filename):
    from io import BytesIO
    from flask import send_file
    from openpyxl.styles import Font, PatternFill
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, cols, rows in sheets:
        ws = wb.create_sheet(name)
        ws.append(cols)
        for c in ws[1]:
            c.font = Font(bold=True)
            c.fill = PatternFill('solid', fgColor='EEF2FF')
        for r in rows:
            ws.append([_s(r.get(c)) for c in cols])
        ws.freeze_panes = 'A2'
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=filename)


def _fetch_table(cur, table, where, args, order):
    cur.execute('SELECT * FROM %s WHERE %s ORDER BY %s' % (table, where, order), args)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    return cols, rows


@mid_term_progress_bp.route('/download_mid')
@login_required
@require_upload_perm
def download_mid():
    """中期計画（期ごと）：T_mid_plan_details と T_mid_plan_evaluations の2シート。"""
    try:
        if not OPENPYXL_AVAILABLE:
            return jsonify({'success': False, 'error': 'openpyxl がインストールされていません'}), 500
        term = safe_int(request.args.get('term'))
        if term is None:
            return jsonify({'success': False, 'error': 'term を指定してください'}), 400
        with _fdb() as (cur, conn):
            s1 = _fetch_table(cur, 'T_mid_plan_details', 'term = %s', (term,), 'sub_term, mid_plan_no, detail_no')
            s2 = _fetch_table(cur, 'T_mid_plan_evaluations', 'term = %s', (term,), 'sub_term, mid_plan_no, phase')
        return _xlsx_response([('T_mid_plan_details',) + s1, ('T_mid_plan_evaluations',) + s2],
                              'T_mid_term%d_%s.xlsx' % (term, get_jst_now().strftime('%Y%m%d_%H%M')))
    except Exception as e:
        logging.error('download_mid error: %s', e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@mid_term_progress_bp.route('/download_ann')
@login_required
@require_upload_perm
def download_ann():
    """年度実績（年度の範囲）：T_ann_plan_details と T_ann_plan_evaluations の2シート。"""
    try:
        if not OPENPYXL_AVAILABLE:
            return jsonify({'success': False, 'error': 'openpyxl がインストールされていません'}), 500
        y1 = safe_int(request.args.get('year_from'))
        y2 = safe_int(request.args.get('year_to'))
        if y1 is None or y2 is None:
            return jsonify({'success': False, 'error': 'year_from と year_to を指定してください'}), 400
        if y2 < y1:
            return jsonify({'success': False, 'error': f'終了年度（{y2}）は開始年度（{y1}）以上にしてください'}), 400
        with _fdb() as (cur, conn):
            s1 = _fetch_table(cur, 'T_ann_plan_details', 'fiscal_year BETWEEN %s AND %s', (y1, y2),
                              'fiscal_year, mid_plan_no, annual_plan_no, detail_no')
            s2 = _fetch_table(cur, 'T_ann_plan_evaluations', 'fiscal_year BETWEEN %s AND %s', (y1, y2),
                              'fiscal_year, mid_plan_no, annual_plan_no')
        return _xlsx_response([('T_ann_plan_details',) + s1, ('T_ann_plan_evaluations',) + s2],
                              'T_ann_%d_%d_%s.xlsx' % (y1, y2, get_jst_now().strftime('%Y%m%d_%H%M')))
    except Exception as e:
        logging.error('download_ann error: %s', e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# -----------------------------------------------
# 5. 非常用アップロード（ダウンロードと同じ形の xlsx を4表へ書き戻す）
# -----------------------------------------------
@mid_term_progress_bp.route('/upload_tables', methods=['POST'])
@login_required
@require_upload_perm
def upload_tables():
    """シート名がテーブル名のシートだけを読み，一意キーで上書き（なければ追加）する。
    replace=1 のときは，ファイルに現れた先頭キー（年度／期）の範囲の行を先に消してから入れる。"""
    try:
        if not OPENPYXL_AVAILABLE:
            return jsonify({'success': False, 'error': 'openpyxl がインストールされていません'}), 500
        f = request.files.get('file')
        if not f or not f.filename:
            return jsonify({'success': False, 'error': 'ファイルが必要です'}), 400
        replace = request.form.get('replace') == '1'
        wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
        targets = [n for n in wb.sheetnames if n in TABLES]
        if not targets:
            return jsonify({'success': False,
                            'error': 'シート名が %s のどれかであるシートがありません' % '／'.join(TABLES)}), 400
        user_id = session.get('user_id')
        now = get_jst_now()
        report = []
        with _fdb() as (cur, conn):
            try:
                for table in targets:
                    keys = TABLES[table]
                    cur.execute('SELECT * FROM %s LIMIT 0' % table)
                    cur.fetchall()
                    tcols = [d[0] for d in cur.description]
                    rows = list(wb[table].iter_rows(values_only=True))
                    if not rows:
                        continue
                    head = [str(h).strip() if h is not None else '' for h in rows[0]]
                    use = [(i, h) for i, h in enumerate(head) if h in tcols and h not in AUTO_COLS]
                    missing = [k for k in keys if k not in [h for _, h in use]]
                    if missing:
                        raise ValueError('%s：キーの列 %s がありません' % (table, '・'.join(missing)))
                    data = []
                    for r in rows[1:]:
                        if r is None or all(v in (None, '') for v in r):
                            continue
                        d = {h: (r[i] if i < len(r) else None) for i, h in use}
                        for k in keys:
                            if k == 'phase':
                                d[k] = safe_str(d[k])
                            else:
                                d[k] = safe_int(d[k])
                        if any(d[k] is None for k in keys):
                            continue
                        for h in list(d):
                            if isinstance(d[h], str) and d[h] == '':
                                d[h] = None
                        data.append(d)
                    deleted = 0
                    if replace and data:
                        first = keys[0]
                        vals = sorted({d[first] for d in data})
                        cur.execute('DELETE FROM %s WHERE %s IN (%s)' % (table, first, ','.join(['%s'] * len(vals))),
                                    tuple(vals))
                        deleted = cur.rowcount
                    cols = [h for _, h in use]
                    upd = [c for c in cols if c not in keys]
                    sql = ('INSERT INTO %s (%s, created_at, updated_at, updated_by) VALUES (%s, %%s, %%s, %%s)'
                           % (table, ', '.join(cols), ', '.join(['%s'] * len(cols))))
                    sql += ' ON DUPLICATE KEY UPDATE ' + ', '.join(
                        ['%s = VALUES(%s)' % (c, c) for c in upd] + ['updated_at = VALUES(updated_at)',
                                                                   'updated_by = VALUES(updated_by)'])
                    for d in data:
                        cur.execute(sql, tuple(d[c] for c in cols) + (now, now, user_id))
                    report.append('%s：%d 行%s' % (table, len(data), '（先に %d 行を削除）' % deleted if replace else ''))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return jsonify({'success': True, 'message': '書き戻しました：' + '，'.join(report)})
    except Exception as e:
        logging.error('upload_tables error: %s', e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@mid_term_progress_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る"""
    return redirect_to_dashboard()


# -----------------------------------------------
# 6. 担当表（閲覧）と，細目編集が使う年度開き・部門の並び
# -----------------------------------------------
def _group_names():
    """担当に選べるグループ名（まいぐるの user_groups）。"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor()
        cur.execute('SELECT name FROM user_groups ORDER BY name')
        names = [r[0] for r in cur.fetchall() if r[0]]
        cur.close(); conn.close()
        return names
    except Exception:
        return []


@mid_term_progress_bp.route('/manage')
@login_required
def manage():
    """担当表の閲覧（計画別・部門別）。割り当ては細目編集（/editor/structure）で行う。"""
    readonly = True
    from collections import OrderedDict
    with _fdb() as (cur, conn):
        cur.execute('SELECT DISTINCT fiscal_year FROM T_ann_plan_details ORDER BY fiscal_year')
        years = [r['fiscal_year'] for r in cur.fetchall()]
        year = safe_int(request.args.get('year')) or (years[-1] if years else None)
        rows, plans = [], OrderedDict()
        if year is not None:
            cur.execute("""SELECT id, fiscal_year, mid_plan_no, annual_plan_no, detail_no, owner_group,
                                  plan_text, result_text, updated_at
                           FROM T_ann_plan_details WHERE fiscal_year = %s
                           ORDER BY mid_plan_no, annual_plan_no, detail_no""", (year,))
            rows = cur.fetchall()
            cur.execute("""SELECT term, mid_plan_no, mid_goal_no, goal_chapter, goal_section, goal_item,
                                  goal_item_title, goal_section_title, goal_chapter_title, mid_plan_text
                           FROM T_mid_plan_details
                           WHERE detail_no = 1 AND %s BETWEEN COALESCE(valid_from, 0) AND COALESCE(valid_to, 9999)
                           ORDER BY mid_plan_no, sub_term DESC""", (year,))
            mids = {}
            for m in cur.fetchall():
                mids.setdefault(m['mid_plan_no'], m)
            for r in rows:
                r['updated_at'] = _s(r['updated_at'])
                r['plan_short'] = (r.get('plan_text') or '').strip()[:120]
                r['has_text'] = bool((r.get('plan_text') or '').strip() or (r.get('result_text') or '').strip())
                p = plans.get(r['mid_plan_no'])
                if p is None:
                    m = mids.get(r['mid_plan_no']) or {}
                    p = plans[r['mid_plan_no']] = {
                        'no': r['mid_plan_no'], 'goal': _goal_code(m) if m else '',
                        'goal_title': _goal_title(m) if m else '',
                        'mid_plan': (m.get('mid_plan_text') or '').strip()[:160] if m else '', 'rows': []}
                p['rows'].append(r)
    counts = {'rows': len(rows), 'assigned': sum(1 for r in rows if (r.get('owner_group') or '').strip())}
    # 交点方式：行＝年度計画（中期計画番号×年度計画番号），列＝担当グループ
    mrows = []
    for p in plans.values():
        by_ann = {}
        for r in p['rows']:
            by_ann.setdefault(r['annual_plan_no'], []).append(r)
        for ann, rs in by_ann.items():
            mrows.append({'mid': p['no'], 'ann': ann, 'goal': p['goal'], 'goal_title': p['goal_title'],
                          'mid_plan': p['mid_plan'], 'plan': (rs[0].get('plan_short') or ''),
                          'details': [{'id': r['id'], 'detail': r['detail_no'], 'owner': (r.get('owner_group') or '').strip(),
                                       'has_text': r['has_text']} for r in rs]})
    tpl = 'manage_rows.html' if request.args.get('view') == 'rows' else 'manage_matrix.html'
    return render_template('mid_term_progress/' + tpl, years=years, year=year, rows=mrows,
                           groups=[] if readonly else _group_names(), counts=counts, columns=_load_columns(),
                           readonly=readonly, can_edit=can_upload())


def _jbody():
    return request.get_json(silent=True) or {}


@mid_term_progress_bp.route('/api/manage/open_year', methods=['POST'])
@login_required
@require_upload_perm
def api_manage_open_year():
    """年度を開く：前年度の行（中期計画番号・年度計画番号・細目・担当）を写し，本文は空で作る
    （【同上】と対応済みの印は引き継ぐ）。
    すでにある行はそのまま（足りない行だけ足す）。年度計画番号ごとの評価の行も同様に用意する。"""
    b = _jbody()
    year = safe_int(b.get('year'))
    src = safe_int(b.get('from')) or (year - 1 if year else None)
    if not year or not src:
        return jsonify({'success': False, 'error': '年度を指定してください'}), 400
    now, uid = get_jst_now(), session.get('user_id')
    with _fdb() as (cur, conn):
        cur.execute('SELECT COUNT(*) AS n FROM T_ann_plan_details WHERE fiscal_year = %s', (src,))
        if not cur.fetchone()['n']:
            return jsonify({'success': False, 'error': f'写し元の {src} 年度に行がありません'}), 400
        cur.execute("""INSERT IGNORE INTO T_ann_plan_details
                         (fiscal_year, mid_plan_no, annual_plan_no, detail_no, owner_group, created_at, updated_at, updated_by)
                       SELECT %s, mid_plan_no, annual_plan_no, detail_no, owner_group, %s, %s, %s
                       FROM T_ann_plan_details WHERE fiscal_year = %s""", (year, now, now, uid, src))
        added = cur.rowcount
        # 細目の性格は引き継ぐ：【同上】は計画に，対応済みは計画・進捗報告・業務実績報告に入れる
        cur.execute("""UPDATE T_ann_plan_details n JOIN T_ann_plan_details o
                           ON o.fiscal_year = %%s AND o.mid_plan_no = n.mid_plan_no
                          AND o.annual_plan_no = n.annual_plan_no AND o.detail_no = n.detail_no
                         SET n.plan_text = %%s
                       WHERE n.fiscal_year = %%s AND n.plan_text IS NULL AND TRIM(o.plan_text) = %%s""" % (),
                    (src, SAME_MARK, year, SAME_MARK))
        cur.execute("""UPDATE T_ann_plan_details n JOIN T_ann_plan_details o
                           ON o.fiscal_year = %%s AND o.mid_plan_no = n.mid_plan_no
                          AND o.annual_plan_no = n.annual_plan_no AND o.detail_no = n.detail_no
                         SET n.plan_text = %%s, n.progress_text = %%s, n.result_text = %%s
                       WHERE n.fiscal_year = %%s AND n.plan_text IS NULL AND %s""" % (DONE_SQL % 'o.plan_text'),
                    (src, DONE_TEXT, DONE_TEXT, DONE_TEXT, year))
        cur.execute("""INSERT IGNORE INTO T_ann_plan_evaluations
                         (fiscal_year, mid_plan_no, annual_plan_no, created_at, updated_at, updated_by)
                       SELECT DISTINCT %s, mid_plan_no, annual_plan_no, %s, %s, %s
                       FROM T_ann_plan_details WHERE fiscal_year = %s""", (year, now, now, uid, src))
        added_e = cur.rowcount
        conn.commit()
    return jsonify({'success': True, 'added': added, 'added_evals': added_e})


# 交点方式の列（担当部門の並び）．サイトで1本持ち，年度をまたいで使う
# （アプリのディレクトリ配下 data/ に置く．テーブルは使わない）
_COLS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'matrix_columns.json')


def _load_columns():
    try:
        with open(_COLS_FILE, encoding='utf-8') as f:
            v = json.load(f)
        return [str(x) for x in (v.get('columns') if isinstance(v, dict) else v) or [] if str(x).strip()]
    except Exception:
        return []


@mid_term_progress_bp.route('/api/manage/columns', methods=['GET', 'POST'])
@login_required
@require_upload_perm
def api_manage_columns():
    """列の並びを読む／丸ごと書き換える。"""
    if request.method == 'GET':
        return jsonify({'success': True, 'columns': _load_columns()})
    cols, seen = [], set()
    for x in (_jbody().get('columns') or []):
        x = str(x).strip()
        if x and x not in seen and len(x) <= 100:
            cols.append(x); seen.add(x)
    os.makedirs(os.path.dirname(_COLS_FILE), exist_ok=True)
    tmp = _COLS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'columns': cols, 'updated_at': _s(get_jst_now()), 'updated_by': session.get('user_id')},
                  f, ensure_ascii=False, indent=1)
    os.replace(tmp, _COLS_FILE)
    return jsonify({'success': True, 'columns': cols})
