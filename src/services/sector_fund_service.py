"""
板块-基金映射服务
"""
import logging
from typing import Dict, Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from src.models.database import SectorFundMapping, FundInfo, SessionLocal

logger = logging.getLogger(__name__)

# 板块映射缓存有效期：跨进程（Cron 体检 → Web 读取）传播上限
_CACHE_TTL = 60.0


class SectorFundService:
    """板块-基金映射服务 - 使用缓存，按需创建会话"""

    _cache: Dict[str, Dict] = {}       # {sector_name: {'code': ..., 'name': ..., 'reviewed': bool}}
    _cache_loaded: bool = False
    _cache_at: float = 0.0

    def __init__(self, db: Session = None):
        self._external_db = db is not None
        self.db = db

    def _get_db(self) -> Session:
        if self._external_db and self.db:
            return self.db
        return SessionLocal()

    def _should_close(self, db: Session) -> bool:
        return not self._external_db or db is not self.db

    def _load_cache(self):
        # TTL：体检跑在 Render Cron 进程里，Web 进程的类级缓存若永不过期，
        # 降级就要等到下次重启才生效——那等于"这轮白做"，而且没有任何报错。
        import time
        if SectorFundService._cache_loaded and \
                (time.time() - SectorFundService._cache_at) < _CACHE_TTL:
            return

        db = self._get_db()
        # 先灌进这张局部表，全部成功后才整体替换 `_cache`（第 15 轮 MAJOR-2）。
        # 上一版为了"取消审查要立刻生效"在查询**之前**清空类级缓存，于是任何一次
        # 查询失败（Supabase 抖一下、连接池超时）都会把好那份整体销毁：
        # `fund_matching` Level -1 的 except 与 `llm_analyzer._get_sector_fund_map`
        # 的 except 会把空结果静默降级成**写死的静态表** —— 那正是 S5 BLOCKER-1
        # 认定的绕过口（静态表一命中就绕过身份体检 + 人工审查的结论）。
        # "旧但完整"永远比"空表 + 静默降级"安全，所以失败时保留上一份继续服务。
        loaded: Dict[str, Dict] = {}
        try:
            # sector_name 不是唯一列：没有 ORDER BY 时"同名多行"取哪一条取决于
            # 数据库返回顺序，SQLite 与 Postgres 可能给出不同基金（同一板块两种结论）。
            # 固定为"已审查优先、id 最小优先"，且已审查条目不被未审查条目覆盖。
            mappings = db.query(SectorFundMapping).filter(
                SectorFundMapping.is_active == True
            ).order_by(
                SectorFundMapping.reviewed.desc().nulls_last(),
                SectorFundMapping.id.asc(),
            ).all()

            for m in mappings:
                if self._unservable(m):
                    # 体检判定"不可服务"的行（股票名/同码别的基金/代码填错/基金域没有此码）
                    # 不进缓存：它既不该服务帖子分析，也不该挡住同板块另一条可服务的行。
                    # 管理界面走 get_all_mappings_with_status，仍然看得见。
                    continue
                # 清空重灌的语义由"这是一张全新的局部表"提供：上一轮那个已被取消审查的
                # 旧条目不会再混进来（第 14 轮 MAJOR-1 要修的正是这个）。
                existing = loaded.get(m.sector_name)
                if existing and existing.get('reviewed') and not (m.reviewed or False):
                    continue
                loaded[m.sector_name] = {
                    'code': m.fund_code,
                    'name': m.fund_name,
                    'reviewed': m.reviewed or False,
                }

            SectorFundService._cache = loaded
            SectorFundService._cache_loaded = True
            SectorFundService._cache_at = time.time()
        except Exception as exc:
            if SectorFundService._cache_loaded:
                age = int(time.time() - SectorFundService._cache_at)
                logger.warning('板块映射重灌失败（%s），继续用上一份缓存（%d 条，%d 秒前）；'
                               '清空重灌会让上层静默退回写死的静态表，绕过体检与人工审查'
                               % (exc, len(SectorFundService._cache), age))
                return
            raise
        finally:
            if self._should_close(db):
                db.close()

    def get_fund_by_sector(self, sector_name: str) -> Optional[Dict]:
        """获取板块对应的基金（优先返回 reviewed=True 的映射）"""
        from src.services.sector_identity_audit import servable_predicate
        # 先走 TTL 检查再读缓存：以前"命中 reviewed 就 return"从不过期 ⇒
        # Cron 里取消审查/降级永远传不到 Web 进程，而这条路径现在在匹配链最前端
        # （第 14 轮 MAJOR-1）。`_load_cache` 自带"没到期就直接返回"，代价是每 60 秒一次查询。
        self._load_cache()
        if sector_name in self._cache:
            cached = self._cache[sector_name]
            if cached.get('reviewed'):
                return cached

        db = self._get_db()

        def _first_servable(query, strict_relevance=False):
            # `servable_predicate()` 只看 `is_fetchable` 列，而"不可服务"还有第二个事实源
            # （`evidence.identity.verdict`）⇒ 粗筛之后必须再过一次唯一判据，否则
            # "列 NULL + verdict 否定"的幽灵行会在这里被当成可服务标的返回，
            # **并且写进进程内缓存**，污染这一进程后续所有帖子分析
            # （第 23 轮 MAJOR-1：同一形态第三次复现）。
            # `strict_relevance` 只给"未审查降级"那条路用：体检说过
            # "这只标的与板块字面无关、而名册里另有字面对口的那只"时，未审查的行
            # 不该被拿去服务新帖子（第 25 轮实测：核聚变→红利低波ETF、区块链→云计算ETF
            # 都因为降级不过这道门而继续指错）。已审查的行不受影响 —— 老板点过就算认。
            from src.services.sector_identity_audit import row_relevance_low
            for row in query.limit(20).all():
                if self._unservable(row):
                    continue
                if strict_relevance and row_relevance_low(row):
                    continue
                return row
            return None

        try:
            # 优先查 reviewed=True
            mapping = _first_servable(db.query(SectorFundMapping).filter(
                SectorFundMapping.sector_name == sector_name,
                SectorFundMapping.is_active == True,
                SectorFundMapping.reviewed == True,
                servable_predicate(),
            ))

            if not mapping:
                # 降级查 reviewed=False。降级分支同样要过滤：体检判"不可服务"的行
                # 如果在这里被捞回来，"取消 reviewed"就等于什么都没做（000725 京东方Ａ
                # 实测正是这样继续服务帖子分析的）。
                # 另外还要过相关性这道门（`strict_relevance=True`）：未审查 + 体检说
                # "这只与板块无关、名册里另有字面对口的"＝机器自己都不信自己，
                # 拿它去贴新帖子就是老板抱怨的那个症状（第 26 轮修，任务 #23）。
                mapping = _first_servable(db.query(SectorFundMapping).filter(
                    SectorFundMapping.sector_name == sector_name,
                    SectorFundMapping.is_active == True,          # noqa: E712
                    servable_predicate(),
                ), strict_relevance=True)

            if mapping:
                result = {
                    'code': mapping.fund_code,
                    'name': mapping.fund_name,
                    'reviewed': mapping.reviewed or False
                }
                self._cache[sector_name] = result
                return result

            return None
        finally:
            if self._should_close(db):
                db.close()

    @staticmethod
    def _unservable(row) -> bool:
        """体检结论有两个来源：`is_fetchable` 列与 evidence 里的 verdict，都要认。"""
        from src.services.sector_identity_audit import row_unservable
        return row_unservable(row)

    # 路由要区分"这行不存在"与"这行被体检拒绝"，所以这个判断得是公开 API
    # （让调用方去碰 `_unservable` 私有名，等于鼓励它在外面自己抄一遍判据）。
    is_unservable = _unservable

    @staticmethod
    def _drop_owner_immunity(row):
        """收回"老板署名 + 体检豁免"这一对，**必须一起收**。

        只撤一半就是两处同时说谎：`row_unservable()` 的 owner 例外看
        `owner_locked or reviewed_by=='owner'`，agent 的覆盖守卫只看 `owner_locked`
        （`sector_fund_agent.py`），留一半会留下"既躲体检又能驱动改标"的僵尸行
        （第 8/9 轮各踩过一次）。四条写入路径共用这个 helper（第 19 轮 MINOR-7：
        之前抄了四份，谁也说不清哪份是最新的）。
        """
        row.reviewed_by = None
        row.owner_locked = False

    def get_all_mappings(self) -> Dict[str, Dict]:
        self._load_cache()
        return self._cache.copy()

    def get_all_mappings_with_status(self, reviewed_filter: Optional[bool] = None) -> List[Dict]:
        """获取所有映射（含 reviewed 状态），供 API 使用"""
        db = self._get_db()
        try:
            query = db.query(SectorFundMapping).filter(SectorFundMapping.is_active == True)
            if reviewed_filter is not None:
                query = query.filter(SectorFundMapping.reviewed == reviewed_filter)

            # 待审查排在前面，同状态内按板块名排序
            mappings = query.order_by(SectorFundMapping.reviewed.asc(), SectorFundMapping.sector_name).all()
            from src.services.sector_identity_audit import identity_view
            out = []
            for m in mappings:
                item = {
                    'id': m.id,
                    'sector_name': m.sector_name,
                    'fund_code': m.fund_code,
                    'fund_name': m.fund_name,
                    'reviewed': m.reviewed or False,
                    # 免疫只能由老板逐行确认买到；列表要能区分"老板已确认"与
                    # "只是编辑过 / 批量看过"（第 18 轮 MAJOR-1）
                    'reviewed_by': m.reviewed_by,
                    'owner_locked': bool(m.owner_locked),
                    # 与 GET /sector-mappings 合入的 builtin 条目对齐，
                    # 前端依赖 source 字段区分内置/自定义（此前 DB 行缺该字段）
                    'source': 'custom',
                    'created_at': m.created_at.isoformat() if m.created_at else None,
                    'updated_at': m.updated_at.isoformat() if m.updated_at else None
                }
                item.update(identity_view(m))
                out.append(item)
            return out
        finally:
            if self._should_close(db):
                db.close()

    def ensure_fund_info_exists(self, fund_code: str, fund_name: str = None,
                                sector_type: str = None,
                                identity_checked: bool = False) -> bool:
        """保存映射前确保 fund_info 里存在该基金档案 —— **档案落笔的唯一咽喉，所以身份门也在这里**。

        sector_fund_mapping.fund_code 有外键指向 fund_info.fund_code，
        若用户为一个尚不在基金库里的代码保存映射，直接写会报
        FOREIGN KEY constraint failed。这里先补一条最小档案
        （代码+名称+板块），净值/历史由后续基金同步任务补全。

        第 49 轮两席共同指出：老板要的是"以后不会再有错误机制产生这种无效数据"，
        而那道门上一批装在 `LLMAnalyzer._save_fund_mapping` 上 —— 那个函数自 `9c598cf`
        起在 `src/` 里**零调用方**，判据还直接调私有方法 ⇒ 绿灯替死代码作保，
        而页面上填一个股票代码时**档案先落库、身份门后判**。现在门挪到这里。

        **措辞边界（第 50 轮两席又抓到同一种过头，别再写满）**：本方法是
        "页面保存/编辑映射"与"审计回写"这条咽喉的门，**不是"所有 caller 的咽喉"** ——
        往 `fund_info` 写行的活路不止这里（agent 自己 `db.add(FundInfo(...))`、
        `full_sync`、`fund_api`、`fund_service`、`data_portability_service` 都写）。
        有几处、分别是谁，用 `grep -rn ensure_fund_info_exists src/` 与
        `grep -rn FundInfo. src/` 现量，**别在这一段抄数字**。那几处各自另有尺子
        （agent 的 T2 判股票即拒、`full_sync` 只在基金域答得出时才建），今天没证据显示它们
        在产生新垃圾档案 —— 但**别人的尺子不算这道门**。

        Args:
            identity_checked: **已过身份门的调用方**显式传 True 才旁路 —— 今天两处：
                `config.py` 的审计回写（它上面三条"不许进门"的判断已经把这一行问过了）、
                本模块 `update_mapping`（它刚用同一个判据探过一次，再探就是同一次保存
                打两圈外网 —— 第 50 轮 A-MINOR-2）。默认 False ⇒ 建之前先问
                "这码是不是基金"，判"不是基金"就不建
                （`_manual_identity_verdict` 失败开放：站点抖动/查不到一律按没结论处理，
                不挡正常保存）。旁路不许由"代码看起来像基金"推出来。

        Returns:
            True 表示本次新建了档案，False 表示已存在、未提供代码或**被身份门拒建**。
        """
        code = (fund_code or '').strip()
        if not code:
            return False

        db = self._get_db()
        try:
            exists = db.query(FundInfo.fund_code).filter(FundInfo.fund_code == code).first()
            if exists:
                return False

            if not identity_checked:
                # 探针是网络活（名册首次 30s、单码 10s×3）：先结束只读事务再探，
                # 否则生产连接池上会挂出几分钟的 `idle in transaction`
                # （与 `update_mapping` 同一把姿势，见那个函数里的注释）。
                name, sector = (fund_name or '').strip(), sector_type
                db.rollback()
                accusation, _verdict = _manual_identity_verdict(code, name, sector or '')
                if accusation:
                    logger.info('[基金档案] 拒建 %s（%s）：%s', code, name or '无名', accusation)
                    return False

            db.add(FundInfo(
                fund_code=code,
                fund_name=(fund_name or '').strip() or None,
                sector_type=sector_type,
            ))
            db.commit()
            logger.info(f"[基金档案] 为板块映射自动创建最小基金档案: {code} ({fund_name})")
            return True
        except IntegrityError:
            # 并发场景下别的请求已创建，视为已存在
            db.rollback()
            return False
        except Exception as e:
            db.rollback()
            logger.warning(f"[基金档案] 自动创建基金档案失败 {code}: {e}")
            raise
        finally:
            if self._should_close(db):
                db.close()

    def mark_reviewed_by_id(self, mapping_id: int, reviewed: bool = True,
                            owner_confirm: bool = False) -> bool:
        """按 ID 标记审查。

        与 `batch_mark_reviewed` 同一套证据守卫：没有 `match_source + verified_at`
        的行不能凭空变成"已审查"，否则审查门禁等于不存在。
        取消审查（reviewed=False）时同时解除 owner_locked，避免"锁定但未审查"的僵尸行。
        """
        db = self._get_db()
        try:
            mapping = db.query(SectorFundMapping).filter(
                SectorFundMapping.id == mapping_id
            ).first()
            if not mapping:
                return False
            if reviewed and self._unservable(mapping):
                # 体检判定"不可服务"（股票名/同码别的基金/代码填错）的行，即使
                # match_source+verified_at 齐备、即使老板勾了"确认"，也不能再回到
                # 已审查：证据齐备恰恰是体检自己写上去的，否则一次误点就复活股票。
                logger.info('[板块映射] 拒绝标记 %s(%s)：%s',
                            mapping.sector_name, mapping.fund_code,
                            mapping.verify_message or '身份判定未通过')
                return False
            if reviewed and not (mapping.match_source and mapping.verified_at) \
                    and not owner_confirm:
                return False

            mapping.reviewed = reviewed
            if reviewed:
                # 第 17 轮 MAJOR-1：署名与豁免也必须吃 `owner_confirm`，否则这条门
                # 对"有机器证据的行"完全无效 —— 实测裸 POST（不带任何参数）照样落
                # `reviewed_by='owner' + owner_locked=True`，而上一轮的承诺是
                # "免疫只能在 API 边界上显式确认才给"。现在与批量路径同一口径：
                # 确认过才是老板署名 + 锁定；没确认只是"看过"，署名跟随来源。
                # 第 8 轮那条老教训仍然成立：`owner_locked=1` 配 `reviewed_by='agent'`
                # 是两处同时说谎（既绕 agent 自己的 0.80 门槛，又被回写脚本当成
                # 老板手定的代理报上去），所以署名与锁定必须一起给、一起撤。
                if owner_confirm:
                    mapping.reviewed_by = 'owner'
                    mapping.owner_locked = True
                else:
                    mapping.reviewed_by = mapping.reviewed_by or 'manual_review'
                mapping.match_source = mapping.match_source or 'manual'
            else:
                self._drop_owner_immunity(mapping)
            db.commit()
            self.refresh_cache()
            return True
        finally:
            if self._should_close(db):
                db.close()

    def batch_mark_reviewed(self, mapping_ids: List[int], reviewed: bool = True,
                           owner_confirm: bool = False) -> int:
        """批量标记已审查。

        没有证据的行（`match_source`/`verified_at` 均为空）默认拒绝：否则"一键全部标记
        已审查"就能把审查门禁清零，而门禁是这轮迭代唯一的护栏。老板在弹窗里明确确认
        （`owner_confirm=True`）时允许，但会记成 `reviewed_by='owner'` + 锁定，
        与 agent 的结论可区分、可追溯。
        """
        from datetime import datetime as _dt
        db = self._get_db()
        try:
            rows = db.query(SectorFundMapping).filter(
                SectorFundMapping.id.in_(mapping_ids)).all()
            flipped, skipped = 0, []
            rejected = []
            for row in rows:
                has_evidence = bool(row.match_source and row.verified_at)
                if reviewed and self._unservable(row):
                    # 身份体检判"不可服务"的行不能被批量审查复活，owner_confirm 也不行
                    rejected.append(row.sector_name)
                    continue
                if reviewed and not has_evidence and not owner_confirm:
                    skipped.append(row.sector_name)
                    continue
                row.reviewed = reviewed
                if reviewed:
                    if owner_confirm:
                        # 老板明确确认过：署名 owner + 锁定（可追溯、享受体检豁免）
                        row.reviewed_by = 'owner'
                        row.owner_locked = True
                    else:
                        # 第 14 轮 MAJOR-2：以前不管 owner_confirm 真假都盖 `reviewed_by='owner'`
                        # + `owner_locked=True` ⇒ 一次点击就让 23 行拿到永久体检免疫，
                        # 而弹窗写的是"不会冒充老板署名"。现在不确认就不署名、不锁定，
                        # 只承认"这些行有机器证据、已批量看过"；署名与豁免留给逐行确认。
                        row.reviewed_by = row.reviewed_by or 'batch_review'
                        # owner_locked 一律不动。（上一版这里写 `= bool(row.owner_locked)`，
                        # 除把 NULL 变成 0 之外什么都不做，而全仓读者都是 `== True`/真值判断
                        # —— 空操作反而让"这行没被批量点击锁住"这件事看不出来，第 15 轮 m-2）
                    row.match_source = row.match_source or 'manual'
                    row.updated_at = _dt.now()
                else:
                    # 取消审查必须连"老板锁定"一起撤：`mark_reviewed_by_id` 一直是这么做的。
                    # 以前这里没有 else，批量取消审查会留下"未审查 + reviewed_by='owner'
                    # + owner_locked=True"的僵尸行 —— 它既躲开体检（owner 例外），
                    # 又能驱动预测改标，还把"机器已纠正待复核"的旗标藏起来（第 9 轮 MAJOR-1）。
                    self._drop_owner_immunity(row)
                    row.updated_at = _dt.now()
                flipped += 1
            db.commit()
            if skipped:
                logger.info('[批量审查] %d 条因无证据被跳过：%s', len(skipped), skipped[:10])
            if rejected:
                logger.info('[批量审查] %d 条因身份体检不通过被拒绝（不可复活）：%s',
                            len(rejected), rejected[:10])
            self.refresh_cache()
            self._last_batch_review_skipped = skipped
            self._last_batch_review_rejected = rejected
            return flipped
        finally:
            if self._should_close(db):
                db.close()

    def update_mapping(self, mapping_id: int, fund_code: str = None,
                       fund_name: str = None,
                       mark_reviewed: Optional[bool] = None,
                       owner_confirm: bool = False) -> Optional[Dict]:
        """更新映射（基金代码/名称）。

        `mark_reviewed=None` 沿用旧行为（手工编辑即视为已审查）；agent 写库时必须
        显式传 False/True，否则低置信结果会被自动标成"已审查"（审查门禁就废了）。

        **老板署名与体检豁免只认 `owner_confirm`**（与 `mark_reviewed_by_id` /
        `batch_mark_reviewed` 同一口径，第 18 轮 MAJOR-1）。编辑保存只是"改过这一行"，
        署名记成 `manual_review`；换了标的却没重新确认时，旧标的上的老板锁定一并撤掉 ——
        锁的是那只旧基金，不该由一次改码继承给新代码。

        **换了标的就先证身份**（`_manual_identity_verdict`）：判"这码根本不是基金"时
        不置审查、不锁老板，写 `is_fetchable=False` 并说明理由；判"没查到"（接口抖动、
        新基金未入库）一律按"没意见"处理，沿用旧行为——审查门不能建在网络抖动上。
        """
        db = self._get_db()
        try:
            mapping = db.query(SectorFundMapping).filter(
                SectorFundMapping.id == mapping_id
            ).first()
            if not mapping:
                return None

            # 体检结论的输入是 (代码, 名字, 板块)：换代码**或**换名字都让旧结论失效，
            # 退回"从未体检"（NULL 仍可服务），等下一轮体检重新证。
            # 只按**代码**判"结论作废"：降级理由的钥匙是代码（同码撞车的另一只基金），
            # 改名字不足以让一只股票变成基金——老板只改名就能复活降级行是不安全的。
            changed = fund_code is not None and fund_code != mapping.fund_code
            # 探测是网络活（名册首次 timeout=30s，单码 10s×3 重试）：**不能**把库事务
            # 压在它身上 —— 生产走 Supabase 连接池，一次页面保存会变成几分钟
            # `idle in transaction`。此刻还没有任何写入，先结束只读事务，探完再取行。
            accusation = identity = None
            if changed:
                sector_name = mapping.sector_name        # rollback 后实例过期，先存标量
                probe_code, probe_name = fund_code, fund_name or mapping.fund_name
                db.rollback()
                accusation, identity = _manual_identity_verdict(
                    probe_code, probe_name, sector_name)
                if not accusation:
                    # 档案在这里补，不再由 PUT/POST 路由先补一次：路由那一次调用带着**自己的**
                    # 身份门 ⇒ 同一次保存打两圈外网（第 50 轮 A-MINOR-2）。传
                    # `identity_checked=True` 是因为这一行刚刚才被同一个判据问过一遍。
                    self.ensure_fund_info_exists(probe_code, probe_name, sector_name,
                                                 identity_checked=True)
                if accusation and db.query(FundInfo.fund_code).filter(
                        FundInfo.fund_code == probe_code).first() is None:
                    # 判"不是基金"⇒ 档案被拒建 ⇒ 这个码**不能**落到映射行上：
                    # `sector_fund_mapping.fund_code → fund_info` 的外键在镜像上真生效
                    # （`_create_sqlite_engine` 开了 `PRAGMA foreign_keys`）⇒ 写下去是
                    # IntegrityError，而**生产库里压根没有这条约束**（2026-09-26 只读量过）
                    # ⇒ 写下去静默留下一行指向查无此码的映射。两个结局都不接受，所以整笔拒改。
                    # 不顺手把旧标的标成"不可服务"：那一行指的是**上一只**基金，
                    # 老板打错一个码不该让一个正常板块失去标的（读路径会立刻查不到这个板块）。
                    logger.info('[板块映射] 拒改标的 %s → %s：%s',
                                mapping.fund_code, probe_code, accusation)
                    return None
            if changed:
                mapping.is_fetchable = None
                mapping.evidence = _drop_identity_evidence(mapping.evidence)
                # 理由列讲的是被换掉那只，留着会让这行"可服务"却挂着旧降级说明
                mapping.verify_message = None
            if fund_code is not None:
                mapping.fund_code = fund_code
            if fund_name is not None:
                mapping.fund_name = fund_name
            mapping.reviewed = True if mark_reviewed is None else mark_reviewed
            if changed:
                # M4：人工换标的本来是**唯一一条没有"这是基金不是股票"证明的写入路径**
                # （机器两条都有：`etf_candidates` 只收场内 ETF 官方名、
                #  `sweep.reaudit_new_code` 用非名册域名重证一次），而它在页面上
                # 直接产出 `is_fetchable=None + reviewed + owner_locked` =
                # 体检与 agent 永久免疫的行。老板在 UI 里输 600519「贵州茅台」
                # 就是这么变成"可信的错误标的"的。判定已在**事务外**探好（见上）。
                if accusation:
                    # 只在这一次判定**确定**否掉时才动手：owner_locked 会挡住
                    # `row_unservable()` 的整条判据（老板例外），所以必须同时撤掉
                    # 审查与锁定，否则"is_fetchable=False"是个永不生效的假动作。
                    mapping.is_fetchable = False
                    mapping.verify_message = accusation
                    mapping.reviewed = False
                    self._drop_owner_immunity(mapping)
                    if identity is not None:
                        # 老板看得见"到底是哪只基金顶上了这个码"，也才有下一轮体检
                        mapping.evidence = _stamp_identity(mapping.evidence, identity)
            if changed and not owner_confirm and (
                    mapping.owner_locked or mapping.reviewed_by == 'owner'):
                # 老板当年锁的是**旧标的**。换代码而不重新确认，留着锁定 = 新代码天生免疫：
                # `row_unservable()` 的 owner 例外会吃掉整条判据（列与 verdict 都跳过）。
                # 撤掉的只是继承来的豁免，老板重新点"已审查"（带 owner_confirm）就能拿回。
                self._drop_owner_immunity(mapping)
            if mapping.reviewed and self._unservable(mapping):
                # 既没换标的也没换名字、只是把状态翻回"已审查" → 拒绝（防一键复活）
                logger.info('[板块映射] 拒绝标记 %s(%s)：身份体检不通过',
                            mapping.sector_name, mapping.fund_code)
                db.rollback()
                return None
            # 写库出口重新物化镜像不变量：verdict 属不可服务 ⇒ 列必须 False。
            # 否则"只有 verdict 是否定"的行会让 SQL 读者继续服务、Python 读者隐藏它。
            if self._unservable(mapping):
                mapping.is_fetchable = False
            if mapping.reviewed:
                # 第 18 轮 MAJOR-1：上一版这里不看 `owner_confirm`，任何一次编辑保存
                # （PUT 路由从不传确认参数、UI 保存也没有弹窗）都无条件盖
                # `reviewed_by='owner' + owner_locked=True` ⇒ 白送永久体检免疫。
                # 署名与锁定必须一起给、一起撤（第 8 轮教训：只给一半是两处同时说谎）。
                if owner_confirm:
                    mapping.reviewed_by = 'owner'
                    mapping.owner_locked = True
                else:
                    mapping.reviewed_by = mapping.reviewed_by or 'manual_review'
                mapping.match_source = mapping.match_source or 'manual'
            # 编辑即激活：若该行曾被级联清理置为 inactive，保存后必须恢复可见，
            # 否则更新会"成功"但列表按 is_active 过滤后凭空丢失该板块
            mapping.is_active = True
            db.commit()
            db.refresh(mapping)

            # 不手写缓存条目：`_load_cache` 会过滤不可服务的行，
            # 直接塞 dict 会让"刚保存的行立刻可见、但所有读路径都不该看到它"这种情况发生
            self.refresh_cache()

            return {
                'id': mapping.id,
                'sector_name': mapping.sector_name,
                'fund_code': mapping.fund_code,
                'fund_name': mapping.fund_name,
                # 换标的被身份证明否掉时这里是 False：调用方（PUT/POST 路由）不许再
                # 无条件写"已标记为已审查"，否则老板看到的是一个根本没审过的行
                'reviewed': bool(mapping.reviewed),
                # 调用方要按真话回执：老板署名/豁免只在显式确认后才给，
                # 路由不能只看 `reviewed` 就宣称"已标记为老板已审查"
                'reviewed_by': mapping.reviewed_by,
                'owner_locked': bool(mapping.owner_locked),
                'verify_message': mapping.verify_message,
            }
        finally:
            if self._should_close(db):
                db.close()

    def delete_mapping(self, mapping_id: int) -> bool:
        """删除映射"""
        db = self._get_db()
        try:
            mapping = db.query(SectorFundMapping).filter(
                SectorFundMapping.id == mapping_id
            ).first()
            if not mapping:
                return False

            sector_name = mapping.sector_name
            db.delete(mapping)
            db.commit()

            if sector_name in self._cache:
                del self._cache[sector_name]
            return True
        finally:
            if self._should_close(db):
                db.close()

    def cascade_cleanup_conflicts(self, sector_name: str, fund_code: str, fund_name: str) -> Dict:
        """
        级联整理：用户编辑板块→基金映射后，停用冲突映射。

        只处理可恢复的映射状态；基金与历史净值属于业务资料，必须保留。
        """
        db = self._get_db()
        cleanup_log = {"sector_fund_mapping": 0, "fund_info": 0}

        try:
            # 1. SectorFundMapping：将同板块但不同基金的记录标记为不活跃
            conflicts = db.query(SectorFundMapping).filter(
                SectorFundMapping.sector_name == sector_name,
                SectorFundMapping.fund_code != fund_code
            ).all()
            for c in conflicts:
                c.is_active = False
                cleanup_log["sector_fund_mapping"] += 1
                logger.info(f"[级联清理] SectorFundMapping: {sector_name} → {c.fund_name}({c.fund_code}) 标记为不活跃")

            # 2. FundInfo 可能包含同板块的有效历史基金，绝不能因映射调整删除。
            fund_info_conflicts = db.query(FundInfo).filter(
                FundInfo.sector_type == sector_name,
                FundInfo.fund_code != fund_code
            ).all()
            if fund_info_conflicts:
                logger.info(
                    "[级联整理] 保留 %s 条同板块基金资料: %s",
                    len(fund_info_conflicts),
                    sector_name,
                )

            if any(v > 0 for v in cleanup_log.values()):
                db.commit()
                logger.info(f"[级联清理] 完成: {cleanup_log}")
            else:
                logger.debug(f"[级联清理] {sector_name} 无冲突需要清理")

        except Exception as e:
            db.rollback()
            logger.warning(f"[级联清理] 失败: {e}")

        finally:
            if self._should_close(db):
                db.close()

        return cleanup_log

    def refresh_cache(self):
        self._cache.clear()
        SectorFundService._cache_loaded = False
        self._load_cache()


_sector_fund_service: Optional[SectorFundService] = None


def get_sector_fund_service(db: Session = None) -> SectorFundService:
    """获取板块-基金服务。

    不传 db → 全局单例（只读缓存，跨请求复用）；
    传 db → 返回**临时实例**。以前这里会把请求级 Session 钉到单例上，
    于是批量线程与其他请求线程会共用同一个 Session
    （SQLAlchemy Session 不是线程安全的）。
    """
    global _sector_fund_service
    if db is not None:
        return SectorFundService(db)
    if _sector_fund_service is None:
        _sector_fund_service = SectorFundService()
    return _sector_fund_service


# 人工改码后要摘掉的过期键。`identity` 是"这一行当前标的"的身份结论，
# `identity_realign`/`etf_upgrade`/`identity_before_upgrade` 是"机器把这行从谁换成了谁"
# 的溯源章（`machine_swap_of()` 的判据 = 章里的代码仍是当前代码）。
_STALE_IDENTITY_KEYS = ('identity', 'identity_realign', 'etf_upgrade',
                        'identity_before_upgrade')


def _drop_identity_evidence(raw):
    """人工改码后，把已过期的身份结论与换标溯源从 evidence 里摘掉。

    这些键讲的都是**被换掉那只**：留着前端会继续指向一次更早的机器纠正。
    而且 M3 把 agent 的自批挡板改成读 evidence 之后，不摘章就等于"这一行永远
    不许自动审查"——老板手改标的**就是那两个章在等的确认**（v7.4 第 6 轮 M4）。
    `prev` 与候选轨迹（`tiers`）保留：那是决策过程，不是身份结论。
    """
    import json as _json
    if not raw:
        return raw
    try:
        data = _json.loads(raw)
    except Exception:
        return raw
    if isinstance(data, dict) and any(k in data for k in _STALE_IDENTITY_KEYS):
        for key in _STALE_IDENTITY_KEYS:
            data.pop(key, None)
        return _json.dumps(data, ensure_ascii=False)
    return raw


# 判定"确定不是这只基金"的三类结论 + "基金域查无此码"，见 audit.UNSERVABLE_VERDICTS。
MANUAL_UNSERVABLE_MESSAGE = '基金域查无此码：这是股票/已清盘，不能作为板块标的'


def _manual_identity_verdict(code, stored_name, sector):
    """人工换标的后的身份证明：返回 `(给老板看的理由, 身份结论 dict)`，放行时 `(None, None)`。

    判据只有"查到了且不对"这一类才动手（`UNSERVABLE_VERDICTS`：股票名/同码别的基金/
    代码填错/基金域明确回答没有这个码）。`unknown`、`probe_unavailable` 与任何异常
    都**失败开放**：体检模块自己的原则是"没查到不是反向证据"，一次网络抖动绝不能
    把老板刚挑的标的判成不可服务——那会让所有读路径立刻失去这个板块。

    探针不另写一份：直接复用 `sector_identity_audit.arbitrate_mapping`（函数内 import
    是本仓库避环的既有写法，agent 与体检互相引用也走这条路）。这里用它的默认探针
    （名册优先、单码兜底）：sweep 换标的时要把名册关掉，是因为那行的**名字本来就是
    名册写进去的**（拿名册自证 = Jaccard 恒 1.0）；这一支的名字是老板手打的，
    两条证据互相独立，名册反而是"德明利/京东方Ａ 这类根本不是基金的名字"的
    第二证据（搜索接口实测会抖，单靠它不能判死）。代价是进程内第一次名册加载
    （3.1MB / 实测 2s），之后整进程复用。
    """
    from src.services import sector_identity_audit as audit
    from datetime import datetime
    try:
        res = audit.arbitrate_mapping((code or '').strip(), (stored_name or '').strip(),
                                      sector or '')
    except Exception as exc:
        # 站点/解析任何意外都按"没结论"处理，绝不能让保存按钮因为体检坏了而报错
        logger.warning('[板块映射] 人工换标的的身份判定失败（按不干预处理）：%s', exc)
        return None, None
    verdict = (res or {}).get('verdict')
    if verdict not in audit.UNSERVABLE_VERDICTS:
        return None, None
    official = (res or {}).get('official_name')
    reason = MANUAL_UNSERVABLE_MESSAGE
    if official:
        reason = '%s（该代码实为「%s」）' % (MANUAL_UNSERVABLE_MESSAGE, official)
    identity = {
        'verdict': verdict,
        'jaccard': res.get('jaccard'),
        'official_name': official,
        'reason': '%s：%s' % (reason, res.get('reason') or ''),
        'suggested_code': res.get('suggested_code'),
        'suggested_name': res.get('suggested_name'),
        'suggestions': [],
        'relevance_low': False,
        'evidence': res.get('evidence'),
        'checked_at': datetime.now().isoformat(timespec='seconds'),
        'source': 'manual_edit',
    }
    return reason, identity


def _stamp_identity(raw, identity):
    """把这次的身份结论写进 evidence（前端与下一轮体检都要看得见它）。"""
    import json as _json
    try:
        data = _json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            data = {'prev': data}
    except Exception:
        data = {}
    data['identity'] = identity
    return _json.dumps(data, ensure_ascii=False)
