"""
帖子服务
处理帖子相关的业务逻辑
"""
from typing import List, Optional, Dict, Tuple, Any
from datetime import date, datetime
from sqlalchemy.orm import Session
from sqlalchemy import func

from .base import BaseService
from src.models.database import Post, Prediction, Blogger
from src.analyzer.llm_analyzer import get_analyzer


class PostService(BaseService[Post]):
    """帖子服务类"""
    
    def __init__(self, db: Session):
        super().__init__(db, Post)
    
    def get_by_blogger(self, blogger_id: int, skip: int = 0, limit: int = 100) -> List[Post]:
        """
        获取博主的帖子列表
        
        Args:
            blogger_id: 博主 ID
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            帖子列表
        """
        return self.db.query(Post).filter(
            Post.blogger_id == blogger_id
        ).order_by(Post.post_date.desc()).offset(skip).limit(limit).all()
    
    def get_by_date_range(self, start_date: date, end_date: date, skip: int = 0, limit: int = 100) -> List[Post]:
        """
        获取日期范围内的帖子
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            帖子列表
        """
        return self.db.query(Post).filter(
            Post.post_date >= start_date,
            Post.post_date <= end_date
        ).order_by(Post.post_date.desc()).offset(skip).limit(limit).all()
    
    def get_unanalyzed(self, limit: int = 50) -> List[Post]:
        """
        获取未分析的帖子
        
        Args:
            limit: 返回数量
            
        Returns:
            未分析的帖子列表
        """
        return self.db.query(Post).filter(
            Post.analyzed == False
        ).order_by(Post.created_at.desc()).limit(limit).all()
    
    def get_with_predictions(self, post_id: int) -> Optional[Dict]:
        """
        获取帖子及其预测
        
        Args:
            post_id: 帖子 ID
            
        Returns:
            包含预测的帖子信息
        """
        post = self.get(post_id)
        if not post:
            return None
        
        predictions = self.db.query(Prediction).filter(
            Prediction.post_id == post_id
        ).all()
        
        return {
            **post.__dict__,
            "predictions": [p.__dict__ for p in predictions]
        }
    
    def mark_analyzed(self, post_id: int, analysis_result: Dict) -> Optional[Post]:
        """
        标记帖子已分析
        
        Args:
            post_id: 帖子 ID
            analysis_result: 分析结果
            
        Returns:
            更新后的帖子实例
        """
        return self.update(post_id, {
            "analyzed": True,
            "analysis_result": analysis_result
        })
    
    def update_title(self, post_id: int, title: str, auto_titled: bool = True) -> Optional[Post]:
        """
        更新帖子标题
        
        Args:
            post_id: 帖子 ID
            title: 新标题
            auto_titled: 是否自动生成
            
        Returns:
            更新后的帖子实例
        """
        return self.update(post_id, {
            "title": title,
            "auto_titled": auto_titled
        })
    
    def search(self, keyword: str, skip: int = 0, limit: int = 20) -> List[Post]:
        """
        搜索帖子
        
        Args:
            keyword: 搜索关键词
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            匹配的帖子列表
        """
        return self.db.query(Post).filter(
            (Post.title.contains(keyword)) | (Post.content.contains(keyword))
        ).order_by(Post.post_date.desc()).offset(skip).limit(limit).all()
    
    def count_by_blogger(self, blogger_id: int) -> int:
        """
        统计博主的帖子数量
        
        Args:
            blogger_id: 博主 ID
            
        Returns:
            帖子数量
        """
        return self.db.query(func.count(Post.id)).filter(
            Post.blogger_id == blogger_id
        ).scalar()
    
    # ==================== 为路由重构新增的方法 ====================
    
    def get_posts_with_blogger_info(
        self, 
        skip: int = 0, 
        limit: int = 100,
        blogger_id: Optional[int] = None,
        analyzed: Optional[bool] = None
    ) -> List[Dict]:
        """
        获取帖子列表（包含博主信息）
        
        Args:
            skip: 跳过记录数
            limit: 返回记录数
            blogger_id: 博主ID筛选
            analyzed: 是否已分析筛选
            
        Returns:
            帖子列表（包含博主名称）
        """
        query = self.db.query(Post)
        
        if blogger_id:
            query = query.filter(Post.blogger_id == blogger_id)
        if analyzed is not None:
            query = query.filter(Post.analyzed == analyzed)
        
        posts = query.order_by(Post.post_date.desc()).offset(skip).limit(limit).all()
        
        result = []
        for p in posts:
            blogger = self.db.query(Blogger).filter(Blogger.id == p.blogger_id).first()
            result.append({
                "id": p.id,
                "blogger_id": p.blogger_id,
                "blogger_name": blogger.name if blogger else "未知",
                "title": p.title,
                "content": p.content[:200] + "..." if len(p.content) > 200 else p.content,
                "post_date": p.post_date.isoformat() if p.post_date else None,
                "source_url": p.source_url,
                "analyzed": p.analyzed,
                "auto_titled": p.auto_titled,
                "created_at": p.created_at.isoformat() if p.created_at else None
            })
        
        return result
    
    def get_post_detail(self, post_id: int) -> Optional[Dict]:
        """
        获取帖子详情（包含预测列表）
        
        Args:
            post_id: 帖子ID
            
        Returns:
            帖子详情字典或None
        """
        post = self.get(post_id)
        if not post:
            return None
        
        # 获取关联的预测
        predictions = self.db.query(Prediction).filter(
            Prediction.post_id == post_id,
            Prediction.is_deleted == False
        ).all()
        
        prediction_list = []
        for p in predictions:
            prediction_list.append({
                "id": p.id,
                "fund_code": p.fund_code,
                "fund_name": p.fund_name,
                "sector": p.sector,
                "sector_type": p.sector_type,
                "prediction_type": p.prediction_type,
                "prediction_content": p.prediction_content,
                "prediction_period": p.prediction_period,
                "target_date": p.target_date.isoformat() if p.target_date else None,
                "confidence": p.confidence,
                "status": p.status,
                "is_correct": p.is_correct,
                "verify_count": p.verify_count,
                "verify_score": p.verify_score
            })
        
        return {
            "id": post.id,
            "blogger_id": post.blogger_id,
            "title": post.title,
            "content": post.content,
            "post_date": post.post_date.isoformat() if post.post_date else None,
            "source_url": post.source_url,
            "analyzed": post.analyzed,
            "analysis_result": post.analysis_result,
            "auto_titled": post.auto_titled,
            "created_at": post.created_at.isoformat() if post.created_at else None,
            "predictions": prediction_list
        }
    
    def create_post_with_analysis(
        self, 
        blogger_id: int,
        content: str,
        post_date: date,
        title: Optional[str] = None,
        source_url: Optional[str] = None,
        async_mode: bool = True
    ) -> Dict:
        """
        创建帖子并自动分析
        
        Args:
            blogger_id: 博主ID
            content: 帖子内容
            post_date: 发布日期
            title: 标题（可选，不传则自动生成）
            source_url: 来源URL（可选）
            async_mode: 是否异步模式（True=快速返回，后台分析）
            
        Returns:
            创建结果，包含帖子信息和创建的预测数量
        """
        from src.fund.fund_auto_manager import fund_auto_manager
        
        llm_analyzer = get_analyzer()
        
        blogger = self.db.query(Blogger).filter(Blogger.id == blogger_id).first()
        if not blogger:
            raise ValueError("博主不存在")
        
        auto_titled = False
        if not title:
            try:
                post_date_str = post_date.strftime('%Y-%m-%d') if post_date else datetime.now().strftime('%Y-%m-%d')
                title = f"{post_date_str} {blogger.name}"
                auto_titled = True
            except (AttributeError, ValueError) as e:
                title = content[:30]
        
        db_post = Post(
            blogger_id=blogger_id,
            title=title,
            content=content,
            post_date=post_date,
            source_url=source_url,
            auto_titled=auto_titled,
            analyzed=False
        )
        self.db.add(db_post)
        self.db.commit()
        self.db.refresh(db_post)
        
        if async_mode:
            return {
                "id": db_post.id,
                "title": db_post.title,
                "auto_titled": auto_titled,
                "analyzed": False,
                "predictions_created": 0,
                "message": "帖子已添加，请手动点击分析"
            }
        
        analysis_result = None
        predictions_created = 0
        
        try:
            result = llm_analyzer.analyze_post(
                title=title or "",
                content=content,
                post_date=post_date.isoformat() if post_date else None
            )
            
            db_post.analyzed = True
            db_post.analysis_result = result
            analysis_result = result
            
            for pred in result.get("predictions", []):
                sector = pred.get("sector", "")
                
                fund_code, fund_name = self._match_fund_for_prediction(
                    pred=pred,
                    sector=sector,
                    fund_auto_manager=fund_auto_manager,
                    llm_analyzer=llm_analyzer
                )
                
                prediction = Prediction(
                    post_id=db_post.id,
                    blogger_id=db_post.blogger_id,
                    fund_code=fund_code,
                    fund_name=fund_name,
                    sector=sector,
                    sector_type=pred.get("sector_type", fund_auto_manager.get_category_for_sector(sector) if sector else "其他"),
                    prediction_type=pred.get("prediction_type"),
                    prediction_content=pred.get("prediction_content"),
                    confidence=pred.get("confidence", 50),
                    prediction_date=db_post.post_date,
                    prediction_period=pred.get("prediction_period", "1周"),
                    target_date=llm_analyzer.calculate_target_date(
                        db_post.post_date,
                        pred.get("prediction_period", "1周")
                    ),
                    next_verify_date=llm_analyzer.calculate_next_verify_date(
                        db_post.post_date,
                        llm_analyzer.calculate_target_date(
                            db_post.post_date,
                            pred.get("prediction_period", "1周")
                        )
                    )
                )
                self.db.add(prediction)
                predictions_created += 1
            
            self.db.commit()
            
        except Exception as e:
            print(f"[PostService] 分析帖子失败: {e}")
            import traceback
            traceback.print_exc()
            self.db.rollback()
            try:
                db_post = Post(
                    blogger_id=blogger_id,
                    title=title,
                    content=content,
                    post_date=post_date,
                    source_url=source_url,
                    auto_titled=auto_titled,
                    analyzed=False
                )
                self.db.add(db_post)
                self.db.commit()
                self.db.refresh(db_post)
                print(f"[PostService] 帖子已保存（未分析）: ID={db_post.id}")
            except Exception as e2:
                print(f"[PostService] 保存帖子失败: {e2}")
                return {
                    "id": None,
                    "title": title,
                    "auto_titled": auto_titled,
                    "analyzed": False,
                    "predictions_created": 0
                }
        
        return {
            "id": db_post.id,
            "title": db_post.title,
            "auto_titled": auto_titled,
            "analyzed": db_post.analyzed,
            "predictions_created": predictions_created
        }
    
    def analyze_post_async(self, post_id: int) -> Dict:
        """
        异步分析帖子（用于后台任务）
        
        Args:
            post_id: 帖子ID
            
        Returns:
            分析结果
        """
        from src.fund.fund_auto_manager import fund_auto_manager
        
        llm_analyzer = get_analyzer()
        
        db_post = self.db.query(Post).filter(Post.id == post_id).first()
        if not db_post:
            return {"success": False, "message": "帖子不存在"}
        
        if db_post.analyzed:
            return {"success": True, "message": "帖子已分析"}
        
        predictions_created = 0
        
        try:
            result = llm_analyzer.analyze_post(
                title=db_post.title or "",
                content=db_post.content,
                post_date=db_post.post_date.isoformat() if db_post.post_date else None
            )
            
            db_post.analyzed = True
            db_post.analysis_result = result
            
            for pred in result.get("predictions", []):
                sector = pred.get("sector", "")
                
                fund_code, fund_name = self._match_fund_for_prediction(
                    pred=pred,
                    sector=sector,
                    fund_auto_manager=fund_auto_manager,
                    llm_analyzer=llm_analyzer
                )
                
                prediction = Prediction(
                    post_id=db_post.id,
                    blogger_id=db_post.blogger_id,
                    fund_code=fund_code,
                    fund_name=fund_name,
                    sector=sector,
                    sector_type=pred.get("sector_type", fund_auto_manager.get_category_for_sector(sector) if sector else "其他"),
                    prediction_type=pred.get("prediction_type"),
                    prediction_content=pred.get("prediction_content"),
                    confidence=pred.get("confidence", 50),
                    prediction_date=db_post.post_date,
                    prediction_period=pred.get("prediction_period", "1周"),
                    target_date=llm_analyzer.calculate_target_date(
                        db_post.post_date,
                        pred.get("prediction_period", "1周")
                    ),
                    next_verify_date=llm_analyzer.calculate_next_verify_date(
                        db_post.post_date,
                        llm_analyzer.calculate_target_date(
                            db_post.post_date,
                            pred.get("prediction_period", "1周")
                        )
                    )
                )
                self.db.add(prediction)
                predictions_created += 1
            
            self.db.commit()
            
            return {
                "success": True,
                "message": f"分析完成，创建 {predictions_created} 个预测",
                "predictions_created": predictions_created
            }
            
        except Exception as e:
            print(f"[PostService] 异步分析帖子失败: {e}")
            import traceback
            traceback.print_exc()
            self.db.rollback()
            return {
                "success": False,
                "message": f"分析失败: {str(e)}",
                "predictions_created": 0
            }
    
    def batch_analyze_posts(self) -> Dict:
        """
        批量分析未分析的帖子
        每个帖子使用独立会话，LLM调用期间不持有数据库连接
        
        Returns:
            分析结果统计
        """
        from src.fund.fund_auto_manager import fund_auto_manager
        from src.models.database import SessionLocal
        
        llm_analyzer = get_analyzer()
        
        db_query = SessionLocal()
        try:
            post_ids = [p.id for p in db_query.query(Post).filter(
                Post.analyzed == False
            ).order_by(Post.created_at.desc()).limit(100).all()]
        finally:
            db_query.close()
        
        if not post_ids:
            return {
                "analyzed": 0,
                "failed": 0,
                "message": "没有需要分析的帖子"
            }
        
        analyzed_count = 0
        failed_count = 0
        
        for post_id in post_ids:
            db = SessionLocal()
            try:
                post = db.query(Post).filter(Post.id == post_id).first()
                if not post or post.analyzed:
                    continue
                
                title = post.title or ""
                content = post.content
                post_date_str = post.post_date.isoformat() if post.post_date else None
                post_date_val = post.post_date
                blogger_id = post.blogger_id
            finally:
                db.close()
            
            try:
                result = llm_analyzer.analyze_post(
                    title=title,
                    content=content,
                    post_date=post_date_str
                )
                
                db2 = SessionLocal()
                try:
                    post = db2.query(Post).filter(Post.id == post_id).first()
                    if not post:
                        continue
                    
                    post.analyzed = True
                    post.analysis_result = result
                    db2.commit()
                    
                    for pred in result.get("predictions", []):
                        sector = pred.get("sector", "")
                        
                        fund_code, fund_name = self._match_fund_for_prediction(
                            pred=pred,
                            sector=sector,
                            fund_auto_manager=fund_auto_manager,
                            llm_analyzer=llm_analyzer
                        )
                        
                        prediction = Prediction(
                            post_id=post.id,
                            blogger_id=blogger_id,
                            fund_code=fund_code,
                            fund_name=fund_name,
                            sector=sector,
                            sector_type=pred.get("sector_type", fund_auto_manager.get_category_for_sector(sector) if sector else "其他"),
                            prediction_type=pred.get("prediction_type"),
                            prediction_content=pred.get("prediction_content"),
                            confidence=pred.get("confidence", 50),
                            prediction_date=post_date_val,
                            prediction_period=pred.get("prediction_period", "1周"),
                            target_date=llm_analyzer.calculate_target_date(
                                post_date_val,
                                pred.get("prediction_period", "1周")
                            ),
                            next_verify_date=llm_analyzer.calculate_next_verify_date(
                                post_date_val,
                                llm_analyzer.calculate_target_date(
                                    post_date_val,
                                    pred.get("prediction_period", "1周")
                                )
                            )
                        )
                        db2.add(prediction)
                    
                    db2.commit()
                    analyzed_count += 1
                except Exception as e:
                    db2.rollback()
                    failed_count += 1
                    print(f"[PostService] 写入帖子 {post_id} 分析结果失败: {e}")
                finally:
                    db2.close()
                    
            except Exception as e:
                print(f"[PostService] 分析帖子 {post_id} 失败: {e}")
                import traceback
                traceback.print_exc()
                failed_count += 1
        
        return {
            "analyzed": analyzed_count,
            "failed": failed_count,
            "message": f"批量分析完成: 成功 {analyzed_count} 个, 失败 {failed_count} 个"
        }
    
    def delete_post(self, post_id: int) -> bool:
        """
        删除帖子
        
        Args:
            post_id: 帖子ID
            
        Returns:
            是否删除成功
        """
        post = self.get(post_id)
        if not post:
            return False
        
        self.db.delete(post)
        self.db.commit()
        return True
    
    def _match_fund_for_prediction(
        self,
        pred: dict,
        sector: str,
        fund_auto_manager,
        llm_analyzer
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        三级降级基金匹配机制
        
        优先级（按可靠性排序）：
        1. 使用 fund_auto_manager 自动匹配（优先查本地映射表）
        2. 使用 LLM 分析器的板块映射表（经过验证的映射）
        3. 使用本地默认映射表
        4. 使用LLM返回的fund_code（作为最后补充，需验证）
        
        Args:
            pred: 预测字典
            sector: 板块名称
            fund_auto_manager: 基金自动管理器
            llm_analyzer: LLM分析器
            
        Returns:
            (fund_code, fund_name)
        """
        fund_code = None
        fund_name = None
        
        # 第一级：使用 fund_auto_manager 自动匹配（优先查本地映射表）
        try:
            success, message, fund = fund_auto_manager.auto_add_fund_for_prediction(sector, self.db)
            if success and fund:
                print(f"[Fund Match] Level 1 (Auto Manager): {message}")
                return fund.fund_code, fund.fund_name
        except Exception as e:
            print(f"[Fund Match] Level 1 failed: {e}")
        
        # 第二级：使用 LLM 分析器的板块映射表（经过验证的映射）
        try:
            fund_info = llm_analyzer.get_fund_for_sector(sector)
            if fund_info:
                fund_code = fund_info.get("code")
                fund_name = fund_info.get("name")
                print(f"[Fund Match] Level 2 (LLM Mapper): {fund_name} ({fund_code})")
                return fund_code, fund_name
        except Exception as e:
            print(f"[Fund Match] Level 2 failed: {e}")
        
        # 第三级：使用本地默认映射表
        try:
            default_fund = self._get_default_fund_for_sector(sector)
            if default_fund:
                fund_code = default_fund["code"]
                fund_name = default_fund["name"]
                print(f"[Fund Match] Level 3 (Default Mapper): {fund_name} ({fund_code})")
                return fund_code, fund_name
        except Exception as e:
            print(f"[Fund Match] Level 3 failed: {e}")
        
        # 第四级：使用LLM返回的fund_code（作为最后补充，需严格验证）
        llm_fund_code = pred.get("fund_code")
        llm_fund_name = pred.get("fund_name")
        
        if llm_fund_code and str(llm_fund_code).strip():
            # 严格验证：必须是6位数字
            if len(str(llm_fund_code)) == 6 and str(llm_fund_code).isdigit():
                # 检查基金是否已存在于数据库（更可靠）
                from src.models.database import FundInfo
                existing_fund = self.db.query(FundInfo).filter(
                    FundInfo.fund_code == str(llm_fund_code)
                ).first()
                
                if existing_fund:
                    print(f"[Fund Match] Level 4 (LLM Result - Verified): {existing_fund.fund_name} ({llm_fund_code})")
                    return existing_fund.fund_code, existing_fund.fund_name
                else:
                    # 基金不存在于数据库，LLM返回的代码可能不可靠
                    print(f"[Fund Match] Level 4 (LLM Result - Unverified): {llm_fund_name} ({llm_fund_code}) - 基金不存在，跳过")
        
        # 最终降级：返回None
        print(f"[Fund Match] All levels failed, using sector name: {sector}")
        return None, None
    
    def _get_default_fund_for_sector(self, sector: str) -> Optional[dict]:
        """
        本地默认基金映射表（第三级降级）
        
        Args:
            sector: 板块名称
            
        Returns:
            基金信息字典或 None
        """
        from src.constants import get_fund_for_sector
        return get_fund_for_sector(sector)
