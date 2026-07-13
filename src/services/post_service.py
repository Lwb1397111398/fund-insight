"""
帖子服务
处理帖子相关的业务逻辑
"""
from typing import List, Optional, Dict
from datetime import date, datetime
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
import re
import logging
import json

logger = logging.getLogger(__name__)

from .base import BaseService
from src.models.database import Post, Prediction, Blogger
from src.analyzer.llm_analyzer import get_analyzer
from src.utils.fund_matching import match_fund_with_fallback


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
            **{k: v for k, v in post.__dict__.items() if not k.startswith('_')},
            "predictions": [{k: v for k, v in p.__dict__.items() if not k.startswith('_')} for p in predictions]
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
        query = self.db.query(Post).options(joinedload(Post.blogger))

        if blogger_id:
            query = query.filter(Post.blogger_id == blogger_id)
        if analyzed is not None:
            query = query.filter(Post.analyzed == analyzed)

        posts = query.order_by(Post.post_date.desc()).offset(skip).limit(limit).all()

        result = []
        for p in posts:
            blogger_name = p.blogger.name if p.blogger else "未知"
            result.append({
                "id": p.id,
                "blogger_id": p.blogger_id,
                "blogger_name": blogger_name,
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
                "success": True,
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
            # 将整个操作包裹在单个事务中，确保数据一致性
            # 如果 LLM 分析成功但创建预测失败，会回滚所有更改
            result = llm_analyzer.analyze_post(
                title=title or "",
                content=content,
                post_date=post_date.isoformat() if post_date else None
            )

            is_empty = not result.get("predictions")
            if is_empty:
                db_post.analysis_result = result
                self.db.commit()
                return {
                    "success": False,
                    "id": db_post.id,
                    "title": db_post.title,
                    "auto_titled": auto_titled,
                    "analyzed": False,
                    "predictions_created": 0,
                    "message": "分析失败：LLM未能提取有效预测（可能内容过短或无关）"
                }

            db_post.analyzed = True
            db_post.analysis_result = result
            analysis_result = result

            for pred in result.get("predictions", []):
                sector = pred.get("sector", "")

                # 使用 match_fund_with_fallback 作为最终保障
                fund_code, fund_name = match_fund_with_fallback(
                    pred=pred,
                    sector=sector,
                    fund_auto_manager=fund_auto_manager,
                    llm_analyzer=llm_analyzer,
                    db=self.db
                )

                prediction = Prediction(
                    post_id=db_post.id,
                    blogger_id=db_post.blogger_id,
                    fund_code=fund_code,
                    fund_name=fund_name,
                    sector=sector,
                    sector_type=pred.get("sector_type", fund_auto_manager.get_category_for_sector(sector) if sector else "其他"),
                    prediction_type=pred.get("prediction_type", "up"),
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
            # 回滚事务，保持数据一致性
            self.db.rollback()
            # 重新刷新帖子状态
            self.db.refresh(db_post)
            # 将 analysis_result 标记为失败状态
            try:
                db_post.analyzed = False
                db_post.analysis_result = json.dumps({"predictions": [], "summary": f"分析失败: {str(e)[:100]}"})
                self.db.commit()
            except Exception:
                pass
            print(f"[PostService] 帖子已保存（分析失败）: ID={db_post.id}")

        return {
            "success": True,
            "id": db_post.id,
            "title": db_post.title,
            "auto_titled": auto_titled,
            "analyzed": db_post.analyzed,
            "predictions_created": predictions_created,
            "message": f"分析完成，创建 {predictions_created} 个预测" if predictions_created > 0 else "分析完成，但未创建预测"
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

            is_empty = not result.get("predictions")
            if is_empty:
                db_post.analysis_result = result
                self.db.commit()
                return {
                    "success": False,
                    "message": "分析失败：LLM未能提取有效预测",
                    "predictions_created": 0
                }

            db_post.analyzed = True
            db_post.analysis_result = result
            
            for pred in result.get("predictions", []):
                sector = pred.get("sector", "")
                
                fund_code, fund_name = match_fund_with_fallback(
                    pred=pred,
                    sector=sector,
                    fund_auto_manager=fund_auto_manager,
                    llm_analyzer=llm_analyzer,
                    db=self.db
                )
                
                prediction = Prediction(
                    post_id=db_post.id,
                    blogger_id=db_post.blogger_id,
                    fund_code=fund_code,
                    fund_name=fund_name,
                    sector=sector,
                    sector_type=pred.get("sector_type", fund_auto_manager.get_category_for_sector(sector) if sector else "其他"),
                    prediction_type=pred.get("prediction_type", "up"),
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
    
    def _is_low_quality_post(self, title: str, content: str) -> tuple:
        """
        检测低质量帖子（太短、广告、闲聊）

        Returns:
            (is_low_quality, reason)
        """
        if not content:
            return True, "内容为空"

        content = content.strip()
        title = (title or "").strip()

        # 1. 内容太短（少于30个字符）
        if len(content) < 30:
            return True, f"内容过短（{len(content)}字符）"

        # 2. 广告/推广关键词
        ad_keywords = [
            '开户', '佣金', '手续费', '返现', '红包', '优惠',
            '加微信', '加群', '私聊', '联系方式', '二维码',
            '推广', '广告', '合作', '商务', '代理',
            '免费领取', '限时优惠', '点击链接'
        ]
        content_lower = content.lower()
        for kw in ad_keywords:
            if kw in content_lower:
                return True, f"疑似广告（包含'{kw}'）"

        # 3. 纯闲聊/无投资内容
        chat_keywords = ['早上好', '晚安', '吃饭', '天气', '周末愉快', '节日快乐']
        investment_keywords = ['涨', '跌', '买', '卖', '加仓', '减仓', '看涨', '看跌',
                              '板块', '基金', '股票', 'ETF', '行情', '走势', '预测',
                              '看好', '看空', '震荡', '突破', '回调', '反弹']

        has_chat = any(kw in content for kw in chat_keywords)
        has_investment = any(kw in content for kw in investment_keywords)

        if has_chat and not has_investment:
            return True, "纯闲聊内容"

        # 4. 纯表情包/符号
        text_only = re.sub(r'[^一-龥a-zA-Z0-9]', '', content)
        if len(text_only) < 10:
            return True, "内容过少（多为表情/符号）"

        return False, ""

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
                "deleted": 0,
                "skipped": 0,
                "message": "没有需要分析的帖子"
            }

        analyzed_count = 0
        failed_count = 0
        deleted_count = 0
        skipped_count = 0

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

            # 检查是否为低质量帖子
            is_low, reason = self._is_low_quality_post(title, content)
            if is_low:
                skipped_count += 1
                logger.info(f"[批量分析] 跳过低质量帖子 {post_id}: {reason}")
                continue

            try:
                result = llm_analyzer.analyze_post(
                    title=title,
                    content=content,
                    post_date=post_date_str
                )

                # 检查是否为有效分析结果（空结果不标记为已分析）
                is_empty = not result.get("predictions")

                db2 = SessionLocal()
                try:
                    post = db2.query(Post).filter(Post.id == post_id).first()
                    if not post:
                        continue

                    if is_empty:
                        # 分析失败，不标记为已分析，记录失败原因
                        post.analysis_result = result
                        db2.commit()
                        failed_count += 1
                        logger.warning(f"[批量分析] 帖子 {post_id} 分析失败（LLM返回空结果），保留未分析状态")
                        continue

                    post.analyzed = True
                    post.analysis_result = result

                    for pred in result.get("predictions", []):
                        sector = pred.get("sector", "")

                        fund_code, fund_name = match_fund_with_fallback(
                            pred=pred,
                            sector=sector,
                            fund_auto_manager=fund_auto_manager,
                            llm_analyzer=llm_analyzer,
                            db=db2
                        )

                        prediction = Prediction(
                            post_id=post.id,
                            blogger_id=blogger_id,
                            fund_code=fund_code,
                            fund_name=fund_name,
                            sector=sector,
                            sector_type=pred.get("sector_type", fund_auto_manager.get_category_for_sector(sector) if sector else "其他"),
                            prediction_type=pred.get("prediction_type", "up"),
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

        message = f"批量分析完成: 成功 {analyzed_count} 个, 失败 {failed_count} 个"
        if skipped_count > 0:
            message += f", 跳过低质量帖子 {skipped_count} 个"

        return {
            "analyzed": analyzed_count,
            "failed": failed_count,
            "deleted": deleted_count,
            "skipped": skipped_count,
            "message": message
        }
    
    def delete_post(self, post_id: int) -> bool:
        """
        删除帖子

        Args:
            post_id: 帖子ID

        Returns:
            是否删除成功

        Raises:
            ValueError: 如果帖子有关联的未删除预测
        """
        post = self.get(post_id)
        if not post:
            return False

        # 检查是否有关联的未删除预测
        active_predictions = self.db.query(Prediction).filter(
            Prediction.post_id == post_id,
            Prediction.is_deleted == False
        ).count()
        if active_predictions > 0:
            raise ValueError(f"该帖子有 {active_predictions} 条关联预测，请先删除预测")

        self.db.delete(post)
        self.db.commit()
        return True
