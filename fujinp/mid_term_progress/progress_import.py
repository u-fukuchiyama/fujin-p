"""
進捗状況報告の取り込み（緊急用）

学外システムから書き出した「{年度}年度 進捗状況報告」の xlsx の「報告内容」列（K列）を，
T_ann_plan_details.progress_text（進捗状況報告の正本）へ差し込む。

- 行の対応：年度計画番号（G列）と年度計画細目番号（H列）で，指定年度の細目の行に当てる。
  同じ年度計画の中で細目番号が重なっている（例：1-1 が2行）ときは，
  行数がこのサイトの細目の数と同じなら上から順に細目番号順の細目へ当て，
  合わないときはその細目番号の行に本文をつないで入れる。
- 執筆者の進捗状況報告（T_ann_plan_requests.author_text，stage='progress'）が入っている細目は，
  それも同じ本文で上書きする（依頼の行が無い細目には行を作らない．状態の列は触らない）。
- 冪等：同じファイルを何度取り込んでも結果は同じ。本文が今と同じ行は書かない（変更なし）。
- 報告内容が空の行は読み飛ばす（既存の本文を消さない）。
- 対応済みの細目は触らない。このサイトに無い細目は追加せず，結果に出すだけ。
"""
import logging
import re

from flask import request, jsonify, session

from . import mid_term_progress_bp
from .routes import (login_required, require_upload_perm, _fdb, get_jst_now, safe_int,
                     detect_year, is_done, OPENPYXL_AVAILABLE)

try:
    import openpyxl
except ImportError:
    openpyxl = None

# 見出しの名前（空白・改行を除いて比べる）→ 既定の列位置（0始まり．G=6, H=7, K=10, E=4）
HEAD_ANN = ('年度計画番号', 6)
HEAD_DETAIL = ('年度計画細目番号', 7)
HEAD_TEXT = ('報告内容', 10)
HEAD_MID = ('中期計画番号', 4)


def _norm(v):
    return re.sub(r'\s+', '', str(v)) if v is not None else ''


def _text(v):
    if v is None:
        return ''
    s = str(v).replace('\r\n', '\n').replace('\r', '\n')
    return s.strip()


def _read_xlsx(f):
    """xlsx の先頭シートから (行番号, 中期計画番号, 年度計画番号, 細目番号, 本文) の並びを読む。"""
    wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError('シートが空です')
    # 見出し行を探す（先頭10行のうち「報告内容」を含む行）．無ければ1行目を見出しとし，既定の列位置を使う
    hi, idx = 0, {}
    for i, r in enumerate(rows[:10]):
        names = [_norm(v) for v in r]
        if HEAD_TEXT[0] in names and HEAD_ANN[0] in names:
            hi = i
            idx = {n: names.index(n) for n in (HEAD_ANN[0], HEAD_DETAIL[0], HEAD_TEXT[0], HEAD_MID[0]) if n in names}
            break
    c_ann = idx.get(HEAD_ANN[0], HEAD_ANN[1])
    c_det = idx.get(HEAD_DETAIL[0], HEAD_DETAIL[1])
    c_txt = idx.get(HEAD_TEXT[0], HEAD_TEXT[1])
    c_mid = idx.get(HEAD_MID[0], HEAD_MID[1])
    out = []
    for n, r in enumerate(rows[hi + 1:], start=hi + 2):
        if r is None:
            continue
        get = (lambda c: r[c] if c < len(r) else None)
        ap, dn = safe_int(get(c_ann)), safe_int(get(c_det))
        if ap is None:
            continue
        out.append({'line': n, 'mid': safe_int(get(c_mid)), 'ap': ap, 'dn': dn if dn is not None else 1,
                    'text': _text(get(c_txt))})
    return ws.title, out


def _plan(cur, year, xrows):
    """取り込みの計画を作る：細目の行ごとに，入れる本文と処置。"""
    cur.execute('SELECT id, mid_plan_no, annual_plan_no, detail_no, plan_text, progress_text '
                'FROM T_ann_plan_details WHERE fiscal_year = %s ORDER BY annual_plan_no, detail_no', (year,))
    db = {}
    for r in cur.fetchall():
        db.setdefault(r['annual_plan_no'], []).append(r)

    by_plan = {}
    for x in xrows:
        by_plan.setdefault(x['ap'], []).append(x)

    targets = []   # (ap, dn, text, how, lines, mid)
    for ap in sorted(by_plan):
        xs = by_plan[ap]
        dns = [x['dn'] for x in xs]
        if len(set(dns)) == len(dns):
            for x in xs:
                targets.append((ap, x['dn'], x['text'], '', [x['line']], x['mid']))
        elif len(xs) == len(db.get(ap, [])):
            for x, d in zip(xs, db[ap]):
                targets.append((ap, d['detail_no'], x['text'], '細目番号が重なるので上から順に当てた',
                                [x['line']], x['mid']))
        else:
            groups = {}
            for x in xs:
                groups.setdefault(x['dn'], []).append(x)
            for dn, g in groups.items():
                text = '\n\n'.join(x['text'] for x in g if x['text'])
                how = '同じ細目番号の %d 行をつないだ' % len(g) if len(g) > 1 else ''
                targets.append((ap, dn, text, how, [x['line'] for x in g], g[0]['mid']))

    dbi = {(r['annual_plan_no'], r['detail_no']): r for rs in db.values() for r in rs}
    # 執筆者の進捗状況報告（T_ann_plan_requests.author_text）．あれば同じ本文で上書きする
    reqs = {}
    try:
        cur.execute("SHOW COLUMNS FROM T_ann_plan_requests LIKE 'author_text'")
        if cur.fetchall():
            cur.execute("SELECT id, annual_plan_no, detail_no, author_text FROM T_ann_plan_requests "
                        "WHERE fiscal_year = %s AND stage = 'progress' AND author_text IS NOT NULL "
                        "AND TRIM(author_text) <> ''", (year,))
            reqs = {(r['annual_plan_no'], r['detail_no']): r for r in cur.fetchall()}
    except Exception:
        reqs = {}
    items = []
    for ap, dn, text, how, lines, mid in targets:
        d = dbi.get((ap, dn))
        it = {'no': '%d-%d' % (ap, dn), 'lines': lines, 'how': how, 'chars': len(text)}
        if not text:
            it['action'] = 'skip_empty'
        elif d is None:
            it['action'] = 'missing'
        elif is_done(d.get('plan_text')):
            it['action'] = 'skip_done'
        elif (d.get('progress_text') or '').replace('\r\n', '\n').strip() == text:
            it['action'] = 'same'
        else:
            it['action'] = 'update' if not (d.get('progress_text') or '').strip() else 'overwrite'
            it['id'] = d['id']
            it['text'] = text
        q = reqs.get((ap, dn))
        if it['action'] in ('update', 'overwrite', 'same') and q is not None \
                and (q.get('author_text') or '').replace('\r\n', '\n').strip() != text:
            it['req_id'] = q['id']
            it['text'] = text
            it['author'] = True
        if d is not None and mid is not None and d['mid_plan_no'] != mid:
            it['how'] = (it['how'] + '．' if it['how'] else '') + '中期計画番号がファイル %d／サイト %d' % (mid, d['mid_plan_no'])
        items.append(it)
    return items


LABEL = {'update': '新たに入れる', 'overwrite': '書き換える', 'same': '変更なし',
         'skip_empty': '空欄なので読み飛ばす', 'skip_done': '対応済みの細目なので触らない',
         'missing': 'このサイトに細目が無い'}


@mid_term_progress_bp.route('/api/import_progress', methods=['POST'])
@login_required
@require_upload_perm
def api_import_progress():
    """dry=1 なら点検だけ，dry=0 なら差し込む。"""
    try:
        if not OPENPYXL_AVAILABLE or openpyxl is None:
            return jsonify({'success': False, 'error': 'openpyxl がインストールされていません'}), 500
        f = request.files.get('file')
        if not f or not f.filename:
            return jsonify({'success': False, 'error': 'ファイルが必要です'}), 400
        dry = request.form.get('dry') != '0'
        year = safe_int(request.form.get('year'))
        title, xrows = _read_xlsx(f)
        if year is None:
            year = detect_year(f.filename, title)
        if year is None:
            return jsonify({'success': False, 'error': '年度を入れてください'}), 400
        if not xrows:
            return jsonify({'success': False, 'error': '年度計画番号の入った行がありません'}), 400
        with _fdb() as (cur, conn):
            items = _plan(cur, year, xrows)
            if not items:
                return jsonify({'success': False, 'error': '取り込む行がありません'}), 400
            done = done_a = 0
            if not dry:
                try:
                    now, uid = get_jst_now(), session.get('user_id')
                    for it in items:
                        if it['action'] in ('update', 'overwrite'):
                            cur.execute('UPDATE T_ann_plan_details SET progress_text = %s, updated_at = %s, '
                                        'updated_by = %s WHERE id = %s', (it['text'], now, uid, it['id']))
                            done += 1
                        if it.get('req_id'):
                            cur.execute('UPDATE T_ann_plan_requests SET author_text = %s, author_text_at = %s, '
                                        'author_text_by = %s WHERE id = %s', (it['text'], now, uid, it['req_id']))
                            done_a += 1
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        counts = {}
        for it in items:
            counts[it['action']] = counts.get(it['action'], 0) + 1
            it['label'] = LABEL[it['action']]
            if it.get('author'):
                counts['author'] = counts.get('author', 0) + 1
                it['label'] += '／執筆者の報告を上書き'
            for k in ('text', 'id', 'req_id'):
                it.pop(k, None)
        return jsonify({'success': True, 'dry': dry, 'year': year, 'file_rows': len(xrows),
                        'counts': counts, 'written': done, 'written_author': done_a, 'items': items})
    except Exception as e:
        logging.error('import_progress error: %s', e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
