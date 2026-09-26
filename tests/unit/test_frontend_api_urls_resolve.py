# -*- coding: utf-8 -*-
"""闸：前端每一个 `/api/...` 字面量都必须对上后端**真的注册过**的路由（任务 #47）。

为什么补：量出线上落后 113 个提交那次，`/api/stats/evidence` 在生产上就是 **404**，而页面照旧渲染 ——
"接口不存在"与"接口没数据"在界面上是同一种表现。路径写错、后端改了名、路由忘了 `include_router`，
全都是**静默**的。所以这条判据不拿一份手搓正则去猜后端有什么路由（那等于再造一份真值副本，
第 30 轮那一族），而是直接读 `src.api.main.app.routes` 里 FastAPI 自己注册的那一份。

两条控制断言（没有"现造一处违规必须被点名"的判据等于没有判据）：
① 造一条不存在的 `/api/...` ⇒ 必须被点名；
② 从 `app.routes` 里现取一条真路由 ⇒ 不许被点名（否则这条闸只是墙）。

边界（说明白，别当成全覆盖）：只判**路径**，不判方法 —— 前端用 `POST` 打了只注册 `GET` 的路由，
这一条看不见（那要逐处读调用点，不是路径集合能表达的）。
"""
import io
import os
import re

from fastapi.routing import APIRoute

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEB = os.path.join(ROOT, 'web')
# 引号或反引号里、以 /api 开头的一段：允许字母数字 _ - / ${} . ? = & % + （够覆盖本仓全部 70 条写法）
_LITERAL = re.compile(r'''['"`](/api[A-Za-z0-9_\-/${}.?=&%+]*)['"`]''')
_TEMPLATE = re.compile(r'\$\{[^`]*?\}')


def _web_files():
    out = []
    for name in sorted(os.listdir(WEB)):
        if name.endswith(('.js', '.html')):
            out.append(os.path.join(WEB, name))
    return out


def _normalize(raw):
    """/api/funds?group_by_sector=false&skip=${n} → /api/funds；/api/posts/${id}/analyze → /api/posts/{x}/analyze。"""
    path = re.split(r'[?#]', raw, 1)[0]
    path = _TEMPLATE.sub('{x}', path)
    return '/' + '/'.join(s for s in path.split('/') if s)


def _frontend_urls(files=None):
    """返回 [(文件相对路径, 原始串, 归一化路径)]，只收 `/api` 开头的字面量。"""
    found = []
    for fp in (files if files is not None else _web_files()):
        with io.open(fp, encoding='utf-8', errors='replace') as fh:
            text = fh.read()
        for m in _LITERAL.finditer(text):
            raw = m.group(1)
            # 跨盘符时 relpath 会抛 ValueError（本机 tmp 在 C:、仓库在 E:），
            # 而"控制断言用临时文件喂抽取器"这条路正是要能走 —— 报不出相对路径就退回文件名。
            try:
                rel = os.path.relpath(fp, ROOT).replace('\\', '/')
            except ValueError:
                rel = os.path.basename(fp)
            found.append((rel, raw, _normalize(raw)))
    return found


def _registered_segment_lists():
    """FastAPI 自己那份路由表：每条路由拆成段列表，`{param}` 原样留着。"""
    from src.api.main import app
    out = []
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith('/api'):
            out.append(tuple(s for s in route.path.split('/') if s))
    return out


def _segment_matches(front, back):
    """只有**后端那一格是参数**才算通配。

    第 49 轮 B 席量到旧写法反了一面：`front.startswith('{')` 让"前端是 `${id}`"
    对**任何**同长度路由都算命中 —— `/api/bloggers/${id}` 归一后撞上的其实是
    `/api/bloggers/top`，于是"删除博主"打一条已被删掉的路由（真 404）而闸报绿。
    前端那一格是变量时，只能配后端也是变量（下一行），不能配字面量。
    """
    if back.startswith('{'):
        return True
    if front.startswith('{'):
        return False
    return front == back


def _unmatched(items, registered):
    bad = []
    for rel, raw, path in items:
        segs = tuple(s for s in path.split('/') if s)
        if not any(len(segs) == len(reg) and all(_segment_matches(a, b) for a, b in zip(segs, reg))
                   for reg in registered):
            bad.append((rel, path))
    return bad


def test_every_frontend_api_url_resolves_to_a_registered_route():
    items = _frontend_urls()
    assert len(items) >= 60, '前端只抓到 %d 条 /api 字面量 ⇒ 抽取本身坏了（正则漂了还是 web/ 目录换了）' % len(items)
    registered = _registered_segment_lists()
    assert len(registered) >= 50, 'app.routes 里只有 %d 条 /api 路由 ⇒ 这份对照不可信' % len(registered)
    bad = _unmatched(items, registered)
    assert not bad, '这些前端路径在后端注册的路由里找不到（只报路径，不报内容）：%s' % sorted(set(bad))


def test_a_path_that_exists_nowhere_is_caught():
    """控制断言①：现造一条不存在的 ⇒ 必须点名（否则上面那条只是在描述自己）。"""
    registered = _registered_segment_lists()
    planted = [('web/index.html', '/api/no-such-endpoint-anywhere',
                _normalize('/api/no-such-endpoint-anywhere'))]
    assert _unmatched(planted, registered), '这条路径压根没注册过却没被点名 ⇒ 这把尺子是空的'


def test_a_route_the_backend_actually_registers_is_not_flagged():
    """控制断言②：拿 app.routes 里的一条真路由当"前端写法"喂回去 ⇒ 不许被点名（闸不是墙）。"""
    registered = _registered_segment_lists()
    assert registered, '没有可对照的路由'
    real = '/' + '/'.join(registered[0])
    assert not _unmatched([('web/x.js', real, _normalize(real))], registered), \
        '后端真注册的路径被判成不存在 ⇒ 匹配器只会报红，没人能信它'


def test_placeholders_and_query_strings_are_stripped_the_same_way():
    """归一化自己的账：查询串与 `${}` 都要落回同一条路径，否则对照会随机漂移。"""
    assert _normalize('/api/funds?group_by_sector=false&skip=${skip}') == '/api/funds'
    assert _normalize('/api/predictions/${prediction.id}') == '/api/predictions/{x}'
    assert _normalize('/api/viewpoints/tasks/${t.value.task_id}/retry') == '/api/viewpoints/tasks/{x}/retry'


def test_a_variable_segment_only_buys_a_match_against_a_route_parameter():
    """第 49 轮 B-1 的那一格：`/api/bloggers/${id}` 曾撞上 `/api/bloggers/top` 而判"命中"。

    旧写法 `front.startswith('{')` 让**前端是变量**这一格对任何字面量路由都放行，
    于是"路由被删了两个月"这种事实正好从闸眼里过去。两侧都要判：
    没有参数路由兜着 ⇒ 必须红；有了 ⇒ 必须放行（闸不是墙）。
    """
    reg_no_param = [('api', 'bloggers'), ('api', 'bloggers', 'top'), ('api', 'posts', '{post_id}')]
    planted = [('web/index.html', '/api/bloggers/${id}', '/api/bloggers/{x}')]
    assert _unmatched(planted, reg_no_param), \
        '后端根本没有 /api/bloggers/{id}，闸却说命中 ⇒ 通配那一臂还在白买'
    assert not _unmatched(planted, reg_no_param + [('api', 'bloggers', '{blogger_id}')]), \
        '路由补回来后仍报红 ⇒ 这条闸会逼人把它关掉'


def test_the_extractor_itself_catches_a_url_written_in_a_temp_file(tmp_path):
    """A-6 的那一格：控制断言必须过**抽取器**，不能只喂手搓三元组。

    旧控制断言把 planted 直接写进 `_unmatched` ⇒ 它证明的是"匹配器有牙"，
    抽取器坏一半（少收一个文件、漏一种写法）仍然全绿，而地板值 60 离真值 88
    意味着可以静默丢掉 27 条 URL。这里现造一条**模板串写法**的字面量，
    要求它被 `_frontend_urls` 收进来、并被 `_unmatched` 点名。
    """
    plant = tmp_path / 'planted.js'
    plant.write_text(
        "const u = `/api/no-such-route-in-this-repo/${id}`;\n"
        "const v = '/api/also-not-registered';\n", encoding='utf-8')
    items = _frontend_urls([str(plant)])
    paths = {p for _, _, p in items}
    assert '/api/no-such-route-in-this-repo/{x}' in paths, \
        '模板串里的 /api 没被抽出来 ⇒ 抽取器看不见这种写法'
    assert '/api/also-not-registered' in paths
    assert len(_unmatched(items, _registered_segment_lists())) == 2, \
        '两条都不存在的 URL 没被全部点名 ⇒ 这把尺子是空的'
