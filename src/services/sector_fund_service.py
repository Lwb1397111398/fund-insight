"""
板块-基金映射服务
"""
import logging
from typing import Dict, Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.models.database import SectorFundMapping, FundInfo, SessionLocal

logger = logging.getLogger(__name__)


class SectorFundService:
    """板块-基金映射服务 - 使用缓存，按需创建会话"""

    _cache: Dict[str, Dict] = {}       # {sector_name: {'code': ..., 'name': ..., 'reviewed': bool}}
    _cache_loaded: bool = False

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
        if self._cache_loaded:
            return

        db = self._get_db()
        try:
            mappings = db.query(SectorFundMapping).filter(
                SectorFundMapping.is_active == True
            ).all()

            for m in mappings:
                self._cache[m.sector_name] = {
                    'code': m.fund_code,
                    'name': m.fund_name,
                    'reviewed': m.reviewed or False
                }

            self._cache_loaded = True
        finally:
            if self._should_close(db):
                db.close()

    def get_fund_by_sector(self, sector_name: str) -> Optional[Dict]:
        """获取板块对应的基金（优先返回 reviewed=True 的映射）"""
        if sector_name in self._cache:
            cached = self._cache[sector_name]
            if cached.get('reviewed'):
                return cached

        db = self._get_db()
        try:
            # 优先查 reviewed=True
            mapping = db.query(SectorFundMapping).filter(
                SectorFundMapping.sector_name == sector_name,
                SectorFundMapping.is_active == True,
                SectorFundMapping.reviewed == True
            ).first()

            if not mapping:
                # 降级查 reviewed=False
                mapping = db.query(SectorFundMapping).filter(
                    SectorFundMapping.sector_name == sector_name,
                    SectorFundMapping.is_active == True
                ).first()

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

            mappings = query.order_by(SectorFundMapping.sector_name).all()
            return [
                {
                    'id': m.id,
                    'sector_name': m.sector_name,
                    'fund_code': m.fund_code,
                    'fund_name': m.fund_name,
                    'reviewed': m.reviewed or False,
                    'created_at': m.created_at.isoformat() if m.created_at else None,
                    'updated_at': m.updated_at.isoformat() if m.updated_at else None
                }
                for m in mappings
            ]
        finally:
            if self._should_close(db):
                db.close()

    def add_mapping(self, sector_name: str, fund_code: str, fund_name: str,
                    keywords: List[str] = None, reviewed: bool = False) -> SectorFundMapping:
        """添加或更新映射（upsert），并级联清理低优先级层的冲突数据"""
        db = self._get_db()
        try:
            existing = db.query(SectorFundMapping).filter(
                SectorFundMapping.sector_name == sector_name
            ).first()

            if existing:
                existing.fund_code = fund_code
                existing.fund_name = fund_name
                if keywords is not None:
                    existing.keywords = keywords
                existing.is_active = True
                if reviewed:
                    existing.reviewed = True
                db.commit()
                db.refresh(existing)
                self._cache[sector_name] = {
                    'code': fund_code, 'name': fund_name, 'reviewed': existing.reviewed or False
                }
                return existing
            else:
                mapping = SectorFundMapping(
                    sector_name=sector_name,
                    fund_code=fund_code,
                    fund_name=fund_name,
                    keywords=keywords,
                    reviewed=reviewed
                )
                db.add(mapping)
                db.commit()
                db.refresh(mapping)
                self._cache[sector_name] = {
                    'code': fund_code, 'name': fund_name, 'reviewed': reviewed
                }
                return mapping
        finally:
            if self._should_close(db):
                db.close()

    def mark_reviewed(self, sector_name: str, reviewed: bool = True) -> bool:
        """标记映射为已审查/未审查"""
        db = self._get_db()
        try:
            mapping = db.query(SectorFundMapping).filter(
                SectorFundMapping.sector_name == sector_name
            ).first()
            if not mapping:
                return False

            mapping.reviewed = reviewed
            db.commit()
            if sector_name in self._cache:
                self._cache[sector_name]['reviewed'] = reviewed
            return True
        finally:
            if self._should_close(db):
                db.close()

    def mark_reviewed_by_id(self, mapping_id: int, reviewed: bool = True) -> bool:
        """按 ID 标记映射为已审查/未审查"""
        db = self._get_db()
        try:
            mapping = db.query(SectorFundMapping).filter(
                SectorFundMapping.id == mapping_id
            ).first()
            if not mapping:
                return False

            mapping.reviewed = reviewed
            db.commit()
            if mapping.sector_name in self._cache:
                self._cache[mapping.sector_name]['reviewed'] = reviewed
            return True
        finally:
            if self._should_close(db):
                db.close()

    def batch_mark_reviewed(self, mapping_ids: List[int], reviewed: bool = True) -> int:
        """批量标记映射为已审查/未审查"""
        db = self._get_db()
        try:
            count = db.query(SectorFundMapping).filter(
                SectorFundMapping.id.in_(mapping_ids)
            ).update({'reviewed': reviewed}, synchronize_session='fetch')
            db.commit()
            self.refresh_cache()
            return count
        finally:
            if self._should_close(db):
                db.close()

    def update_mapping(self, mapping_id: int, fund_code: str = None,
                       fund_name: str = None) -> Optional[Dict]:
        """更新映射（基金代码/名称），自动标记为已审查"""
        db = self._get_db()
        try:
            mapping = db.query(SectorFundMapping).filter(
                SectorFundMapping.id == mapping_id
            ).first()
            if not mapping:
                return None

            if fund_code is not None:
                mapping.fund_code = fund_code
            if fund_name is not None:
                mapping.fund_name = fund_name
            mapping.reviewed = True  # 编辑自动标记为已审查
            db.commit()
            db.refresh(mapping)

            self._cache[mapping.sector_name] = {
                'code': mapping.fund_code,
                'name': mapping.fund_name,
                'reviewed': True
            }

            return {
                'id': mapping.id,
                'sector_name': mapping.sector_name,
                'fund_code': mapping.fund_code,
                'fund_name': mapping.fund_name,
                'reviewed': True
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
        级联清理：用户编辑板块→基金映射后，删除低优先级层中同板块不同基金的冲突条目。

        清理范围：
        - SectorFundMapping 表：删除同板块但不同基金的其他记录（is_active=False）
        - FundInfo 表：删除 sector_type 匹配但 fund_code 不同的记录

        不清理：
        - 硬编码表（代码里写死的，无法删除，但优先级低于用户编辑）
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

            # 2. FundInfo：删除 sector_type 匹配但 fund_code 不同的记录
            fund_info_conflicts = db.query(FundInfo).filter(
                FundInfo.sector_type == sector_name,
                FundInfo.fund_code != fund_code
            ).all()
            for f in fund_info_conflicts:
                db.delete(f)
                cleanup_log["fund_info"] += 1
                logger.info(f"[级联清理] FundInfo: {sector_name} → {f.fund_name}({f.fund_code}) 已删除")

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
        self._cache_loaded = False
        self._load_cache()


_sector_fund_service: Optional[SectorFundService] = None


def get_sector_fund_service(db: Session = None) -> SectorFundService:
    """获取板块-基金服务单例（不再持有会话）"""
    global _sector_fund_service
    if _sector_fund_service is None:
        _sector_fund_service = SectorFundService(db)
    elif db is not None:
        # 更新 db 引用，确保使用当前请求的 session
        _sector_fund_service.db = db
        _sector_fund_service._external_db = True
    return _sector_fund_service
