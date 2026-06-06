"""
基金服务
处理基金相关的业务逻辑
"""
from typing import List, Optional, Dict, Any
from datetime import date, datetime
from sqlalchemy.orm import Session
from sqlalchemy import func
import json
import threading

from .base import BaseService
from src.models.database import FundInfo, FundHistory, Prediction

# 基金更新锁，防止重复执行
_update_lock = threading.Lock()
_is_updating = False


class FundService(BaseService[FundInfo]):
    """基金服务类"""
    
    def __init__(self, db: Session):
        super().__init__(db, FundInfo)
    
    def get_by_code(self, fund_code: str) -> Optional[FundInfo]:
        """
        根据基金代码获取基金
        
        Args:
            fund_code: 基金代码
            
        Returns:
            基金实例或 None
        """
        return self.db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
    
    def get_by_sector(self, sector_type: str, skip: int = 0, limit: int = 100) -> List[FundInfo]:
        """
        根据板块类型获取基金列表
        
        Args:
            sector_type: 板块类型
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            基金列表
        """
        return self.db.query(FundInfo).filter(
            FundInfo.sector_type == sector_type
        ).offset(skip).limit(limit).all()
    
    def get_active(self, skip: int = 0, limit: int = 100) -> List[FundInfo]:
        """
        获取活跃基金列表
        
        Args:
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            活跃基金列表
        """
        return self.db.query(FundInfo).filter(
            FundInfo.active_predictions > 0
        ).order_by(FundInfo.active_predictions.desc()).offset(skip).limit(limit).all()
    
    def get_with_predictions(self, fund_code: str) -> Optional[Dict]:
        """
        获取基金及其预测
        
        Args:
            fund_code: 基金代码
            
        Returns:
            包含预测的基金信息
        """
        fund = self.get_by_code(fund_code)
        if not fund:
            return None
        
        predictions = self.db.query(Prediction).filter(
            Prediction.fund_code == fund_code,
            Prediction.status == 'pending'
        ).all()
        
        return {
            **fund.__dict__,
            "predictions": [p.__dict__ for p in predictions]
        }
    
    def update_nav(self, fund_code: str, nav: float, nav_date: date, 
                   day_growth: float = None) -> Optional[FundInfo]:
        """
        更新基金净值
        
        Args:
            fund_code: 基金代码
            nav: 净值
            nav_date: 净值日期
            day_growth: 日涨跌幅
            
        Returns:
            更新后的基金实例
        """
        return self.update_by_code(fund_code, {
            "latest_nav": nav,
            "nav_date": nav_date,
            "day_growth": day_growth
        })
    
    def update_by_code(self, fund_code: str, obj_in: dict) -> Optional[FundInfo]:
        """
        根据代码更新基金
        
        Args:
            fund_code: 基金代码
            obj_in: 更新数据字典
            
        Returns:
            更新后的基金实例
        """
        fund = self.get_by_code(fund_code)
        if fund:
            for key, value in obj_in.items():
                if hasattr(fund, key) and value is not None:
                    setattr(fund, key, value)
            self.db.commit()
            self.db.refresh(fund)
        return fund
    
    def increment_predictions(self, fund_code: str) -> Optional[FundInfo]:
        """
        增加活跃预测数
        
        Args:
            fund_code: 基金代码
            
        Returns:
            更新后的基金实例
        """
        fund = self.get_by_code(fund_code)
        if fund:
            fund.active_predictions = (fund.active_predictions or 0) + 1
            fund.can_delete = False
            self.db.commit()
            self.db.refresh(fund)
        return fund
    
    def decrement_predictions(self, fund_code: str) -> Optional[FundInfo]:
        """
        减少活跃预测数
        
        Args:
            fund_code: 基金代码
            
        Returns:
            更新后的基金实例
        """
        fund = self.get_by_code(fund_code)
        if fund and fund.active_predictions > 0:
            fund.active_predictions -= 1
            if fund.active_predictions == 0:
                fund.can_delete = True
            self.db.commit()
            self.db.refresh(fund)
        return fund
    
    def search(self, keyword: str, limit: int = 20) -> List[FundInfo]:
        """
        搜索基金
        
        Args:
            keyword: 搜索关键词
            limit: 返回数量
            
        Returns:
            匹配的基金列表
        """
        return self.db.query(FundInfo).filter(
            (FundInfo.fund_code.contains(keyword)) | 
            (FundInfo.fund_name.contains(keyword))
        ).limit(limit).all()
    
    def get_history(self, fund_code: str, days: int = 30) -> List[FundHistory]:
        """
        获取基金历史净值
        
        Args:
            fund_code: 基金代码
            days: 天数
            
        Returns:
            历史净值列表
        """
        return self.db.query(FundHistory).filter(
            FundHistory.fund_code == fund_code
        ).order_by(FundHistory.nav_date.desc()).limit(days).all()
    
    def add_history(self, fund_code: str, fund_name: str, nav_date: date, 
                    nav: float, day_growth: float = None) -> FundHistory:
        """
        添加历史净值记录
        
        Args:
            fund_code: 基金代码
            fund_name: 基金名称
            nav_date: 净值日期
            nav: 净值
            day_growth: 日涨跌幅
            
        Returns:
            创建的历史记录
        """
        history = FundHistory(
            fund_code=fund_code,
            fund_name=fund_name,
            nav_date=nav_date,
            nav=nav,
            day_growth=day_growth
        )
        self.db.add(history)
        self.db.commit()
        self.db.refresh(history)
        return history
    
    # ==================== 为路由重构新增的方法 ====================
    
    def _get_recent_history_map(self, fund_codes: List[str], per_fund: int = 5) -> Dict[str, List[FundHistory]]:
        if not fund_codes:
            return {}

        ranked_history = self.db.query(
            FundHistory.id.label("id"),
            func.row_number().over(
                partition_by=FundHistory.fund_code,
                order_by=FundHistory.nav_date.desc()
            ).label("row_number")
        ).filter(
            FundHistory.fund_code.in_(fund_codes)
        ).subquery()

        rows = self.db.query(FundHistory).join(
            ranked_history,
            FundHistory.id == ranked_history.c.id
        ).filter(
            ranked_history.c.row_number <= per_fund
        ).order_by(
            FundHistory.fund_code,
            FundHistory.nav_date.desc()
        ).all()

        history_map = {}
        for item in rows:
            history_map.setdefault(item.fund_code, []).append(item)
        return history_map

    def get_funds_with_grouping(
        self,
        skip: int = 0,
        limit: int = 100,
        sector_type: Optional[str] = None,
        group_by_sector: bool = True
    ) -> List[Dict]:
        """
        获取基金列表（支持按板块分组）
        优化：批量查询历史数据，解决 N+1 问题
        """
        query = self.db.query(FundInfo)
        if sector_type:
            query = query.filter(FundInfo.sector_type == sector_type)
        
        funds = query.offset(skip).limit(limit).all()
        
        if not funds:
            return [] if group_by_sector else []
        
        fund_codes = [f.fund_code for f in funds]
        
        history_map = self._get_recent_history_map(fund_codes, per_fund=5)
        
        if group_by_sector:
            sector_groups = {}
            
            for f in funds:
                sector = f.sector_type or "其他"
                if sector not in sector_groups:
                    sector_groups[sector] = {
                        "sector_name": sector,
                        "fund_count": 0,
                        "funds": [],
                        "avg_day_growth": 0,
                        "avg_week_growth": 0,
                        "avg_month_growth": 0,
                        "active_predictions": 0
                    }
                
                history = history_map.get(f.fund_code, [])
                fund_data = {
                    "id": f.id,
                    "fund_code": f.fund_code,
                    "fund_name": f.fund_name,
                    "fund_type": f.fund_type,
                    "sector_type": f.sector_type,
                    "latest_nav": f.latest_nav,
                    "nav_date": f.nav_date.isoformat() if f.nav_date else None,
                    "day_growth": f.day_growth,
                    "week_growth": f.week_growth,
                    "month_growth": f.month_growth,
                    "active_predictions": f.active_predictions,
                    "can_delete": f.can_delete,
                    "last_analyze_date": f.last_analyze_date.isoformat() if f.last_analyze_date else None,
                    "updated_at": f.updated_at.isoformat() if f.updated_at else None,
                    "recent_history": [
                        {
                            "date": h.nav_date.isoformat(),
                            "nav": h.nav,
                            "growth": h.day_growth
                        }
                        for h in history
                    ]
                }
                
                sector_groups[sector]["funds"].append(fund_data)
                sector_groups[sector]["fund_count"] += 1
                sector_groups[sector]["active_predictions"] += f.active_predictions or 0
            
            for sector in sector_groups:
                funds_list = sector_groups[sector]["funds"]
                if funds_list:
                    sector_groups[sector]["avg_day_growth"] = round(
                        sum(f.get("day_growth") or 0 for f in funds_list) / len(funds_list), 2
                    )
                    sector_groups[sector]["avg_week_growth"] = round(
                        sum(f.get("week_growth") or 0 for f in funds_list) / len(funds_list), 2
                    )
                    sector_groups[sector]["avg_month_growth"] = round(
                        sum(f.get("month_growth") or 0 for f in funds_list) / len(funds_list), 2
                    )
            
            return list(sector_groups.values())
        else:
            result = []
            for f in funds:
                history = history_map.get(f.fund_code, [])
                fund_data = {
                    "id": f.id,
                    "fund_code": f.fund_code,
                    "fund_name": f.fund_name,
                    "fund_type": f.fund_type,
                    "sector_type": f.sector_type,
                    "latest_nav": f.latest_nav,
                    "nav_date": f.nav_date.isoformat() if f.nav_date else None,
                    "day_growth": f.day_growth,
                    "week_growth": f.week_growth,
                    "month_growth": f.month_growth,
                    "active_predictions": f.active_predictions,
                    "can_delete": f.can_delete,
                    "last_analyze_date": f.last_analyze_date.isoformat() if f.last_analyze_date else None,
                    "updated_at": f.updated_at.isoformat() if f.updated_at else None,
                    "recent_history": [
                        {
                            "date": h.nav_date.isoformat(),
                            "nav": h.nav,
                            "growth": h.day_growth
                        }
                        for h in history
                    ]
                }
                result.append(fund_data)
            
            return result
    
    def add_fund_with_history(
        self,
        fund_code: str,
        fund_name: Optional[str] = None,
        fund_type: Optional[str] = None,
        sector_type: Optional[str] = None
    ) -> Dict:
        """
        添加基金并获取历史数据
        
        Args:
            fund_code: 基金代码
            fund_name: 基金名称（可选）
            fund_type: 基金类型（可选）
            sector_type: 板块类型（可选）
            
        Returns:
            添加结果
        """
        from src.fund.fund_api import fund_api, fund_data_manager
        
        existing = self.get_by_code(fund_code)
        if existing:
            return {"success": False, "message": "基金已存在", "data": None}
        
        fund_info = fund_api.get_fund_info(fund_code)
        
        def parse_nav_date(date_str: str):
            if not date_str:
                return None
            try:
                return datetime.strptime(date_str, "%Y-%m-%d").date()
            except (ValueError, TypeError) as e:
                print(f"[FundService] 日期解析失败: {date_str}, 错误: {e}")
                return None
        
        db_fund = FundInfo(
            fund_code=fund_code,
            fund_name=fund_name or (fund_info.get("fund_name") if fund_info else None),
            fund_type=fund_type or (fund_info.get("fund_type") if fund_info else None),
            sector_type=sector_type,
            latest_nav=fund_info.get("nav") if fund_info else None,
            nav_date=parse_nav_date(fund_info.get("nav_date")) if fund_info else None,
            day_growth=fund_info.get("day_growth") if fund_info else None
        )
        self.db.add(db_fund)
        self.db.commit()
        self.db.refresh(db_fund)
        
        history_count = 0
        try:
            history_count = fund_data_manager.update_fund_history(fund_code, days=10, db=self.db)
            self.db.commit()
        except Exception as e:
            print(f"[FundService] 获取历史净值失败: {e}")
        
        return {
            "success": True,
            "message": "基金添加成功",
            "data": {
                "id": db_fund.id,
                "fund_code": db_fund.fund_code,
                "fund_name": db_fund.fund_name,
                "history_count": history_count
            }
        }
    
    def get_fund_detail(self, fund_code: str) -> Optional[Dict]:
        """
        获取基金详情
        
        Args:
            fund_code: 基金代码
            
        Returns:
            基金详情或None
        """
        fund = self.get_by_code(fund_code)
        if not fund:
            return None
        
        history = self.db.query(FundHistory).filter(
            FundHistory.fund_code == fund_code
        ).order_by(FundHistory.nav_date.desc()).limit(30).all()
        
        return {
            "fund_code": fund.fund_code,
            "fund_name": fund.fund_name,
            "fund_type": fund.fund_type,
            "sector_type": fund.sector_type,
            "latest_nav": fund.latest_nav,
            "nav_date": fund.nav_date.isoformat() if fund.nav_date else None,
            "day_growth": fund.day_growth,
            "week_growth": fund.week_growth,
            "month_growth": fund.month_growth,
            "history": [
                {
                    "date": h.nav_date.isoformat() if h.nav_date else None,
                    "nav": h.nav,
                    "day_growth": h.day_growth
                }
                for h in history
            ]
        }
    
    def update_all_funds(self) -> Dict:
        """
        智能更新所有基金数据

        Returns:
            更新结果
        """
        global _is_updating

        # 检查是否正在更新
        if _is_updating:
            return {
                "success": False,
                "message": "基金更新正在进行中，请稍后再试",
                "data": None
            }

        # 获取锁
        if not _update_lock.acquire(blocking=False):
            return {
                "success": False,
                "message": "基金更新正在进行中，请稍后再试",
                "data": None
            }

        try:
            _is_updating = True
            from src.fund.fund_sync_manager import fund_sync_manager

            result = fund_sync_manager.full_sync(self.db)

            if result.get("success"):
                # 使用 full_sync 返回的详细消息
                message = result.get("message", "")
                if not message:
                    # 兜底：如果没有 message，手动构建
                    summary = result.get("summary", {})
                    message = f"同步完成：检测 {summary.get('total_predictions', 0)} 个预测，"
                    message += f"新增 {summary.get('new_funds_added', 0)} 个基金，"
                    message += f"关联 {summary.get('predictions_linked', 0)} 个预测，"
                    message += f"更新 {summary.get('funds_updated', 0)} 个基金"

                return {
                    "success": True,
                    "message": message,
                    "data": result
                }
            else:
                error_msg = result.get('error', '未知错误')
                return {
                    "success": False,
                    "message": f"同步失败: {error_msg}",
                    "data": result
                }

        except Exception as e:
            print(f"[FundService] 更新失败: {e}")
            import traceback
            traceback.print_exc()
            return {
                "success": False,
                "message": f"更新失败: {str(e)}",
                "data": None
            }
        finally:
            _is_updating = False
            _update_lock.release()
    
    def delete_fund(self, fund_code: str) -> Dict:
        """
        删除基金
        
        Args:
            fund_code: 基金代码
            
        Returns:
            删除结果
        """
        fund = self.get_by_code(fund_code)
        if not fund:
            return {"success": False, "message": "基金不存在"}
        
        if not fund.can_delete:
            return {"success": False, "message": "该基金有关联的预测，无法删除"}
        
        self.db.delete(fund)
        self.db.commit()
        
        return {"success": True, "message": "基金删除成功"}
