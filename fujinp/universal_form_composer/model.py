"""ゆにこん — プロジェクトの中身（こんかの部品群）を読むための道具。

部品群の形はこんかと同じ：quanta（作品・複合部品・倉庫部品・箇条）と links（箇条の並び）。
ここは読むだけで，書き換えはしない。閲覧はサーバ側で HTML に組み立てる。
"""

import json
import re

from markupsafe import Markup, escape

WORK, PART = 'work', 'part'
LEVEL_LABEL = {'work': '作品', 'part': '複合部品', 'box': '倉庫部品', 'item': '箇条'}
MAX_DEPTH = 6

_TAG_SCRIPT = re.compile(r'<\s*(script|iframe|object|embed)\b.*?<\s*/\s*\1\s*>', re.I | re.S)
_TAG_OPEN = re.compile(r'<\s*(script|iframe|object|embed|link|meta)\b[^>]*>', re.I)
_ON_ATTR = re.compile(r'\son[a-z]+\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', re.I)
_JS_URL = re.compile(r'(href|src)\s*=\s*("|\')\s*javascript:[^"\']*\2', re.I)


def clean_html(raw):
    """書かれた HTML から，動くものを落とす（こんかと同じ規則）。"""
    h = raw or ''
    h = _TAG_SCRIPT.sub('', h)
    h = _TAG_OPEN.sub('', h)
    h = _ON_ATTR.sub('', h)
    h = _JS_URL.sub('', h)
    return h


def _loads(raw):
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or '{}')
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


class Project:
    def __init__(self, data):
        parts = (data or {}).get('parts') or {}
        self.q = {}
        for k, v in (parts.get('quanta') or {}).items():
            try:
                self.q[int(k)] = v
            except (TypeError, ValueError):
                continue
        self.bykey = {q['key_path']: q for q in self.q.values() if q.get('key_path')}
        self.kids = {}
        for ln in parts.get('links') or []:
            self.kids.setdefault(int(ln['parent']), []).append(ln)
        for v in self.kids.values():
            v.sort(key=lambda x: (x.get('ord', 0), x.get('child', 0)))

    # ------------------------------------------------------------ 引く
    def get(self, qid):
        try:
            return self.q.get(int(qid))
        except (TypeError, ValueError):
            return None

    def resolve(self, ref):
        """枠の指し先（ID か key_path）。"""
        if ref is None or ref == '':
            return None
        s = str(ref).strip()
        return self.get(s) if s.isdigit() else self.bykey.get(s)

    def attrs(self, q):
        return _loads((q or {}).get('attrs'))

    def level_of(self, q):
        if not q:
            return None
        if q.get('kind') == 'item':
            return 'item'
        if q.get('recipe') == 'arena':
            return WORK if self.attrs(q).get('level') == WORK else PART
        return 'box'

    def layout(self, q):
        lay = _loads((q or {}).get('body'))
        lay.setdefault('root', {'t': 'col', 'align': 'top', 'items': []})
        return lay

    def children(self, pid):
        out = []
        for ln in self.kids.get(int(pid), []):
            c = self.q.get(int(ln['child']))
            if c:
                out.append(c)
        return out

    def listing(self, level):
        rows = [q for q in self.q.values() if self.level_of(q) == level]
        rows.sort(key=lambda q: (q.get('updated_at') or ''), reverse=True)
        return rows

    # ------------------------------------------------------------ 描く
    def render(self, arena):
        """作品（または複合部品）を HTML にする。"""
        lay = self.layout(arena)
        show = bool(lay.get('show_labels'))
        width = ((lay.get('view') or {}).get('s')) or None
        inner = self._node(lay.get('root'), None, 0, (arena['id'],), show)
        # 幅は表示枠に合わせて伸縮する。こんかの設計幅 s は「これより狭くはしない」下限の目安に使う
        # （狭い画面では横スクロールで読む）。下限は 720px を超えない。
        mw = min(int(width), 720) if isinstance(width, (int, float)) and width > 0 else 480
        return Markup('<div class="stage" style="min-width:%dpx">%s</div>' % (mw, inner))

    @staticmethod
    def _flex(node, parent_t):
        z = node.get('size') or {}
        if parent_t == 'row' and z.get('w'):
            return ' style="flex:%s 1 0"' % float(z['w'])
        return ''

    def _node(self, node, parent_t, depth, seen, show):
        if not isinstance(node, dict):
            return ''
        t = node.get('t')
        st = self._flex(node, parent_t)
        if t in ('row', 'col'):
            al = node.get('align') or ('left' if t == 'row' else 'top')
            inner = ''.join(self._node(c, t, depth, seen, show) for c in node.get('items') or [])
            return '<div class="%s a-%s"%s>%s</div>' % (t, escape(al), st, inner)
        ref = node.get('ref')
        q = self.resolve(ref)
        if not q:
            return '<div class="tb ng"%s><span class="miss">部品が見つかりません（%s）</span></div>' % (
                st, escape(ref if ref not in (None, '') else '未指定'))
        label = ''
        if show and not node.get('hide_label'):
            label = '<div class="lb">%s</div>' % escape(node.get('label') or q.get('title') or q.get('key_path') or '')
        tip = escape('#%s %s' % (q['id'], q.get('key_path') or q.get('title') or ''))
        if q.get('recipe') == 'arena':
            if depth >= MAX_DEPTH or q['id'] in seen:
                return '<div class="tb ng"%s><span class="miss">埋め込みが深すぎるか循環しています（#%s）</span></div>' % (st, q['id'])
            lay = self.layout(q)
            inner = self._node(lay.get('root'), None, depth + 1, seen + (q['id'],), show)
            return '<div class="emb" data-q="%s" title="%s"%s>%s%s</div>' % (q['id'], tip, st, label, inner)
        return '<div class="tb" data-q="%s" title="%s"%s>%s%s</div>' % (q['id'], tip, st, label, self._content(q))

    def _content(self, q):
        if q.get('kind') == 'item':
            return self._lines([q.get('body') or ''])
        r = q.get('recipe') or ''
        if r == 'text':
            h = clean_html(q.get('body') or '').strip()
            return '<div class="rich">%s</div>' % h if h else '<span class="empty">未記入</span>'
        if r == 'record':
            out = []
            for k in self.children(q['id']):
                lb = escape(self.attrs(k).get('label') or '')
                if k.get('kind') == 'item':
                    body = self._lines([k.get('body') or ''])
                else:
                    body = self._content(k)
                out.append('<div class="fld"><span class="fl">%s</span>%s</div>' % (lb, body))
            return ''.join(out) or '<span class="empty">欄がありません</span>'
        if r == 'count':
            return '<div class="num">%d</div>' % len(self.children(q['id']))
        # seq と，作法の分からない箱は箇条を順に並べる
        lines = []
        for k in self.children(q['id']):
            if k.get('kind') == 'item':
                lines.append(k.get('body') or '')
            else:
                lines.append(re.sub(r'<[^>]+>', '', k.get('body') or ''))
        return self._lines(lines)

    @staticmethod
    def _lines(lines):
        lines = [x.strip() for x in lines if (x or '').strip()]
        if not lines:
            return '<span class="empty">未記入</span>'
        return '<ul>%s</ul>' % ''.join('<li>%s</li>' % escape(x) for x in lines)
