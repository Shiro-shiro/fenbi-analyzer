# -*- coding: utf-8 -*-
""" 登录模块：扫码登录 + 手动粘贴Cookie + 记住登录态(本机保存)
   v2.0 审计修复版：登录校验业务标志+用户身份、保存前验证、异常字符提示。 """
import os, json, time, base64, hashlib, threading
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(BASE, 'data', 'session.json')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36'

QR_GEN = 'https://ke.fenbi.com/qrcode-login/api/gen_code'
QR_QUERY = 'https://ke.fenbi.com/qrcode-login/api/query_code_status'
USER_INFO = 'https://login.fenbi.com/api/users/info'


def clean_cookie_warning(text):
    """ 检查Cookie里是否有会被静默过滤的字符；返回警告文字或None。
        （不偷偷改数据——发现异常先告诉用户）"""
    s = str(text or '')
    bad = [c for c in s if ord(c) >= 256]
    if bad:
        return '检测到%d个非ASCII字符（如中文/全角符号），已忽略这些字符；若登录失败请重新复制纯英文Cookie。' % len(bad)
    if '\n' in s or '\r' in s:
        return 'Cookie中含换行，已按空格处理；请确认复制完整。'
    return None


def _clean_cookie(text):
    """ 清洗：去换行、过滤非latin1字符（保留警告由调用方展示） """
    s = str(text or '').strip()
    s = ''.join(c for c in s if ord(c) < 256)
    s = s.replace('\r', ' ').replace('\n', ' ').strip()
    if s.lower().startswith('cookie:'):
        s = s[7:].strip()
    return s


def save_cookie(cookie_header, user_id=None, user_name=None):
    os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
    d = {'cookie': cookie_header, 'saved_at': time.time()}
    if user_id: d['userId'] = str(user_id)
    if user_name: d['userName'] = user_name
    try:
        os.chmod(SESSION_FILE, 0o600)  # Windows 上可能无效，尽力而为
    except Exception:
        pass
    json.dump(d, open(SESSION_FILE, 'w', encoding='utf-8'), ensure_ascii=False)


def load_session():
    if not os.path.exists(SESSION_FILE): return None
    try: return json.load(open(SESSION_FILE, encoding='utf-8'))
    except Exception: return None


def load_cookie():
    return (load_session() or {}).get('cookie')


def clear_cookie():
    if os.path.exists(SESSION_FILE): os.remove(SESSION_FILE)


def cookie_ok(cookie_header):
    """ 校验业务成功标志 + 用户身份；返回 (ok, user_info)。
        不再只看 HTTP 200（业务失败时也可能返回200）。 """
    if not cookie_header: return False, None
    try:
        s = requests.Session()
        s.headers.update({'User-Agent': UA, 'Cookie': cookie_header, 'Accept': 'application/json'})
        r = s.get(USER_INFO, timeout=15)
        if r.status_code != 200: return False, None
        d = r.json()
        # 业务成功：有用户数据（id / userId）且无失败标志
        data = d.get('data') or d
        uid = data.get('id') or data.get('userId') or data.get('uid')
        code = d.get('code')
        if uid and code in (None, 0, 200, '0', '200'):
            return True, {'userId': str(uid), 'userName': data.get('name') or data.get('nickname')}
        # 兜底：拿做题历史验证（业务层成功必须返回 historyItems 字段容器）
        try:
            r2 = s.get('https://tiku.fenbi.com/combine/exercise/getExerciseBriefHistory'
                       '?categoryId=1&cursor=&limit=1&routecs=xingce', timeout=15)
            d2 = r2.json() if r2.status_code == 200 else {}
            data2 = d2.get('data') if isinstance(d2, dict) else None
            ok = (isinstance(data2, dict) and 'historyItems' in data2
                  and d2.get('code') in (0, 200, '0'))
            return ok, None
        except Exception:
            return False, None
    except Exception:
        return False, None


def verify_and_save(cookie_header, on_warning=None):
    """ 手动Cookie保存入口：先验证再保存，失败保留旧登录态。
        返回 (ok, message, user_info) """
    warn = clean_cookie_warning(cookie_header)
    if warn and on_warning: on_warning(warn)
    clean = _clean_cookie(cookie_header)
    ok, info = cookie_ok(clean)
    if not ok:
        return False, '验证失败：登录态无效或网络不通，未覆盖原有登录态。', None
    save_cookie(clean, (info or {}).get('userId'), (info or {}).get('userName'))
    return True, '验证成功，登录态已保存。' + (('账号ID：%s' % info['userId']) if info else ''), info


def header_from_cookies(cookiejar_or_dict):
    items = cookiejar_or_dict.items() if hasattr(cookiejar_or_dict, 'items') else list(cookiejar_or_dict)
    return '; '.join('%s=%s' % (k, v) for k, v in items if v is not None)


class QRLogin:
    def __init__(self, on_qr=None, on_status=None, cancel=None):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': UA,
                                     'Origin': 'https://spa.fenbi.com',
                                     'Referer': 'https://spa.fenbi.com/'})
        self.on_qr = on_qr or (lambda url: None)
        self.on_status = on_status or (lambda msg: None)
        self.cancel = cancel or threading.Event()
        self.lgtoken = None
        self.qr_url = None

    def start(self):
        import random
        try:
            r = self.session.get(QR_GEN, params={'random': str(random.random())}, timeout=20)
            d = r.json()
        except Exception as e:
            raise RuntimeError('获取二维码失败：%s' % e)
        data = d.get('data') or {}
        self.lgtoken = data.get('lgtoken')
        self.qr_url = data.get('codeContent') or data.get('codeUrl')
        if not self.lgtoken or not self.qr_url:
            raise RuntimeError('二维码数据异常，请重试')
        self.on_qr(self.qr_url)
        return self.qr_url

    def poll(self, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.cancel.is_set(): raise RuntimeError('已取消')
            try:
                r = self.session.post(QR_QUERY, json={'lgtoken': self.lgtoken}, timeout=15)
                d = r.json()
            except Exception:
                time.sleep(2); continue
            code = d.get('data')
            if isinstance(code, str) and code.isdigit(): code = int(code)
            if code == 3:
                self.on_status('扫码成功，正在获取登录态…')
                return self._finalize()
            if code == 4: raise RuntimeError('二维码已过期，请点重试')
            if code == 5: raise RuntimeError('登录失败，请重试')
            if code == 6:
                self.on_status('二维码刷新中…')
                try: self.start()
                except Exception: pass
            elif code == 2: self.on_status('已扫描，请在手机上确认…')
            else: self.on_status('等待扫码…')
            time.sleep(2)
        raise RuntimeError('扫码超时，请重试')

    def _finalize(self):
        cookie = header_from_cookies(self.session.cookies)
        ok, info = cookie_ok(cookie)
        if ok:
            save_cookie(_clean_cookie(cookie), (info or {}).get('userId'),
                        (info or {}).get('userName'))
            return _clean_cookie(cookie)
        got = self._try_exchange()
        if got:
            ok, info = cookie_ok(got)
            if ok:
                save_cookie(_clean_cookie(got), (info or {}).get('userId'),
                            (info or {}).get('userName'))
                return _clean_cookie(got)
        raise RuntimeError('已扫码，但未能取得做题接口的登录态，请用手动粘贴Cookie方式')

    def _try_exchange(self):
        attempts = ['https://login.fenbi.com/api/users/ticketToSession',
                    'https://ke.fenbi.com/api/users/ticketToSession']
        for url in attempts:
            try:
                r = self.session.get(url, timeout=10)
                if r.status_code == 200 and r.text.strip().startswith('{'):
                    c = header_from_cookies(self.session.cookies)
                    if c: return c
            except Exception:
                pass
        return None


def make_qr_image(url, path):
    import qrcode
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=6, border=2)
    qr.add_data(url); qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    img.save(path)
    return path
