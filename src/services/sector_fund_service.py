"""
板块-基金映射服务
"""
from typing import Dict, Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.models.database import SectorFundMapping, SessionLocal


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
        db = self._get_db()
        try:
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
    return _sector_fund_service
