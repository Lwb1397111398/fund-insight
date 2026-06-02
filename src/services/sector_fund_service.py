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

    _cache: Dict[str, Dict] = {}
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
                    'name': m.fund_name
                }

            self._cache_loaded = True
        finally:
            if self._should_close(db):
                db.close()

    def get_fund_by_sector(self, sector_name: str) -> Optional[Dict]:
        if sector_name in self._cache:
            return self._cache[sector_name]

        db = self._get_db()
        try:
            mapping = db.query(SectorFundMapping).filter(
                SectorFundMapping.sector_name == sector_name,
                SectorFundMapping.is_active == True
            ).first()

            if mapping:
                self._cache[sector_name] = {
                    'code': mapping.fund_code,
                    'name': mapping.fund_name
                }
                return self._cache[sector_name]

            return None
        finally:
            if self._should_close(db):
                db.close()

    def get_all_mappings(self) -> Dict[str, Dict]:
        self._load_cache()
        return self._cache.copy()

    def add_mapping(self, sector_name: str, fund_code: str, fund_name: str, keywords: List[str] = None) -> SectorFundMapping:
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
                db.commit()
                db.refresh(existing)
                self._cache[sector_name] = {'code': fund_code, 'name': fund_name}
                return existing
            else:
                mapping = SectorFundMapping(
                    sector_name=sector_name,
                    fund_code=fund_code,
                    fund_name=fund_name,
                    keywords=keywords
                )
                db.add(mapping)
                db.commit()
                db.refresh(mapping)
                self._cache[sector_name] = {'code': fund_code, 'name': fund_name}
                return mapping
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
