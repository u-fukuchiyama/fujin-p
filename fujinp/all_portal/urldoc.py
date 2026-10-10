# SPDX-FileCopyrightText: 2026 Toyoaki Nishida
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This file is part of FUJIN-P.
# Copyright (C) 2026 Toyoaki Nishida
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

"""オール：URL 文書（URL を与えると HTML が返ってくる文書）

URL 文書は本文を持たず，URL を持つ．読むたびにその URL をたたいて HTML を受け取り，テキストにして渡す．
相手がためてある HTML を返すだけでも，その場で HTML を作って返すのでも，オールから見れば同じである．

・このサイトの中の URL（/ で始まるパス，またはこのサイトのホスト名の URL）は，同じ Flask アプリを内部で呼ぶ．
  呼ぶときの身元は読む人（Web はその人のセッション，Claude は接続を許可した人）なので，相手のアプリの権限がそのまま効く．
・ほかのサイトの URL は HTTP で取りに行く（ログインはしない．内部ネットワークのアドレスは断る）．
・URL の # 以降はその id の要素だけを抜き出す指定として使う（例 /mid_term_progress/view?years=2025#plan-2-12）．
・取ったテキストは all_items.body に「写し」として置く．写しはアクトの人が保存・読み直しをしたときだけ更新し，
  検索と，その場で取れなかったときの代わりに使う．
"""

import ipaddress
import logging
import re
import socket
from urllib.parse import urljoin, urlsplit

import requests
from flask import current_app, request, session

logger = logging.getLogger(__name__)

MAX_BYTES = 30 * 1024 * 1024        # PDF の年報なども読めるように
TIMEOUT = 30
MAX_REDIRECTS = 3
SELF_PREFIX = '/all_portal'          # オール自身は呼ばない（呼び出しの輪を作らない）
DROP_TAGS = ('script', 'style', 'noscript', 'template', 'svg', 'canvas', 'iframe', 'button', 'form', 'input',
             'select', 'textarea')


class FetchError(Exception):
    pass


# ── URL の区分 ──

def normalize(url):
    """登録できる URL か確かめて整える．/ で始まるパスか http(s) の URL"""
    u = (url or '').strip()
    if not u:
        raise ValueError('URL を書いてください')
    if u.startswith('/') and not u.startswith('//'):
        return u
    if u.startswith(('http://', 'https://')):
        return u
    raise ValueError('URL は / で始まるこのサイトのパスか，http:// または https:// で始めてください')


def is_internal(url, host=None):
    if url.startswith('/') and not url.startswith('//'):
        return True
    host = host or (request.host if request else '')
    return bool(host) and urlsplit(url).netloc.lower() == host.lower()


def split_fragment(url):
    if '#' in url:
        u, frag = url.split('#', 1)
        return u, frag.strip()
    return url, ''


def public_link(url):
    """人が開くためのリンク（このサイトのパスはそのまま）"""
    return url


# ── 取りに行く ──

def _session_for(who):
    """内部呼び出しのセッション．Web は今のセッションの写し，Claude は users から作る"""
    if who.via == 'web':
        return {k: v for k, v in session.items() if not k.endswith('csrf_token')}
    if not who.uid:
        return {}
    from .core import user_info
    u = user_info(who.uid)
    return {'user_id': who.uid, 'user_category': u['category'], 'user_name': u['name'],
            'full_name': u['name'], 'email': u['email'], 'logged_in': True}


def _dispatch(app, target, host, sess):
    """同じアプリの中でそのパスを処理させて応答を返す．
    クッキーは使わず，内部のリクエストのセッションに身元を直接入れる（ドメインやクッキーの設定に左右されない）"""
    with app.test_request_context(target, base_url='https://' + host,
                                  headers={'X-FUJINP-Internal': 'all_portal'}) as ctx:
        if sess:
            ctx.session.update(sess)
        resp = app.full_dispatch_request()
        resp.direct_passthrough = False
        return resp


def fetch_internal(path, who):
    """同じアプリを内部で呼んで (HTML, 最後の URL) を返す"""
    app = current_app._get_current_object()
    host = request.host if request else 'localhost'
    sess = _session_for(who)
    url = path
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.netloc and parts.netloc.lower() != host.lower():
            return fetch_external(url)
        target = parts.path + ('?' + parts.query if parts.query else '')
        if target.startswith(SELF_PREFIX):
            raise FetchError('オール自身の URL は URL 文書にできません')
        r = _dispatch(app, target, host, sess)
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get('Location') or ''
            if '/login' in loc:
                raise FetchError('この URL を読む権限がありません（ログイン画面へ転送されました）')
            url = urljoin('https://' + host + target, loc)
            continue
        if r.status_code != 200:
            raise FetchError(f'この URL は読めませんでした（{r.status_code}）')
        ctype = r.headers.get('Content-Type', '')
        data = r.get_data()
        if len(data) > MAX_BYTES:
            raise FetchError('大きすぎます')
        return data, ctype, r.mimetype_params.get('charset'), 'https://' + host + target
    raise FetchError('転送が多すぎます')


def _public_host(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return bool(infos)


def fetch_external(url):
    """ほかのサイトを HTTP で取りに行く（ログインなし）"""
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            raise FetchError('http か https の URL だけを読めます')
        if not _public_host(parts.hostname):
            raise FetchError('内部ネットワークのアドレスは読めません')
        try:
            r = requests.get(url, timeout=TIMEOUT, stream=True, allow_redirects=False,
                             headers={'User-Agent': 'FUJIN-P all_portal'})
        except requests.RequestException as e:
            raise FetchError(f'取りに行けませんでした（{e.__class__.__name__}）')
        if r.is_redirect:
            url = urljoin(url, r.headers.get('Location') or '')
            r.close()
            continue
        if r.status_code != 200:
            raise FetchError(f'この URL は読めませんでした（{r.status_code}）')
        ctype = r.headers.get('Content-Type', '')
        chunks, total = [], 0
        for ch in r.iter_content(65536):
            total += len(ch)
            if total > MAX_BYTES:
                raise FetchError('大きすぎます')
            chunks.append(ch)
        cs = requests.utils.get_encoding_from_headers(r.headers) if 'charset' in ctype.lower() else None
        return b''.join(chunks), ctype, cs, url
    raise FetchError('転送が多すぎます')


def fetch_raw(url, who):
    """(データ, Content-Type, 文字コード, 最後の URL)"""
    base, _ = split_fragment(url)
    if is_internal(base):
        return fetch_internal(base, who)
    return fetch_external(base)


def _decode(data, charset):
    if charset:
        return data.decode(charset, errors='replace')
    head = data[:4096].decode('ascii', errors='ignore')
    m = re.search(r'charset=["\']?([A-Za-z0-9_-]+)', head)
    try:
        return data.decode(m.group(1) if m else 'utf-8', errors='replace')
    except LookupError:
        return data.decode('utf-8', errors='replace')


def _kind(data, ctype, url):
    c = (ctype or '').lower()
    if 'pdf' in c or data[:5] == b'%PDF-' or urlsplit(url).path.lower().endswith('.pdf'):
        return 'pdf'
    if 'html' in c or 'xml' in c or not c:
        return 'html'
    if c.startswith('text/'):
        return 'text'
    return 'other'


def fetch_html(url, who):
    """HTML を (文字列, 最後の URL) で返す．HTML でなければ FetchError"""
    data, ctype, cs, final = fetch_raw(url, who)
    if _kind(data, ctype, final) != 'html':
        raise FetchError(f'HTML ではありません（{ctype or "形式不明"}）')
    return _decode(data, cs), final


def pdf_to_text(data):
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            raise FetchError('PDF を読む部品（pypdf）がありません')
    import io
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for i, pg in enumerate(reader.pages, 1):
            t = (pg.extract_text() or '').strip()
            if t:
                pages.append(f'［{i}ページ］\n{t}')
    except Exception as e:
        raise FetchError(f'PDF を読めませんでした（{e.__class__.__name__}）')
    if not pages:
        raise FetchError('PDF から文字を取り出せませんでした（画像だけの PDF かもしれません）')
    return '\n\n'.join(pages)


def extract_links(html, base_url, fragment=''):
    """ページの中のリンクを [{'title', 'url'}] で返す（重複・ページ内リンク・javascript などは除く）"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or '', 'html.parser')
    root = soup.find(id=fragment) if fragment else soup
    root = root or soup
    for t in root.find_all(['script', 'style', 'noscript', 'template']):
        t.decompose()
    out, seen = [], set()
    base_noflag = base_url.split('#', 1)[0]
    for a in root.find_all('a', href=True):
        href = a['href'].strip()
        if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:', 'data:')):
            continue
        u = urljoin(base_url, href)
        if not u.startswith(('http://', 'https://')):
            continue
        key = u.split('#', 1)[0]
        if key == base_noflag or key in seen:
            continue
        seen.add(key)
        title = re.sub(r'\s+', ' ', a.get_text(' ', strip=True)) or a.get('title') or ''
        if not title:
            img = a.find('img', alt=True)
            title = img['alt'].strip() if img else ''
        out.append({'title': title[:300] or u.rsplit('/', 1)[-1] or u, 'url': u})
    return out


# ── HTML をテキストに ──

def _cell(el):
    return re.sub(r'\s+', ' ', el.get_text(' ', strip=True))


def html_to_text(html, fragment=''):
    """見出し・段落・表・箇条を保ってテキストにする．fragment があればその id の要素だけ"""
    from bs4 import BeautifulSoup, NavigableString
    soup = BeautifulSoup(html or '', 'html.parser')
    root = soup
    if fragment:
        root = soup.find(id=fragment)
        if root is None:
            raise FetchError(f'#{fragment} の部分が見つかりません')
    for t in root.find_all(DROP_TAGS):
        t.decompose()
    for t in root.find_all(attrs={'hidden': True}):
        t.decompose()
    title = ''
    if not fragment and soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not fragment:
        root = soup.body or soup
        for t in root.find_all(['head', 'title', 'meta', 'link']):
            t.decompose()

    out = []

    def emit(s):
        s = s.strip()
        if s:
            out.append(s)

    def walk(el):
        for ch in el.children:
            if isinstance(ch, NavigableString):
                if ch.strip():
                    emit(re.sub(r'\s+', ' ', str(ch)))
                continue
            name = ch.name
            if name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
                out.append('')
                emit('#' * int(name[1]) + ' ' + _cell(ch))
            elif name == 'table':
                rows = []
                for tr in ch.find_all('tr'):
                    cells = [_cell(c) for c in tr.find_all(['th', 'td'], recursive=False)]
                    if any(cells):
                        rows.append('| ' + ' | '.join(cells) + ' |')
                if rows:
                    out.append('')
                    out.extend(rows)
                    out.append('')
            elif name == 'li':
                emit('- ' + _cell(ch))
            elif name == 'br':
                out.append('')
            elif name in ('pre',):
                emit(ch.get_text())
            elif name in ('p', 'div', 'section', 'article', 'header', 'footer', 'main', 'blockquote', 'dd', 'dt',
                          'tr', 'ul', 'ol', 'dl', 'nav', 'aside', 'details', 'summary', 'figure', 'figcaption'):
                if ch.find(['p', 'div', 'section', 'article', 'table', 'ul', 'ol', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                            'li', 'dl', 'pre', 'blockquote']):
                    walk(ch)
                else:
                    emit(_cell(ch))
            else:
                walk(ch)

    walk(root)
    text = '\n'.join(out)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return (('# ' + title + '\n\n') if title else '') + text


def read_text(url, who):
    """URL をたたいてテキストを返す（HTML・PDF・テキスト）"""
    return read_text_final(url, who)[0]


def read_text_final(url, who):
    """(テキスト, 転送を追ったあとの最後の URL)"""
    data, ctype, cs, final = fetch_raw(url, who)
    return _to_text(url, data, ctype, cs, final), final


def _to_text(url, data, ctype, cs, final):
    kind = _kind(data, ctype, final)
    if kind == 'pdf':
        return pdf_to_text(data)
    if kind == 'text':
        return _decode(data, cs)
    if kind != 'html':
        raise FetchError(f'読める形式ではありません（{ctype or "形式不明"}）')
    _, frag = split_fragment(url)
    return html_to_text(_decode(data, cs), frag)
