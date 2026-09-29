"""ゆにこん — 部品と常設テーブルのあいだで中身を運ぶ。

  to_table(data)    部品の中身 → T_ann_／T_mid_ の4表（nishida$fujinp．対応付けのある欄だけ書く）
  from_table(data)  テーブルの中身 → 部品（プロジェクトの JSON を書き換える）

文字の扱い：
  ・テーブルの本文では，箇条1つを「・」で始まるひとかたまりとして書く。箇条の中の改行は
    そのまま続きの行になる（「・」で始まらない行は直前の箇条の続き）。
  ・型テキストの箱は HTML を行に割った素のテキスト（「・」は付けない）。
  ・欄に部品が複数あるときは，k 番目のかたまりが k 番目の部品に対応する。
  ・テーブル側で「・」を使わずに書かれた本文は，1行を1かたまりとして読む。
  ・自己評価・委員会評価（1〜4）は数字だけを拾い，それ以外は空にする。
正本はテーブル。部品へ書くときは，テーブルの欄が空（NULL・空文字）でも，行そのものが無くても，
そのとおり部品を空にする（箇条は空の箇条として残し，対応付けの枠は保つ）。
テーブルへ書くときは対応付けで部品が指定された欄だけを書き，他の欄（テーブル側で書かれたメモなど）は触らない。
"""

import html as _html
import json
import re

from . import mapping as mp
from . import store

INT_COLS = {'self_eval', 'committee_eval'}
TABLE = {'details': 'T_ann_plan_details', 'plans': 'T_ann_plan_evaluations',
         'mid_details': 'T_mid_plan_details', 'mid_evals': 'T_mid_plan_evaluations'}
TABLE_DB = 'fujinp'          # 常設テーブルの置き場（nishida$fujinp）。db.get_db_cursor(database=...) に渡す名前
REQUIRED_VALS = {'mid_details': ('mid_goal_no',)}   # テーブル側で NOT NULL の値の列


# ------------------------------------------------------------------ 文字の変換

def html_to_lines(h):
    h = re.sub(r'(?i)<\s*br\s*/?>', '\n', h or '')
    h = re.sub(r'(?i)</\s*(p|div|li|h[1-6]|tr)\s*>', '\n', h)
    h = re.sub(r'<[^>]+>', '', h)
    return clean_lines(_html.unescape(h))


def clean_lines(text):
    """素のテキストを行に割る（空行を捨てる）。"""
    return [ln.strip() for ln in (text or '').replace('\r', '').split('\n') if ln.strip()]


BULLET = re.compile(r'^\s*(・|-\s+|\*\s+)')


def split_units(text):
    """テーブルの本文をかたまり（箇条1つ分）に割る。"""
    lines = [ln.rstrip() for ln in (text or '').replace('\r', '').split('\n')]
    if not any(BULLET.match(ln) for ln in lines):
        return clean_lines(text)
    units, cur = [], None
    for ln in lines:
        if BULLET.match(ln):
            if cur is not None:
                units.append(cur)
            cur = BULLET.sub('', ln).strip()
        elif ln.strip():
            cur = ln.strip() if cur is None else cur + '\n' + ln.strip()
    if cur is not None:
        units.append(cur)
    return [u for u in (x.strip() for x in units) if u]


def norm(body):
    return '\n'.join(clean_lines(body))


def lines_to_html(lines):
    return ''.join('<p>%s</p>' % _html.escape(x, quote=False) for x in lines)


def eval_value(lines):
    s = ''.join(lines)
    s = s.translate(str.maketrans('０１２３４５６７８９', '0123456789'))
    m = re.search(r'[1-4]', s)
    return int(m.group(0)) if m and re.fullmatch(r'\s*[1-4]\s*', s) else None


# ------------------------------------------------------------------ プロジェクトの JSON を直接扱う道具

class Parts:
    def __init__(self, data):
        self.data = data
        parts = data.setdefault('parts', {})
        parts.setdefault('quanta', {}); parts.setdefault('links', [])
        self.q = parts['quanta']                  # 文字列キー → 量子
        self.links = parts['links']
        self.bykey = {v['key_path']: v for v in self.q.values() if v.get('key_path')}

    def get(self, ref):
        s = str(ref).strip()
        return self.q.get(s) if s.isdigit() else self.bykey.get(s)

    def kids(self, pid):
        ls = sorted([ln for ln in self.links if int(ln['parent']) == int(pid)],
                    key=lambda x: (x.get('ord', 0), x.get('child', 0)))
        return ls

    def item_kids(self, pid):
        return [self.q[str(ln['child'])] for ln in self.kids(pid)
                if str(ln['child']) in self.q and self.q[str(ln['child'])].get('kind') == 'item']

    def parent_of(self, cid):
        for ln in self.links:
            if int(ln['child']) == int(cid):
                return int(ln['parent'])
        return None

    def new_item(self, body):
        parts = self.data['parts']
        nid = int(parts.get('next_id') or 1)
        used = max([int(k) for k in self.q] or [0]) + 1
        nid = max(nid, used)
        parts['next_id'] = nid + 1
        ts = store._now().strftime('%Y-%m-%d %H:%M:%S')
        q = {'id': nid, 'kind': 'item', 'key_path': None, 'title': None, 'body': body, 'recipe': None,
             'attrs': {'origin': 'unicon'}, 'owner_id': None, 'created_at': ts, 'updated_at': ts}
        self.q[str(nid)] = q
        return q

    def reorder(self, pid, child_ids):
        """親の並びを child_ids の順に作り直す（並びに無い子は外す）。"""
        self.links[:] = [ln for ln in self.links if int(ln['parent']) != int(pid)]
        for i, c in enumerate(child_ids, 1):
            self.links.append({'parent': int(pid), 'child': int(c), 'ord': i})

    @staticmethod
    def touch(q):
        q['updated_at'] = store._now().strftime('%Y-%m-%d %H:%M:%S')

    # 読む
    def units_of(self, q):
        """部品の中身を (かたまり, 箇条か) の並びにする。"""
        if q.get('kind') == 'item':
            b = norm(q.get('body') or '')
            return [(b, True)] if b else []
        if q.get('recipe') == 'arena':
            return []
        if q.get('recipe') == 'text':
            return [(ln, False) for ln in html_to_lines(q.get('body') or '')]
        return [(norm(k.get('body') or ''), True) for k in self.item_kids(q['id']) if norm(k.get('body') or '')]


# ------------------------------------------------------------------ 部品 → テーブル

def _key_ok(r, keys):
    return all(mp.key_ok(r.get(k), k) for k, _ in keys)


def _fetch(cur, kind, rows, kcols):
    """対応付けの行に関係するテーブルの行を，先頭のキー（年度／期）で絞って読む。"""
    first = kcols[0]
    vals = sorted({r[first] for r in rows})
    out = {}
    if not vals:
        return out
    cur.execute('SELECT * FROM %s WHERE %s IN (%s)' % (TABLE[kind], first, ','.join(['%s'] * len(vals))), tuple(vals))
    for e in cur.fetchall():
        out[tuple(e[k] for k in kcols)] = e
    return out


def to_table(data, actor_id):
    """部品の中身をテーブルへ書く。返り値は報告。"""
    pts = Parts(data)
    m = mp.get(data)
    rep = {'inserted': 0, 'updated': 0, 'same': 0, 'skipped': [], 'notes': []}
    now = store._now()
    with store._db(TABLE_DB) as (cur, conn):
        for kind in mp.KINDS:
            keys, vals, cells = mp.spec(kind)
            kcols = [k for k, _ in keys]
            rows = [r for r in (m.get(kind) or []) if _key_ok(r, keys)]
            bad = len(m.get(kind) or []) - len(rows)
            if bad:
                rep['skipped'].append('%s：キーが整っていない %d 行は飛ばしました' % (mp.SHEETS[kind], bad))
            existing = _fetch(cur, kind, rows, kcols)
            for r in rows:
                key = tuple(r[k] for k in kcols)
                vals_out = {}
                for k, lb in cells:
                    ids = (r.get('cells') or {}).get(k) or []
                    if not ids:
                        continue
                    units = []
                    for ref in ids:
                        q = pts.get(ref)
                        if not q:
                            rep['notes'].append('%s %s・%s：部品 %s が見つかりません' % (mp.SHEETS[kind], '-'.join(map(str, key)), lb, ref))
                            continue
                        units.extend(pts.units_of(q))
                    if k in INT_COLS:
                        raw = [u for u, _ in units]
                        v = eval_value(raw)
                        if raw and v is None:
                            rep['notes'].append('%s %s・%s：「%s」は1〜4ではないので空にしました'
                                                % (mp.SHEETS[kind], '-'.join(map(str, key)), lb, ''.join(raw)[:10]))
                        vals_out[k] = v
                    else:
                        vals_out[k] = '\n'.join(('・' + u) if is_item else u for u, is_item in units)
                for k, _ in vals:
                    v = r.get(k)
                    if k in mp.INT_VALS:
                        if isinstance(v, int):
                            vals_out[k] = v
                    elif v:
                        vals_out[k] = v
                e = existing.get(key)
                if e is None:
                    lack = [k for k in REQUIRED_VALS.get(kind, ()) if k not in vals_out]
                    if lack:
                        rep['skipped'].append('%s %s：%s が無いので行を作れませんでした'
                                              % (mp.SHEETS[kind], '-'.join(map(str, key)), '・'.join(lack)))
                        continue
                    cols = kcols + list(vals_out) + ['created_at', 'updated_at', 'updated_by']
                    args = list(key) + list(vals_out.values()) + [now, now, actor_id]
                    cur.execute('INSERT INTO %s (%s) VALUES (%s)' % (TABLE[kind], ','.join(cols), ','.join(['%s'] * len(cols))),
                                tuple(args))
                    rep['inserted'] += 1
                    continue
                diff = {k: v for k, v in vals_out.items() if (e.get(k) if e.get(k) is not None else None) != v
                        and not (e.get(k) in (None, '') and v in (None, ''))}
                if not diff:
                    rep['same'] += 1
                    continue
                sets = ','.join('%s=%%s' % k for k in diff) + ',updated_at=%s,updated_by=%s'
                cur.execute('UPDATE %s SET %s WHERE id=%%s' % (TABLE[kind], sets),
                            tuple(diff.values()) + (now, actor_id, e['id']))
                rep['updated'] += 1
        conn.commit()
    return rep


# ------------------------------------------------------------------ テーブル → 部品

def from_table(data):
    """テーブルの中身を部品へ書く。data（プロジェクトの JSON）をその場で書き換え，報告を返す。"""
    pts = Parts(data)
    m = mp.get(data)
    rep = {'changed': 0, 'same': 0, 'cleared': 0, 'added_items': 0, 'removed_items': 0, 'empty': 0,
           'missing_rows': 0, 'conflicts': [], 'notes': []}
    written = {}          # 部品ID → この回に書いた行（同じ部品を指す別の行との食い違いを見る）
    shared = {}           # 部品ID → 対応付けの中で指されている回数（共有の箇条は外さない）
    for kd in mp.KINDS:
        for r in m.get(kd) or []:
            for ids in (r.get('cells') or {}).values():
                for x in ids or []:
                    shared[str(x)] = shared.get(str(x), 0) + 1
    mapping_changed = False
    with store._db(TABLE_DB) as (cur, conn):
        for kind in mp.KINDS:
            keys, vals, cells = mp.spec(kind)
            kcols = [k for k, _ in keys]
            rows = [r for r in (m.get(kind) or []) if _key_ok(r, keys)]
            table = _fetch(cur, kind, rows, kcols)
            for r in rows:
                key = tuple(r[k] for k in kcols)
                name = '%s %s' % (mp.SHEETS[kind], '-'.join(map(str, key)))
                e = table.get(key) or {}
                if not e:
                    rep['missing_rows'] += 1
                for k, lb in cells:
                    ids = list((r.get('cells') or {}).get(k) or [])
                    if not ids:
                        continue
                    v = e.get(k)
                    if v is None or v == '':
                        rep['empty'] += 1
                        _clear_cell(pts, ids, name, lb, rep, written)
                        continue
                    lines = [str(v)] if k in INT_COLS else split_units(str(v))
                    new_ids = _write_cell(pts, ids, lines, name, lb, rep, written, shared)
                    if new_ids != ids:
                        r['cells'][k] = new_ids
                        mapping_changed = True
    if mapping_changed:
        m['updated_at'] = store._now().strftime('%Y-%m-%d %H:%M:%S')
        data['mapping'] = m
        rep['notes'].append('「・」の増減に合わせて対応付けのセルも書き換えました')
    return rep


def _set_part(pts, q, lines, rep):
    """部品1つに行の並びを入れる。変わったら True。"""
    if q.get('kind') == 'item':
        body = '\n'.join(lines)
        if norm(q.get('body') or '') == norm(body):
            return False
        q['body'] = body; pts.touch(q); return True
    if q.get('recipe') == 'text':
        flat = [x for u in lines for x in clean_lines(u)]
        if html_to_lines(q.get('body') or '') == flat:
            return False
        q['body'] = lines_to_html(flat); pts.touch(q); return True
    if q.get('recipe') == 'arena':
        return False
    kids = pts.item_kids(q['id'])
    if [norm(k.get('body') or '') for k in kids] == [norm(u) for u in lines]:
        return False
    order = []
    for i, ln in enumerate(lines):
        if i < len(kids):
            if norm(kids[i].get('body') or '') != norm(ln):
                kids[i]['body'] = ln; pts.touch(kids[i])
            order.append(kids[i]['id'])
        else:
            order.append(pts.new_item(ln)['id']); rep['added_items'] += 1
    rep['removed_items'] += max(0, len(kids) - len(lines))
    other = [ln['child'] for ln in pts.kids(q['id']) if pts.q.get(str(ln['child']), {}).get('kind') != 'item']
    pts.reorder(q['id'], order + other)
    pts.touch(q)
    return True


def _write_cell(pts, ids, lines, name, lb, rep, written, shared):
    """欄1つ分。部品が複数なら k 行目を k 番目へ。箇条の数と行の数が違えば箇条を足し引きする。"""
    qs = [pts.get(x) for x in ids]
    if any(q is None for q in qs):
        rep['notes'].append('%s・%s：見つからない部品があるので飛ばしました' % (name, lb))
        return ids
    # 部品が1つなら全部そこへ（箇条なら行の数だけ箇条にする）
    if len(qs) == 1 and qs[0].get('kind') != 'item':
        if _record(qs[0], lines, name, lb, rep, written):
            _cnt(rep, _set_part(pts, qs[0], lines, rep))
        return ids
    all_items = all(q.get('kind') == 'item' for q in qs)
    if all_items:
        n = min(len(qs), len(lines))
        for q, ln in zip(qs[:n], lines[:n]):
            if _record(q, [ln], name, lb, rep, written):
                _cnt(rep, _set_part(pts, q, [ln], rep))
        out = list(ids)
        if len(lines) > len(qs):                      # 行が多い：最後の箇条のうしろに箇条を足す
            last = qs[-1]; pid = pts.parent_of(last['id'])
            if pid is None:
                rep['notes'].append('%s・%s：行が %d 多いのですが，親の箱が分からず足せませんでした' % (name, lb, len(lines) - len(qs)))
                return ids
            order = [ln['child'] for ln in pts.kids(pid)]
            at = order.index(int(last['id'])) + 1
            for ln in lines[len(qs):]:
                nq = pts.new_item(ln); order.insert(at, nq['id']); at += 1
                out.append(str(nq['id'])); rep['added_items'] += 1
                written[str(nq['id'])] = (name, [ln])
            pts.reorder(pid, order); rep['changed'] += 1
        elif len(lines) < len(qs):                    # 行が少ない：余った箇条を並びから外す（共有の箇条は残す）
            for q in qs[len(lines):]:
                if shared.get(str(q['id']), 0) > 1:
                    continue
                pid = pts.parent_of(q['id'])
                if pid is not None:
                    order = [ln['child'] for ln in pts.kids(pid) if int(ln['child']) != int(q['id'])]
                    pts.reorder(pid, order)
                out.remove(str(q['id'])); rep['removed_items'] += 1; rep['changed'] += 1
        return out
    # 箱と箇条が混ざるとき：前から1行ずつ，最後の部品に残り全部
    for i, q in enumerate(qs):
        part = lines[i:i + 1] if i < len(qs) - 1 else lines[i:]
        if part and _record(q, part, name, lb, rep, written):
            _cnt(rep, _set_part(pts, q, part, rep))
    return ids


def _clear_cell(pts, ids, name, lb, rep, written):
    """テーブルの欄が空：指された部品を空にする。箇条は空の箇条として残し，並びと対応付けは保つ。"""
    for ref in ids:
        q = pts.get(ref)
        if not q or q.get('recipe') == 'arena':
            continue
        if not _record(q, [], name, lb, rep, written):
            continue
        if q.get('kind') == 'item' or q.get('recipe') == 'text':
            if norm(q.get('body') or '') or html_to_lines(q.get('body') or ''):
                q['body'] = ''; pts.touch(q); rep['cleared'] += 1
            else:
                rep['same'] += 1
            continue
        hit = False
        for k in pts.item_kids(q['id']):
            if norm(k.get('body') or ''):
                k['body'] = ''; pts.touch(k); hit = True
        rep['cleared' if hit else 'same'] += 1


def _cnt(rep, changed):
    rep['changed' if changed else 'same'] += 1
    return True


def _record(q, lines, name, lb, rep, written):
    """同じ部品を別の行がすでに書いていれば書かない。空の欄より中身のある欄を優先し，
    中身どうしが食い違うときは先の行を採って記録する。"""
    k = str(q['id'])
    if k in written:
        prev = written[k][1]
        if not lines:
            return False
        if not prev:
            written[k] = (name + '・' + lb, lines)
            return True
        if [norm(x) for x in prev] != [norm(x) for x in lines]:
            rep['conflicts'].append('#%s：%s と %s・%s で文面が食い違います（先の行を採りました）'
                                    % (k, written[k][0], name, lb))
        return False
    written[k] = (name + '・' + lb, lines)
    return True
