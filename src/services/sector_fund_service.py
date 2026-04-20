"""
板块-基金映射服务
"""
from typing import Dict, Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.models.database import SectorFundMapping, SessionLocal


class SectorFundService:
    """板块-基金映射服务"""
    
    _cache: Dict[str, Dict] = {}
    _cache_loaded: bool = False
    
    def __init__(self, db: Session = None):
        self.db = db or SessionLocal()
        self._load_cache()
    
    def _load_cache(self):
        """加载缓存"""
        if self._cache_loaded:
            return
        
        mappings = self.db.query(SectorFundMapping).filter(
            SectorFundMapping.is_active == True
        ).all()
        
        for m in mappings:
            self._cache[m.sector_name] = {
                'code': m.fund_code,
                'name': m.fund_name
            }
        
        self._cache_loaded = True
    
    def get_fund_by_sector(self, sector_name: str) -> Optional[Dict]:
        """
        根据板块名称获取基金
        
        Args:
            sector_name: 板块名称
            
        Returns:
            基金信息 {'code': ..., 'name': ...} 或 None
        """
        if sector_name in self._cache:
            return self._cache[sector_name]
        
        mapping = self.db.query(SectorFundMapping).filter(
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
    
    def get_all_mappings(self) -> Dict[str, Dict]:
        """获取所有映射"""
        return self._cache.copy()
    
    def add_mapping(self, sector_name: str, fund_code: str, fund_name: str, keywords: List[str] = None) -> SectorFundMapping:
        """添加映射"""
        mapping = SectorFundMapping(
            sector_name=sector_name,
            fund_code=fund_code,
            fund_name=fund_name,
            keywords=keywords
        )
        self.db.add(mapping)
        self.db.commit()
        self.db.refresh(mapping)
        
        self._cache[sector_name] = {'code': fund_code, 'name': fund_name}
        
        return mapping
    
    def refresh_cache(self):
        """刷新缓存"""
        self._cache.clear()
        self._cache_loaded = False
        self._load_cache()


_sector_fund_service: Optional[SectorFundService] = None


def get_sector_fund_service(db: Session = None) -> SectorFundService:
    """获取板块-基金服务单例"""
    global _sector_fund_service
    if _sector_fund_service is None:
        _sector_fund_service = SectorFundService(db)
    return _sector_fund_service
