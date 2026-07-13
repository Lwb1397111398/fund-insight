"""
测试数据清理服务
识别和清理测试数据
"""
import re
from typing import Dict, List
from sqlalchemy.orm import Session
from sqlalchemy import or_

from src.models.database import Blogger, Post, Prediction, Viewpoint, FundInfo


class TestDataCleanupService:
    """测试数据清理服务类"""
    __test__ = False
    
    TEST_PATTERNS = [
        r'^测试',
        r'^test',
        r'^TEST',
        r'^Test',
        r'^demo',
        r'^Demo',
        r'^示例',
        r'^样例',
        r'测试数据',
        r'测试帖子',
        r'测试文章',
        r'测试内容',
        r'测试博主',
        r'测试基金',
        r'测试预测',
        r'测试观点',
    ]
    
    def __init__(self, db: Session):
        self.db = db
    
    def _matches_test_patterns(self, text: str) -> bool:
        """检查文本是否匹配测试模式（更严格的匹配）"""
        if not text:
            return False
        
        text_stripped = text.strip()
        for pattern in self.TEST_PATTERNS:
            if re.search(pattern, text_stripped, re.IGNORECASE):
                return True
        return False
    
    def find_test_bloggers(self) -> List[Dict]:
        """查找测试博主"""
        bloggers = self.db.query(Blogger).all()
        test_bloggers = []
        
        for blogger in bloggers:
            if self._matches_test_patterns(blogger.name):
                test_bloggers.append({
                    "id": blogger.id,
                    "name": blogger.name,
                    "platform": blogger.platform,
                    "posts_count": self.db.query(Post).filter(Post.blogger_id == blogger.id).count(),
                    "predictions_count": self.db.query(Prediction).filter(Prediction.blogger_id == blogger.id).count()
                })
        
        return test_bloggers
    
    def find_test_posts(self) -> List[Dict]:
        """查找测试帖子"""
        posts = self.db.query(Post).all()
        test_posts = []
        
        for post in posts:
            if self._matches_test_patterns(post.title) or self._matches_test_patterns(post.content):
                test_posts.append({
                    "id": post.id,
                    "title": post.title,
                    "blogger_id": post.blogger_id,
                    "predictions_count": self.db.query(Prediction).filter(Prediction.post_id == post.id).count()
                })
        
        return test_posts
    
    def find_test_funds(self) -> List[Dict]:
        """查找测试基金"""
        funds = self.db.query(FundInfo).all()
        test_funds = []
        
        for fund in funds:
            if self._matches_test_patterns(fund.fund_name):
                test_funds.append({
                    "id": fund.id,
                    "fund_code": fund.fund_code,
                    "fund_name": fund.fund_name,
                    "predictions_count": self.db.query(Prediction).filter(Prediction.fund_code == fund.fund_code).count()
                })
        
        return test_funds
    
    def find_test_viewpoints(self) -> List[Dict]:
        """查找测试观点"""
        viewpoints = self.db.query(Viewpoint).filter(
            Viewpoint.is_deleted == False
        ).all()
        test_viewpoints = []

        for viewpoint in viewpoints:
            if self._matches_test_patterns(viewpoint.content):
                test_viewpoints.append({
                    "id": viewpoint.id,
                    "content": viewpoint.content[:100] + "..." if len(viewpoint.content) > 100 else viewpoint.content,
                    "blogger_id": viewpoint.blogger_id
                })

        return test_viewpoints

    def find_test_predictions(self) -> List[Dict]:
        """查找测试预测"""
        predictions = self.db.query(Prediction).all()
        test_predictions = []

        for prediction in predictions:
            if self._matches_test_patterns(prediction.prediction_content) or self._matches_test_patterns(prediction.fund_name):
                test_predictions.append({
                    "id": prediction.id,
                    "post_id": prediction.post_id,
                    "blogger_id": prediction.blogger_id,
                    "fund_code": prediction.fund_code,
                    "fund_name": prediction.fund_name,
                    "prediction_content": prediction.prediction_content[:100] + "..." if prediction.prediction_content and len(prediction.prediction_content) > 100 else prediction.prediction_content,
                })

        return test_predictions

    def get_all_test_data(self) -> Dict:
        """获取所有测试数据"""
        test_bloggers = self.find_test_bloggers()
        test_posts = self.find_test_posts()
        test_funds = self.find_test_funds()
        test_viewpoints = self.find_test_viewpoints()
        test_predictions = self.find_test_predictions()

        return {
            "bloggers": test_bloggers,
            "posts": test_posts,
            "funds": test_funds,
            "viewpoints": test_viewpoints,
            "predictions": test_predictions,
            "summary": {
                "total_bloggers": len(test_bloggers),
                "total_posts": len(test_posts),
                "total_funds": len(test_funds),
                "total_viewpoints": len(test_viewpoints),
                "total_predictions": len(test_predictions),
                "total": len(test_bloggers) + len(test_posts) + len(test_funds) + len(test_viewpoints) + len(test_predictions)
            }
        }
    
    def cleanup_test_data(self) -> Dict:
        """清理所有测试数据（硬删除）"""
        test_data = self.get_all_test_data()
        
        deleted = {
            "bloggers": 0,
            "posts": 0,
            "predictions": 0,
            "viewpoints": 0,
            "funds": 0,
            "fund_history": 0
        }
        
        for blogger in test_data["bloggers"]:
            predictions = self.db.query(Prediction).filter(Prediction.blogger_id == blogger["id"]).all()
            deleted["predictions"] += len(predictions)
            for p in predictions:
                self.db.delete(p)
            
            posts = self.db.query(Post).filter(Post.blogger_id == blogger["id"]).all()
            deleted["posts"] += len(posts)
            for p in posts:
                self.db.delete(p)
            
            viewpoints = self.db.query(Viewpoint).filter(Viewpoint.blogger_id == blogger["id"]).all()
            deleted["viewpoints"] += len(viewpoints)
            for v in viewpoints:
                self.db.delete(v)
            
            blogger_obj = self.db.query(Blogger).filter(Blogger.id == blogger["id"]).first()
            if blogger_obj:
                self.db.delete(blogger_obj)
                deleted["bloggers"] += 1
        
        for post in test_data["posts"]:
            if post["blogger_id"] not in [b["id"] for b in test_data["bloggers"]]:
                predictions = self.db.query(Prediction).filter(Prediction.post_id == post["id"]).all()
                deleted["predictions"] += len(predictions)
                for p in predictions:
                    self.db.delete(p)
                
                post_obj = self.db.query(Post).filter(Post.id == post["id"]).first()
                if post_obj:
                    self.db.delete(post_obj)
                    deleted["posts"] += 1
        
        for fund in test_data["funds"]:
            predictions = self.db.query(Prediction).filter(Prediction.fund_code == fund["fund_code"]).all()
            deleted["predictions"] += len(predictions)
            for p in predictions:
                self.db.delete(p)
            
            from src.models.database import FundHistory
            history = self.db.query(FundHistory).filter(FundHistory.fund_code == fund["fund_code"]).all()
            deleted["fund_history"] += len(history)
            for h in history:
                self.db.delete(h)
            
            fund_obj = self.db.query(FundInfo).filter(FundInfo.id == fund["id"]).first()
            if fund_obj:
                self.db.delete(fund_obj)
                deleted["funds"] += 1
        
        for viewpoint in test_data["viewpoints"]:
            if viewpoint["blogger_id"] not in [b["id"] for b in test_data["bloggers"]]:
                viewpoint_obj = self.db.query(Viewpoint).filter(Viewpoint.id == viewpoint["id"]).first()
                if viewpoint_obj:
                    self.db.delete(viewpoint_obj)
                    deleted["viewpoints"] += 1

        deleted_prediction_ids = set()
        for prediction in test_data["predictions"]:
            if prediction["blogger_id"] in [b["id"] for b in test_data["bloggers"]]:
                continue
            if prediction["post_id"] in [p["id"] for p in test_data["posts"]]:
                continue
            if prediction["fund_code"] in [f["fund_code"] for f in test_data["funds"]]:
                continue
            if prediction["id"] in deleted_prediction_ids:
                continue

            prediction_obj = self.db.query(Prediction).filter(Prediction.id == prediction["id"]).first()
            if prediction_obj:
                self.db.delete(prediction_obj)
                deleted_prediction_ids.add(prediction["id"])
                deleted["predictions"] += 1

        self.db.commit()
        
        return {
            "success": True,
            "message": f"清理完成：删除了 {deleted['bloggers']} 个博主、{deleted['posts']} 条帖子、{deleted['predictions']} 条预测、{deleted['viewpoints']} 条观点、{deleted['funds']} 个基金、{deleted['fund_history']} 条历史净值",
            "deleted": deleted
        }
